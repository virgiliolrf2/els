import os
import sys
import time
import json
import signal
import argparse
import requests
import torch
import hivemind
import transformers
import threading
import queue
import io
from pathlib import Path
from cryptography.fernet import Fernet
import elysium_crypto

# --- CONFIG ---
DHT_INITIAL_PEERS = os.environ.get("ELYSIUM_INITIAL_PEERS", "").split(",")
PRIVATE_KEY_PATH = os.environ.get("ELYSIUM_KEY_PATH", "/app/node_key.pem")
JOB_ID = os.environ.get("ELYSIUM_JOB_ID", "default_job")
WORKER_ID = os.environ.get("ELYSIUM_WORKER_ID", "unknown_worker")
DATA_DIR = os.environ.get("ELYSIUM_DATA_DIR", "/app/data")
OUTPUT_DIR = os.environ.get("ELYSIUM_OUTPUT_DIR", "/app/output")

def main():
    print(f"[RUNNER] 🚀 Starting Elysium Runner for Job {JOB_ID}", flush=True)

    # 1. Load Security Key
    private_key = elysium_crypto.load_key(PRIVATE_KEY_PATH)
    if not private_key:
        print("[RUNNER] ❌ Private Key not found! generating temporary...", flush=True)
        private_key = elysium_crypto.generate_key()

    public_key_pem = elysium_crypto.get_public_key_pem(private_key)
    print(f"[RUNNER] 🔐 Identity loaded. Public Key Hash: {hash(public_key_pem)}", flush=True)

    # 2. Connect to DHT
    try:
        if not any(DHT_INITIAL_PEERS):
            print("[RUNNER] ⚠️ No initial peers provided. Starting standalone DHT.", flush=True)
            dht = hivemind.DHT(start=True)
        else:
            print(f"[RUNNER] 📡 Connecting to Swarm via {DHT_INITIAL_PEERS}...", flush=True)
            dht = hivemind.DHT(initial_peers=DHT_INITIAL_PEERS, start=True)
        print(f"[RUNNER] ✅ Connected. Visible peers: {len(dht.get_visible_maddrs())}", flush=True)
    except Exception as e:
        print(f"[RUNNER] ❌ DHT Connection Failed: {e}", flush=True)
        sys.exit(1)

    # 3. Load User Config / Model Strategy
    # We expect a 'config.json' in the workspace or passed via ENV
    config_path = Path("/app/workspace/config.json") # Mapped volume
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        # Default fallback
        config = {
            "mode": "data_parallel", # or 'model_parallel'
            "model_name": "bert-base-uncased",
            "batch_size": 32,
            "target_steps": 100
        }

    # 3.1 Fetch Dynamic Assignment from Master (Secure Config)
    # The runner asks the Master "What specific part of the graph am I?"
    # It must be authenticated.
    try:
        master_url = os.environ.get("ELYSIUM_MASTER_URL", "http://127.0.0.1:5000")
        # In V5, sign this request
        r = requests.get(f"{master_url}/api/job/config/{WORKER_ID}", timeout=5)
        if r.status_code == 200:
            data = r.json()
            if data['status'] == 'ASSIGNED':
                print(f"[RUNNER] 📥 Received Dynamic Assignment: {data['config']}", flush=True)
                config["dynamic_assignment"] = data['config']
                config["data_source"] = data['config'].get("data_source")
    except Exception as e:
        print(f"[RUNNER] ⚠️ Failed to fetch dynamic config: {e}", flush=True)

    print(f"[RUNNER] ⚙️ Mode: {config.get('mode')}", flush=True)

    # 4. Scenario A: Data Parallel (Decentralized Averaging)
    if config.get("mode") == "data_parallel":
        run_data_parallel(dht, config, private_key, public_key_pem)

    # 5. Scenario B: Model Parallel (Serving Layers)
    elif config.get("mode") == "model_parallel":
        run_model_parallel(dht, config, private_key, public_key_pem)

    else:
        print(f"[RUNNER] ❌ Unknown mode: {config.get('mode')}", flush=True)

def sign_and_publish_metrics(dht, step, loss, velocity, private_key, public_key_pem):
    """Publishes signed telemetry to DHT."""
    payload = {
        "step": step,
        "loss": loss,
        "velocity": velocity,
        "worker_id": WORKER_ID,
        "timestamp": time.time(),
        "public_key": public_key_pem
    }
    payload_str = json.dumps(payload, sort_keys=True)
    signature = elysium_crypto.sign_message(private_key, payload_str)

    entry = {
        "payload": payload,
        "signature": signature
    }

    # Key: job_{id}_metrics (Example: appendable list or just latest status per worker)
    # Hivemind DHT doesn't support appendable lists natively easily without complex CRDTs.
    # For MVP, we use a key specific to this worker: job_{id}_metrics_{worker_id}
    # Master can scan keys or we use a Subkey.

    key = f"job_{JOB_ID}_metrics_{WORKER_ID}"
    dht.store(key, json.dumps(entry), expiration_time=time.time()+600)
    # Also update global progress key with expiration (Master polls this)
    # dht.store(f"job_{JOB_ID}_progress", step, ...)

# --- SAGEMAKER-LIKE EXECUTOR ---

class Executor:
    def __init__(self, spec):
        self.spec = spec
        self.base_dir = Path("/opt/ml")
        self.input_dir = self.base_dir / "input/data"
        self.model_dir = self.base_dir / "model"
        self.output_dir = self.base_dir / "output"
        self.config_dir = self.base_dir / "input/config"

        for p in [self.input_dir, self.model_dir, self.output_dir, self.config_dir]:
            p.mkdir(parents=True, exist_ok=True)

    def prepare(self):
        print("[EXECUTOR] 📂 Setting up environment...", flush=True)

        # 1. Write Hyperparameters
        with open(self.config_dir / "hyperparameters.json", "w") as f:
            json.dump(self.spec.get("HyperParameters", {}), f)

        # 2. Download Data (Mock for MVP)
        inputs = self.spec.get("InputDataConfig", {})
        for channel, cfg in inputs.items():
            print(f"[EXECUTOR] ⬇️ Downloading channel: {channel}...", flush=True)
            # In real impl, parse S3Uri and download
            # For MVP, create dummy file
            c_dir = self.input_dir / channel
            c_dir.mkdir(exist_ok=True)
            with open(c_dir / "data.txt", "w") as f: f.write("dummy data")

    def run(self):
        algo = self.spec.get("AlgorithmSpecification", {})
        entry_points = algo.get("ContainerEntrypoint", [])

        if not entry_points:
            print("[EXECUTOR] ❌ No entrypoint defined.", flush=True)
            return

        cmd = entry_points
        print(f"[EXECUTOR] 🚀 Executing: {cmd}", flush=True)

        # Set Env Vars
        env = os.environ.copy()
        env["SM_MODEL_DIR"] = str(self.model_dir)
        env["SM_OUTPUT_DATA_DIR"] = str(self.output_dir)
        env["SM_CHANNEL_TRAIN"] = str(self.input_dir / "train")

        # Execute User Code
        try:
            # Here we assume the entrypoint script exists or is passed.
            # In a real scenario, we'd download the SourceCode S3Uri.
            # For MVP, we create a dummy train.py if it's "train.py" and doesn't exist
            if cmd[0] == "train.py" and not os.path.exists("train.py"):
                with open("train.py", "w") as f:
                    f.write("import os; import time; print('Hello form User Script!'); time.sleep(10); print('Done');")

            subprocess.run(["python"] + cmd, check=True, env=env)
            print("[EXECUTOR] ✅ Execution Successful.", flush=True)
        except Exception as e:
            print(f"[EXECUTOR] ❌ Execution Failed: {e}", flush=True)

    def upload_artifacts(self):
        print("[EXECUTOR] ⬆️ Uploading artifacts...", flush=True)
        # Mock Upload
        pass

def run_executor_mode(spec):
    exe = Executor(spec)
    exe.prepare()
    exe.run()
    exe.upload_artifacts()

if __name__ == "__main__":
    # Check if config.json exists (mounted by Node)
    config_path = Path("/app/workspace/config.json")
    if config_path.exists():
        with open(config_path) as f:
            spec = json.load(f)
        # If it's a new style Job Spec
        if "AlgorithmSpecification" in spec:
            run_executor_mode(spec)
        else:
            main() # Fallback to old runner logic if needed, or remove
    else:
        print("[RUNNER] ❌ No configuration found.", flush=True)
