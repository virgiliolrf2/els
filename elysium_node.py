import os
import time
import sys
import argparse
import requests
import threading
import psutil
from pathlib import Path
import docker
import elysium_crypto

# --- ARGUMENTS & CONFIG ---
parser = argparse.ArgumentParser()
parser.add_argument("--master_url", type=str, default="http://127.0.0.1:5000")
args = parser.parse_args()

BASE_DIR = Path.cwd().resolve()
WORKSPACE = BASE_DIR / "node_workspace"
KEY_PATH = BASE_DIR / "elysium_node_key.pem"
IMAGE_NAME = "elysium-worker:latest"

if not WORKSPACE.exists():
    WORKSPACE.mkdir(parents=True, exist_ok=True)

# --- IDENTITY & SECURITY ---
if not KEY_PATH.exists():
    print("[INIT] 🔑 Generating new Node Identity...", flush=True)
    pk = elysium_crypto.generate_key()
    elysium_crypto.save_key(pk, KEY_PATH)

def get_hardware_profile():
    """Detects VRAM, RAM, TFLOPS (Simulated), and Bandwidth."""
    profile = {
        "vram_gb": 0.0,
        "ram_gb": 0.0,
        "bandwidth_mbps": 100.0, # Default / Simulated
        "region": "unknown",
        "compute_score": 0.0
    }

    # 1. RAM
    try:
        profile["ram_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
    except: pass

    # 2. VRAM (NVIDIA)
    # Since we are on host, we can try nvidia-smi
    try:
        import subprocess
        # Get Total Memory
        res = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        vram_mb = float(res.decode().strip())
        profile["vram_gb"] = round(vram_mb / 1024, 2)

        # Get Compute Capability / Name (Rough TFLOPS proxy)
        res_name = subprocess.check_output(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        name = res_name.decode().strip()

        # Heuristic Scoring for Orchestrator
        score = 10.0 # Base (e.g. T4)
        if "A100" in name: score = 100.0
        elif "H100" in name: score = 300.0
        elif "3090" in name: score = 30.0
        elif "4090" in name: score = 60.0
        profile["compute_score"] = score

    except Exception:
        # Fallback/Simulation for Dev Environment
        # print(f"[HW] GPU Detection failed or no GPU. Using fallback.", flush=True)
        profile["vram_gb"] = 8.0 # Simulate a basic consumer GPU
        profile["compute_score"] = 5.0

    # 3. Region / Bandwidth
    # In real deployment, we might ping a benchmark server or check public IP geo.
    # For MVP, we simulate or assume "US-East"
    profile["region"] = os.environ.get("ELYSIUM_REGION", "global")

    return profile

def register_worker():
    """Registers this worker with the Master so it knows our Public Key."""
    try:
        pk = elysium_crypto.load_key(KEY_PATH)
        pub_pem = elysium_crypto.get_public_key_pem(pk)

        # We need a stable ID for the session. In reality, this should be persisted too.
        # For MVP, we generate a session ID or check if we can persist it.
        # Let's use a hash of the public key or just a random ID if we don't care about restart persistence.
        # But 'elysium_runner' generates its own ID?
        # Ideally, Node Manager and Runner share the ID.

        # Let's persist ID in a file
        id_path = BASE_DIR / "node_id.txt"
        if id_path.exists():
            with open(id_path, 'r') as f: wid = f.read().strip()
        else:
            wid = f"node_{os.urandom(3).hex()}"
            with open(id_path, 'w') as f: f.write(wid)

        profile = get_hardware_profile()
        print(f"[INIT] 📝 Registering {wid} with Master (VRAM: {profile['vram_gb']}GB)...", flush=True)

        requests.post(f"{args.master_url}/api/job/heartbeat", json={
            "worker_id": wid,
            "public_key": pub_pem,
            "hardware": profile
        }, timeout=5)
        return wid
    except Exception as e:
        print(f"[INIT] ⚠️ Registration Warning: {e}", flush=True)
        return "unknown_node"

# --- DOCKER CLIENT ---
try:
    docker_client = docker.from_env()
    print("[INIT] 🐳 Docker Client connected.", flush=True)
except Exception as e:
    print(f"[ERR] ❌ Could not connect to Docker: {e}. Is the daemon running?", flush=True)
    sys.exit(1)

# --- ADAPTIVE COMPUTE MONITOR ---
class ResourceMonitor(threading.Thread):
    def __init__(self, pause_event):
        super().__init__(daemon=True)
        self.pause_event = pause_event
        self.running = True

    def run(self):
        print("[MONITOR] 🛡️ Adaptive Compute Active.", flush=True)
        while self.running:
            # Check System Load
            # If GPU usage or CPU usage is too high (user playing game), pause.
            # Since we don't have easy cross-platform GPU check without 3rd party libs (GPUtil),
            # we will rely on CPU/RAM for this MVP or assume user manually manages.
            # But let's check CPU > 90%

            cpu_usage = psutil.cpu_percent(interval=1)
            if cpu_usage > 90:
                if not self.pause_event.is_set():
                    print(f"[MONITOR] ⚠️ High CPU ({cpu_usage}%). Pausing Worker...", flush=True)
                    self.pause_event.set() # Pause
            else:
                if self.pause_event.is_set():
                    print(f"[MONITOR] ✅ CPU Normalized ({cpu_usage}%). Resuming...", flush=True)
                    self.pause_event.clear() # Resume

            time.sleep(5)

# --- MAIN NODE LOGIC ---

def build_or_pull_image():
    # In MVP, we build from local Dockerfile
    dockerfile = BASE_DIR / "Dockerfile"
    if dockerfile.exists():
        print("[DOCKER] 🔨 Building Elysium Worker Image...", flush=True)
        try:
            # Copy crypto lib to context if needed, but Dockerfile handles COPY
            # Assuming cwd has Dockerfile, elysium_runner.py, elysium_crypto.py
            docker_client.images.build(path=str(BASE_DIR), tag=IMAGE_NAME, rm=True)
            print("[DOCKER] ✅ Build Complete.", flush=True)
            return True
        except Exception as e:
            print(f"[DOCKER] ❌ Build Failed: {e}", flush=True)
            return False
    else:
        print("[DOCKER] ❌ Dockerfile not found.", flush=True)
        return False

def run_worker_container(job_config, master_peers, worker_id):
    """
    Runs the container with the Elysium Runner.
    """
    env_vars = {
        "ELYSIUM_JOB_ID": job_config.get("job_id"),
        "ELYSIUM_WORKER_ID": worker_id,
        "ELYSIUM_INITIAL_PEERS": ",".join(master_peers),
        "ELYSIUM_KEY_PATH": "/app/node_key.pem",
        "ELYSIUM_MASTER_URL": args.master_url, # Pass Master URL for Secure Config Fetch
    }

    # Mounts
    volumes = {
        str(WORKSPACE): {'bind': '/app/workspace', 'mode': 'rw'},
        str(KEY_PATH): {'bind': '/app/node_key.pem', 'mode': 'ro'}
    }

    try:
        print(f"[DOCKER] 🚀 Launching Container for Job {job_config.get('job_id')}...", flush=True)
        # Configure GPU Request (NVIDIA Container Toolkit)
        device_requests = [
            docker.types.DeviceRequest(count=-1, capabilities=[['gpu']])
        ]

        container = docker_client.containers.run(
            IMAGE_NAME,
            detach=True,
            environment=env_vars,
            volumes=volumes,
            network_mode="host", # Needed for P2P/DHT ease of access
            device_requests=device_requests,
            auto_remove=True
        )
        return container
    except Exception as e:
        print(f"[DOCKER] ❌ Run Error: {e}", flush=True)
        return None

def main():
    print("--- ELYSIUM NODE v4.0 (P2P Hivemind) ---", flush=True)

    # 1. Build Image
    if not build_or_pull_image():
        return

    # 2. Register Identity
    worker_id = register_worker()

    # 3. Start Monitor
    pause_event = threading.Event()
    monitor = ResourceMonitor(pause_event)
    monitor.start()

    current_container = None
    last_job_id = None

    while True:
        if pause_event.is_set():
            if current_container:
                print("[NODE] ⏸️ Pausing Container...", flush=True)
                try:
                    current_container.stop()
                    current_container = None
                except: pass
            time.sleep(5)
            continue

        try:
            # Poll Master for Job Config (Coordinator)
            # Master no longer receives uploads, but tells us "The Job is X, Peers are Y, Z"
            r = requests.get(f"{args.master_url}/api/job/current", timeout=5)
            if r.status_code == 200:
                data = r.json()
                meta = data.get('meta', {})
                status = meta.get('status')
                job_id = meta.get('job_id')
                peers = meta.get('initial_peers', [])

                if status == 'ACTIVE' and job_id:
                    if job_id != last_job_id:
                        # Stop old if exists
                        if current_container:
                            current_container.stop()

                        # Start new
                        print(f"[NODE] 🆕 Received Job {job_id}. Swarm Peers: {len(peers)}", flush=True)

                        # Prepare Config for Runner
                        # We dump the job metadata to config.json so the runner knows the mode (Data/Model Parallel)
                        import json
                        config_path = WORKSPACE / "config.json"
                        with open(config_path, "w") as f:
                            json.dump(meta, f)

                        current_container = run_worker_container(meta, peers, worker_id)
                        last_job_id = job_id

                    # If container died or finished?
                    if current_container:
                        current_container.reload()
                        if current_container.status == 'exited':
                            print("[NODE] ⚠️ Container exited.", flush=True)
                            current_container = None
                            last_job_id = None # Reset to try again or wait

                elif status != 'ACTIVE' and current_container:
                    print("[NODE] 🛑 Job ended. Stopping container.", flush=True)
                    current_container.stop()
                    current_container = None
                    last_job_id = None

            time.sleep(10)

        except requests.exceptions.ConnectionError:
            print(f"[NET] ⚠️ Master unreachable {args.master_url}...", flush=True)
            time.sleep(10)
        except KeyboardInterrupt:
            print("\n[SYS] Shutting down...", flush=True)
            if current_container: current_container.stop()
            sys.exit(0)
        except Exception as e:
            print(f"[ERR] Loop Error: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
