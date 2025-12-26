import sys, os, subprocess, time, threading, sqlite3, json
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
from werkzeug.utils import secure_filename
import elysium_crypto

# --- 1. BOOTLOADER HÍBRIDO (WINDOWS -> WSL) ---
# Se estiver rodando no Windows, ele se "auto-injeta" no WSL
if os.name == 'nt':
    print("[BOOT] 🖥️ Detectado Windows. Preparando ambiente WSL Linux...", flush=True)

    current_script = os.path.abspath(__file__)
    drive, path = os.path.splitdrive(current_script)
    wsl_path = f"/mnt/{drive.lower().replace(':', '')}{path.replace(os.sep, '/')}"

    # Simple dependency check for WSL
    try:
        subprocess.check_call(["wsl", "bash", "-c", "sudo apt update && sudo apt install -y python3 python3-pip python3-flask && pip install hivemind cryptography torch"])
    except: pass

    print(f"[BOOT] 🚀 Lançando Master dentro do Linux: {wsl_path}")
    subprocess.call(["wsl", "python3", wsl_path])
    sys.exit(0)

# ==============================================================================
# DAQUI PARA BAIXO É CÓDIGO LINUX (RODANDO DENTRO DO WSL OU LINUX NATIVO)
# ==============================================================================

import hivemind

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- CONFIG ---
DB_FILE = "elysium_ledger_v3.db"
STORAGE_DIR = Path("./elysium_storage")
for p in [STORAGE_DIR]: p.mkdir(parents=True, exist_ok=True)

CURRENT_MISSION = {
    "job_id": None,
    "status": "IDLE",
    "initial_peers": [],
    "logs": [],
    "swarm_velocity": 0.0,
    "active_peers_count": 0
}

DHT = None

def log_master(msg):
    ts = time.strftime('%H:%M:%S')
    print(f"[MASTER] {msg}", flush=True)
    CURRENT_MISSION["logs"].append(f"{ts} - {msg}")
    if len(CURRENT_MISSION["logs"]) > 100: CURRENT_MISSION["logs"].pop(0)

# --- DB ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    # Added hardware_specs column (JSON text) and region
    conn.execute("CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, last_seen REAL, balance REAL, total_steps INTEGER, public_key TEXT, hardware_specs TEXT, region TEXT)")
    conn.commit(); conn.close()

def update_worker_credit(wid, steps_inc, amount, public_key=None, hardware=None):
    conn = sqlite3.connect(DB_FILE)
    if public_key:
        # Check if hardware info provided
        hw_str = json.dumps(hardware) if hardware else "{}"
        region = hardware.get("region", "global") if hardware else "global"

        # Upsert logic (simplistic)
        conn.execute("INSERT OR REPLACE INTO workers (worker_id, last_seen, balance, total_steps, public_key, hardware_specs, region) VALUES (?, ?, COALESCE((SELECT balance FROM workers WHERE worker_id=?), 0), COALESCE((SELECT total_steps FROM workers WHERE worker_id=?), 0), ?, ?, ?)",
                     (wid, time.time(), wid, wid, public_key, hw_str, region))
    else:
        conn.execute("UPDATE workers SET last_seen=?, balance=balance+?, total_steps=total_steps+? WHERE worker_id=?",
                     (time.time(), amount, steps_inc, wid))
    conn.commit(); conn.close()

def get_worker_balance(wid):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.execute("SELECT balance FROM workers WHERE worker_id=?", (wid,))
    res = cur.fetchone()
    conn.close()
    return res[0] if res else 0.0

# --- ORCHESTRATOR & SCHEDULER ---

class Orchestrator:
    def __init__(self):
        self.topology_map = {} # { worker_id: { "layers": [0,1,2], "data_key": "..." } }
        self.lock = threading.Lock()

    def schedule_job(self, job_spec):
        """
        Dynamically assigns layers to workers based on VRAM and Latency.
        """
        log_master("🧠 Orchestrator: Calculating Topology...")
        conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
        workers = conn.execute("SELECT * FROM workers WHERE last_seen > ?", (time.time() - 300,)).fetchall()
        conn.close()

        if not workers:
            log_master("❌ No active workers for scheduling.")
            return

        # 1. Clustering by Region (Latency Aware)
        clusters = {}
        for w in workers:
            reg = w['region'] or 'global'
            if reg not in clusters: clusters[reg] = []
            clusters[reg].append(w)

        # Select biggest cluster for Model Parallelism (to minimize cross-region latency)
        # For Data Parallel, we can use all.
        primary_region = max(clusters, key=lambda k: len(clusters[k]))
        active_pool = clusters[primary_region]
        log_master(f"🌐 Latency Optimization: Selected region '{primary_region}' with {len(active_pool)} nodes.")

        # 2. Hardware Profiling & Sharding
        # Simple Logic: Sort by Compute Score (VRAM/Speed)
        active_pool.sort(key=lambda w: json.loads(w['hardware_specs']).get('compute_score', 0), reverse=True)

        total_layers = job_spec.get('total_layers', 32)

        # Calculate total compute power
        total_score = sum([json.loads(w['hardware_specs']).get('compute_score', 0) for w in active_pool])
        if total_score == 0: total_score = 1

        current_layer = 0
        new_topology = {}

        for w in active_pool:
            specs = json.loads(w['hardware_specs'])
            score = specs.get('compute_score', 0)

            # Proportional assignment
            share = score / total_score
            n_layers = int(math.ceil(share * total_layers))

            # Boundary Check
            if current_layer >= total_layers: n_layers = 0
            elif current_layer + n_layers > total_layers: n_layers = total_layers - current_layer

            if n_layers > 0:
                new_topology[w['worker_id']] = {
                    "layer_start": current_layer,
                    "num_layers": n_layers,
                    "data_source": job_spec.get("data_source") # S3 Keys / Presigned URL
                }
                current_layer += n_layers
            else:
                # Workers with no layers (or overflow) can be Data Parallel helpers or idle
                new_topology[w['worker_id']] = {"role": "helper", "data_source": job_spec.get("data_source")}

        with self.lock:
            self.topology_map = new_topology

        log_master(f"✅ Schedule Complete. Assigned {len(new_topology)} nodes.")

SCHEDULER = Orchestrator()

# --- HIVEMIND DHT & MONITOR ---
def start_dht_service():
    global DHT
    log_master("🚀 Iniciando DHT Bootstrap Node...")
    try:
        # Start a DHT node that serves as an entry point
        DHT = hivemind.DHT(start=True, host_maddrs=["/ip4/0.0.0.0/tcp/8001"])
        visible = DHT.get_visible_maddrs()
        CURRENT_MISSION["initial_peers"] = [str(a) for a in visible]
        log_master(f"🌐 DHT Online. Peers: {CURRENT_MISSION['initial_peers']}")

        # Start passive monitor thread
        threading.Thread(target=monitor_swarm_metrics, daemon=True).start()
    except Exception as e:
        log_master(f"❌ Falha ao iniciar DHT: {e}")

def monitor_swarm_metrics():
    """
    Background thread that scans the DHT for worker metrics.
    Since we don't have a list of all workers, we rely on workers
    reporting to known keys or we scan a range if possible.
    For MVP: We scan keys if we knew the worker IDs, OR we rely on Heartbeats to
    tell us "I am worker X", then we verify X's metrics in DHT.
    """
    while True:
        if CURRENT_MISSION["status"] == "ACTIVE" and DHT:
            # For this prototype, we will iterate over workers we know from DB
            # and check their specific metric keys in DHT.

            conn = sqlite3.connect(DB_FILE)
            workers = conn.execute("SELECT worker_id, public_key FROM workers").fetchall()
            conn.close()

            total_velocity = 0.0
            active_count = 0

            for wid, pub_key in workers:
                key = f"job_{CURRENT_MISSION['job_id']}_metrics_{wid}"
                entry = DHT.get(key, latest=True)

                if entry and entry.value:
                    try:
                        data = json.loads(entry.value)
                        payload = data['payload']
                        signature = data['signature']

                        # Verify Signature
                        # Reconstruct string to verify
                        payload_str = json.dumps(payload, sort_keys=True)

                        # SECURITY FIX: Verify against the stored public key (pub_key) from DB,
                        # NOT the one inside the payload (which could be spoofed).
                        # Only fall back to payload key if we are registering a new worker (handled elsewhere usually)
                        # Here we iterate known workers, so pub_key is trusted from DB.

                        key_to_use = pub_key if pub_key else payload['public_key']

                        if elysium_crypto.verify_signature(key_to_use, payload_str, signature):
                            # Valid Metric
                            last_ts = payload.get('timestamp', 0)
                            if time.time() - last_ts < 30: # Active in last 30s
                                active_count += 1
                                total_velocity += payload.get('velocity', 0)

                                # Credit the worker (Micro-transaction simulation)
                                # Only credit if step > last_known_step?
                                # For MVP, we just bump "last_seen" here and assume
                                # 'api/job/heartbeat' handles bulk crediting or we do it here.
                                # Let's do it here: Pay per active second implies verifying work.
                                # We'll just update 'last_seen' here to confirm they are really working.
                                update_worker_credit(wid, 0, 0.0001, pub_key) # Credit 0.0001 per verification loop (~5s)
                    except Exception as e:
                        # log_master(f"Bad metric for {wid}: {e}")
                        pass

            CURRENT_MISSION["swarm_velocity"] = total_velocity
            CURRENT_MISSION["active_peers_count"] = active_count

        time.sleep(5)

# --- API ---

@app.route('/api/job/current')
def api_job_current():
    return jsonify({
        "meta": CURRENT_MISSION,
        "active_peers": CURRENT_MISSION["active_peers_count"],
        "swarm_velocity": CURRENT_MISSION["swarm_velocity"]
    })

@app.route('/api/wallet/balance/<worker_id>')
def api_wallet(worker_id):
    bal = get_worker_balance(worker_id)
    return jsonify({"worker_id": worker_id, "balance": bal})

@app.route('/api/job/heartbeat', methods=['POST'])
def api_heartbeat():
    # Workers report here to announce presence (Initial Registration)
    # Payload: { "worker_id": "...", "public_key": "PEM...", "hardware": {...} }
    data = request.json
    wid = data.get('worker_id')
    pk = data.get('public_key') # Optional if already known
    hw = data.get('hardware')

    if wid:
        update_worker_credit(wid, 0, 0.0001, pk, hw) # Tiny keep-alive credit + Register HW

    return jsonify({"status": "ack", "peers": CURRENT_MISSION["initial_peers"]})

@app.route('/api/job/config/<worker_id>')
def api_secure_config(worker_id):
    """
    Securely delivers the assigned topology/shard and private data keys
    to a specific authenticated worker.
    """
    # In V5.0, verify signature of request.
    # For now, simple ID check
    with SCHEDULER.lock:
        assignment = SCHEDULER.topology_map.get(worker_id)

    if assignment:
        return jsonify({"status": "ASSIGNED", "config": assignment})
    else:
        # Default config if not scheduled yet
        return jsonify({"status": "WAITING", "config": {"role": "standby"}})

# --- UI ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Start New Mission
        CURRENT_MISSION["job_id"] = f"JOB_{int(time.time())}"
        CURRENT_MISSION["status"] = "ACTIVE"
        CURRENT_MISSION["mode"] = request.form.get("mode", "data_parallel")

        # New: Trigger Scheduler
        job_spec = {
            "mode": CURRENT_MISSION["mode"],
            "total_layers": 32, # Example for LLM
            "data_source": {"s3_bucket": "secure-bucket", "key": "train-data.parquet"}
        }
        SCHEDULER.schedule_job(job_spec)

        log_master(f"🆕 Mission Started: {CURRENT_MISSION['job_id']} [{CURRENT_MISSION['mode']}]")

    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    workers = conn.execute("SELECT * FROM workers ORDER BY last_seen DESC").fetchall()
    conn.close()

    return render_template_string("""
<!DOCTYPE html>
<html>
<head>
    <title>Elysium V4.0 | Supercloud</title>
    <style>
        body { background: #0f172a; color: #fff; font-family: sans-serif; padding: 20px; }
        .card { background: #1e293b; padding: 20px; margin-bottom: 20px; border-radius: 8px; }
        .metric { font-size: 2em; font-weight: bold; }
        .success { color: #22c55e; }
        table { width: 100%; border-collapse: collapse; }
        td, th { padding: 10px; border-bottom: 1px solid #334155; text-align: left; }
    </style>
    <script>
        setInterval(() => {
            fetch('/api/job/current').then(r=>r.json()).then(d => {
                document.getElementById('peers').innerText = d.active_peers;
                document.getElementById('vel').innerText = d.swarm_velocity.toFixed(2) + ' step/s';
                document.getElementById('job').innerText = d.meta.job_id || "IDLE";
            });
        }, 2000);
    </script>
</head>
<body>
    <h1>Elysium V4.0 (Hivemind + Docker)</h1>

    <div class="card" style="display:flex; justify-content: space-around;">
        <div>
            <div style="color:#94a3b8">Active Job</div>
            <div id="job" class="metric">{{ mission.job_id or 'IDLE' }}</div>
        </div>
        <div>
            <div style="color:#94a3b8">Active Swarm Peers</div>
            <div id="peers" class="metric success">{{ active_peers }}</div>
        </div>
        <div>
            <div style="color:#94a3b8">Global Velocity</div>
            <div id="vel" class="metric">{{ velocity }}</div>
        </div>
    </div>

    <div class="card">
        <h2>Start New Mission</h2>
        <form method="post">
            <select name="mode" style="padding:10px">
                <option value="data_parallel">Data Parallel (Small Models)</option>
                <option value="model_parallel">Model Parallel (LLMs / 70B+)</option>
            </select>
            <button style="padding:10px; background: #2563eb; color:#fff; border:none; cursor:pointer">LAUNCH SWARM</button>
        </form>
    </div>

    <div class="card">
        <h2>Worker Fleet</h2>
        <table>
            <thead><tr><th>ID</th><th>Last Seen</th><th>Balance</th><th>Steps</th></tr></thead>
            <tbody>
                {% for w in workers %}
                <tr>
                    <td>{{ w.worker_id }}</td>
                    <td>{{ "%.0f"|format(time.time() - w.last_seen) }}s ago</td>
                    <td class="success">${{ "%.5f"|format(w.balance) }}</td>
                    <td>{{ w.total_steps }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</body>
</html>
    """, mission=CURRENT_MISSION, workers=workers, active_peers=CURRENT_MISSION["active_peers_count"], velocity=CURRENT_MISSION["swarm_velocity"], time=time)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_dht_service, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
