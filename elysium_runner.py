import os
import sys
import time
import json
import signal
import argparse
import torch
import hivemind
import transformers
from pathlib import Path
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

def run_data_parallel(dht, config, private_key, public_key_pem):
    print("[RUNNER] 🧠 Initializing Data Parallel Training...", flush=True)

    # Load Model (Transformers)
    model_name = config.get("model_name", "bert-base-uncased")
    model = transformers.AutoModelForMaskedLM.from_pretrained(model_name)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-5)

    # Hivemind Decentralized Optimizer
    opt = hivemind.Optimizer(
        dht=dht,
        run_id=f"{JOB_ID}_run",
        batch_size_per_step=config.get("batch_size", 32),
        target_batch_size=1000, # Large batch simulation
        optimizer=optimizer,
        use_local_updates=True,
        matchmaking_time=5.0,
        averaging_timeout=10.0,
        verbose=True
    )

    # Graceful Shutdown Handler
    def shutdown_handler(signum, frame):
        print(f"\n[RUNNER] 🛑 Caught signal {signum}. Shutting down optimizer...", flush=True)
        opt.shutdown()
        dht.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    print("[RUNNER] 🚀 Loop Started.", flush=True)
    step = 0
    start_time = time.time()

    # Mock Data Loop (In real usage, we load dataset)
    while step < config.get("target_steps", 100):
        # Mock Training Step
        optimizer.zero_grad()
        dummy_input = torch.randint(0, 1000, (8, 128)) # Batch 8
        outputs = model(dummy_input, labels=dummy_input)
        loss = outputs.loss
        loss.backward()

        # Hivemind Step (Averaging)
        opt.step()

        step += 1
        elapsed = time.time() - start_time
        velocity = step / elapsed if elapsed > 0 else 0

        if step % 10 == 0:
            print(f"[RUNNER] Step {step} | Loss: {loss.item():.4f} | Vel: {velocity:.2f} step/s", flush=True)
            sign_and_publish_metrics(dht, step, loss.item(), velocity, private_key, public_key_pem)

    print("[RUNNER] ✅ Training Complete.", flush=True)
    opt.shutdown()

def run_model_parallel(dht, config, private_key, public_key_pem):
    print("[RUNNER] 🧩 Initializing Model Parallel (Layer Serving)...", flush=True)

    model_name = config.get("model_name", "gpt2")
    layer_start = config.get("layer_start", 0)
    num_layers = config.get("num_layers", 4)

    # 1. Load Model Config
    print(f"[RUNNER] Loading config for {model_name}...", flush=True)
    model_config = transformers.AutoConfig.from_pretrained(model_name)

    # 2. Instantiate Specific Layers (Slice)
    # This logic assumes a Transformer structure (like GPT/BERT) where we can extract blocks.
    # For a 70B model, we would use 'accelerate' to init empty and then load weights.
    # Here we define a simple wrapper to hold the layers.

    # Check for Dynamic Assignment from Master
    if config.get("dynamic_assignment"):
        # Overwrite defaults with what Orchestrator assigned
        layer_start = config["dynamic_assignment"].get("layer_start", layer_start)
        num_layers = config["dynamic_assignment"].get("num_layers", num_layers)
        print(f"[RUNNER] 🔄 Dynamic Re-Assignment: Layers {layer_start}-{layer_start+num_layers}", flush=True)

    # Secure Data Streamer (Stub)
    if config.get("data_source"):
        ds = config["data_source"]
        print(f"[RUNNER] 🔒 Initializing Secure Data Stream from {ds.get('s3_bucket')}...", flush=True)
        # In real impl: boto3.client(...).get_object()

    print(f"[RUNNER] Serving layers {layer_start} to {layer_start + num_layers}...", flush=True)

    class LayerSlice(torch.nn.Module):
        def __init__(self, config, start, num):
            super().__init__()
            # Attempt to find the 'h' or 'layers' attribute typical in Transformers
            # This is a generic heuristic for MVP.
            self.layers = torch.nn.ModuleList()
            # In a real implementation we would load only these weights from disk/stream.
            # For now, we instantiate fresh layers to simulate memory usage of that slice.
            # If the model is huge, we'd use meta-device.

            # Using a dummy linear layer stack to simulate compute/memory cost of a Transformer Block
            hidden_size = getattr(config, 'hidden_size', 768)
            for _ in range(num):
                self.layers.append(torch.nn.Linear(hidden_size, hidden_size))

        def forward(self, hidden_states):
            for layer in self.layers:
                hidden_states = layer(hidden_states)
            return hidden_states

    model_slice = LayerSlice(model_config, layer_start, num_layers)

    # 3. Hivemind ModuleBackend
    # We expose this slice as a remote module.
    # Peers can call 'dht.run_remote_module(uid, inputs)'
    uid = f"{JOB_ID}_layers_{layer_start}_{layer_start+num_layers}"

    # Note: hivemind.ModuleBackend requires a specialized forward signature usually.
    # We wrap it in a ModuleBackend to handle P2P requests.

    backend = hivemind.ModuleBackend(
        module=model_slice,
        optimizer=None, # Inference only for this slice example, or add optimizer
        args_schema=(hivemind.BatchTensorDescriptor((1, model_config.hidden_size), compression=hivemind.Uniform8BitQuantization()),),
        outputs_schema=hivemind.BatchTensorDescriptor((1, model_config.hidden_size), compression=hivemind.Uniform8BitQuantization()),
    )

    server = hivemind.Server(dht=dht, module_backends={uid: backend}, num_connection_handlers=10)

    print(f"[RUNNER] 🚀 Serving Module UID: {uid}", flush=True)
    server.start()

    # Graceful Shutdown Handler for Server
    def shutdown_server_handler(signum, frame):
        print(f"\n[RUNNER] 🛑 Caught signal {signum}. Shutting down server...", flush=True)
        server.shutdown()
        dht.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_server_handler)
    signal.signal(signal.SIGINT, shutdown_server_handler)

    try:
        while True:
            time.sleep(5)
            # Heartbeat showing we are serving
            sign_and_publish_metrics(dht, 0, 0.0, 1.0, private_key, public_key_pem) # Vel=1.0 means active
    except Exception as e:
        print(f"[RUNNER] Error in server loop: {e}", flush=True)
        server.shutdown()

if __name__ == "__main__":
    main()
