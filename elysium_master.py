import sys, os, subprocess, time, threading, sqlite3, json, math, uuid
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
import flask
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import elysium_crypto

# --- 1. BOOTLOADER HÍBRIDO (WINDOWS -> WSL) ---
if os.name == 'nt':
    print("[BOOT] 🖥️ Detectado Windows. Preparando ambiente WSL Linux...", flush=True)
    current_script = os.path.abspath(__file__)
    drive, path = os.path.splitdrive(current_script)
    wsl_path = f"/mnt/{drive.lower().replace(':', '')}{path.replace(os.sep, '/')}"
    venv_path = "~/.elysium_master_env"

    print("[BOOT] 🛠️ Configurando Ambiente Virtual no WSL (Evita conflitos PEP 668)...")
    try:
        subprocess.check_call(["wsl", "bash", "-c", "sudo apt update && sudo apt install -y python3 python3-venv python3-pip golang-go"])
        setup_cmd = (
            f"if [ ! -d {venv_path} ]; then python3 -m venv {venv_path}; fi && "
            f"{venv_path}/bin/pip install --quiet hivemind cryptography torch flask transformers datasets"
        )
        subprocess.check_call(["wsl", "bash", "-c", setup_cmd])
    except subprocess.CalledProcessError as e:
        print(f"[BOOT] ⚠️ Erro na instalação de dependências: {e}")

    print(f"[BOOT] 🚀 Lançando Master dentro do Linux (Venv): {wsl_path}")
    launch_cmd = f"{venv_path}/bin/python3 {wsl_path}"
    subprocess.call(["wsl", "bash", "-c", launch_cmd])
    sys.exit(0)

# ==============================================================================
# DAQUI PARA BAIXO É CÓDIGO LINUX
# ==============================================================================

def check_and_fix_hivemind():
    try:
        import hivemind.hivemind_cli as cli
        p2pd_path = os.path.join(os.path.dirname(cli.__file__), 'p2pd')
        if not os.path.exists(p2pd_path): raise FileNotFoundError("p2pd binary missing")
        try:
            proc = subprocess.Popen([p2pd_path, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired: proc.kill()
        except Exception as e: raise e
    except Exception as e:
        print(f"[BOOT] 🛠️ Rebuilding Hivemind from Source...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-binary", "hivemind", "hivemind"])
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as build_err:
            print(f"[BOOT] ❌ Critical Failure rebuilding Hivemind: {build_err}")
            sys.exit(1)

check_and_fix_hivemind()
import hivemind
import transformers
import datasets

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.jinja_env.filters['from_json'] = json.loads

# --- CONFIG ---
DB_FILE = "elysium_ledger_v4.db"
STORAGE_DIR = Path("./elysium_storage")
for p in [STORAGE_DIR]: p.mkdir(parents=True, exist_ok=True)

CURRENT_MISSION = {
    "job_id": None,
    "status": "IDLE",
    "initial_peers": [],
    "logs": [],
    "swarm_velocity": 0.0,
    "active_peers_count": 0,
    "mode": "data_parallel"
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
    # Workers: Hardware telemetry + Owner
    conn.execute("CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, last_seen REAL, balance REAL, total_steps INTEGER, public_key TEXT, hardware_specs TEXT, region TEXT, owner_wallet_id TEXT)")
    # Users: Auth + Wallet
    conn.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, password_hash TEXT, wallet_id TEXT UNIQUE, balance REAL DEFAULT 0.0, created_at REAL)")
    # Withdrawals: Financial Ledger
    conn.execute("CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id TEXT, address TEXT, amount REAL, status TEXT, timestamp REAL)")
    # Transactions: History
    conn.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id TEXT, amount REAL, timestamp REAL, description TEXT)")
    conn.commit(); conn.close()

def register_user(email, password):
    conn = sqlite3.connect(DB_FILE)
    try:
        if conn.execute("SELECT email FROM users WHERE email=?", (email,)).fetchone(): return None
        pw_hash = generate_password_hash(password)
        wallet_id = f"ELYS-{str(uuid.uuid4())[:8].upper()}"
        conn.execute("INSERT INTO users (email, password_hash, wallet_id, created_at) VALUES (?, ?, ?, ?)",
                     (email, pw_hash, wallet_id, time.time()))
        conn.commit(); return wallet_id
    finally: conn.close()

def verify_user(email, password):
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], password): return user
    return None

def update_worker_credit(wid, steps_inc, amount, public_key=None, hardware=None, owner_wallet_id=None):
    conn = sqlite3.connect(DB_FILE)
    try:
        if public_key:
            hw_str = json.dumps(hardware) if hardware else "{}"
            region = hardware.get("region", "global") if hardware else "global"
            if owner_wallet_id:
                conn.execute("""
                    INSERT INTO workers (worker_id, last_seen, balance, total_steps, public_key, hardware_specs, region, owner_wallet_id)
                    VALUES (?, ?, 0, 0, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen, public_key=excluded.public_key, hardware_specs=excluded.hardware_specs, region=excluded.region, owner_wallet_id=excluded.owner_wallet_id
                """, (wid, time.time(), public_key, hw_str, region, owner_wallet_id))
            else:
                conn.execute("""
                    INSERT INTO workers (worker_id, last_seen, balance, total_steps, public_key, hardware_specs, region)
                    VALUES (?, ?, 0, 0, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET last_seen=excluded.last_seen, public_key=excluded.public_key, hardware_specs=excluded.hardware_specs, region=excluded.region
                """, (wid, time.time(), public_key, hw_str, region))
        else:
            conn.execute("UPDATE workers SET last_seen=?, balance=balance+?, total_steps=total_steps+? WHERE worker_id=?",
                         (time.time(), amount, steps_inc, wid))
            if amount > 0:
                cur = conn.execute("SELECT owner_wallet_id FROM workers WHERE worker_id=?", (wid,))
                res = cur.fetchone()
                if res and res[0]:
                    conn.execute("UPDATE users SET balance=balance+? WHERE wallet_id=?", (amount, res[0]))
                    # Optional: Log small mining rewards? Maybe too verbose.
        conn.commit()
    finally: conn.close()

# --- ORCHESTRATOR ---
class Orchestrator:
    def __init__(self):
        self.topology_map = {}
        self.lock = threading.Lock()

    def schedule_job(self, job_spec):
        log_master("🧠 Orchestrator: Assigning Mission...")
        conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
        workers = conn.execute("SELECT * FROM workers WHERE last_seen > ?", (time.time() - 300,)).fetchall()
        conn.close()

        if not workers:
            log_master("❌ No active workers found.")
            return

        # Simple assignment for now
        with self.lock:
            for w in workers:
                self.topology_map[w['worker_id']] = job_spec
        log_master(f"✅ Mission assigned to {len(workers)} nodes.")

SCHEDULER = Orchestrator()

# --- HIVEMIND ---
def start_dht_service():
    global DHT
    log_master("🚀 DHT Bootstrap Online.")
    try:
        DHT = hivemind.DHT(start=True, host_maddrs=["/ip4/0.0.0.0/tcp/8001"])
        visible = DHT.get_visible_maddrs()
        CURRENT_MISSION["initial_peers"] = [str(a) for a in visible]
        threading.Thread(target=monitor_swarm_metrics, daemon=True).start()
    except Exception as e: log_master(f"❌ DHT Error: {e}")

def monitor_swarm_metrics():
    while True:
        if CURRENT_MISSION["status"] == "ACTIVE" and DHT:
            conn = sqlite3.connect(DB_FILE)
            workers = conn.execute("SELECT worker_id, public_key FROM workers").fetchall()
            conn.close()
            for wid, pub_key in workers:
                key = f"job_{CURRENT_MISSION['job_id']}_metrics_{wid}"
                entry = DHT.get(key, latest=True)
                if entry and entry.value:
                    try:
                        data = json.loads(entry.value)
                        sig = data['signature']
                        payload = data['payload']
                        payload_str = json.dumps(payload, sort_keys=True)
                        key_to_use = pub_key if pub_key else payload['public_key']
                        if elysium_crypto.verify_signature(key_to_use, payload_str, sig):
                            if time.time() - payload.get('timestamp', 0) < 30:
                                update_worker_credit(wid, 0, 0.0001, pub_key)
                    except: pass
        time.sleep(5)

# --- API ---
@app.route('/api/job/current')
def api_job_current(): return jsonify({"meta": CURRENT_MISSION})

@app.route('/api/wallet/balance/<wallet_id>')
def api_wallet(wallet_id):
    conn = sqlite3.connect(DB_FILE)
    res = conn.execute("SELECT balance FROM users WHERE wallet_id=?", (wallet_id,)).fetchone()
    conn.close()
    return jsonify({"wallet_id": wallet_id, "balance": res[0] if res else 0.0})

@app.route('/api/job/heartbeat', methods=['POST'])
def api_heartbeat():
    d = request.json
    if d.get('worker_id'):
        update_worker_credit(d['worker_id'], 0, 0.0001, d.get('public_key'), d.get('hardware'), d.get('wallet_id'))
    return jsonify({"status": "ack", "peers": CURRENT_MISSION["initial_peers"]})

@app.route('/api/job/config/<worker_id>')
def api_secure_config(worker_id):
    with SCHEDULER.lock:
        cfg = SCHEDULER.topology_map.get(worker_id)
    return jsonify({"status": "ASSIGNED", "config": cfg}) if cfg else jsonify({"status": "WAITING"})

@app.route('/api/wallet/withdraw', methods=['POST'])
def api_withdraw():
    if 'user_id' not in flask.session: return jsonify({"status": "error"}), 403
    addr = request.form.get("address")
    conn = sqlite3.connect(DB_FILE)
    try:
        user = conn.execute("SELECT * FROM users WHERE email=?", (flask.session['user_id'],)).fetchone()
        if user and user[3] >= 10.0: # Check balance >= 10
            # Deduct
            conn.execute("UPDATE users SET balance = balance - ? WHERE email=?", (user[3], user[0]))
            # Log
            conn.execute("INSERT INTO withdrawals (wallet_id, address, amount, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                         (user[2], addr, user[3], 'PENDING', time.time()))
            conn.commit()
            return jsonify({"status": "ok", "message": "Withdrawal Pending"})
        return jsonify({"status": "error", "message": "Insufficient Funds"}), 400
    finally: conn.close()

# --- AUTH & JOB START ---
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    user = verify_user(request.form.get('email'), request.form.get('password'))
    if user:
        flask.session['user_id'] = user['email']
        flask.session['wallet_id'] = user['wallet_id']
        return jsonify({"status": "ok", "redirect": "/dashboard"})
    return jsonify({"status": "error"}), 401

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    wid = register_user(request.form.get('email'), request.form.get('password'))
    if wid:
        flask.session['user_id'] = request.form.get('email')
        flask.session['wallet_id'] = wid
        return jsonify({"status": "ok", "redirect": "/dashboard"})
    return jsonify({"status": "error"}), 400

@app.route('/api/auth/logout')
def api_logout():
    flask.session.clear(); return flask.redirect('/')

@app.route('/api/job/start', methods=['POST'])
def api_job_start():
    # Supports both Form (UI) and JSON (SDK) submission
    if request.is_json:
        # SDK Path
        # No session check for MVP SDK usage (or assume API Key later)
        spec = request.json
        job_name = spec.get("TrainingJobName", f"job-{int(time.time())}")
    else:
        # UI Path (Form)
        if 'user_id' not in flask.session: return jsonify({"status": "forbidden"}), 403

        # Construct Spec from Form
        model = request.form.get("model", "gpt2")
        mode = request.form.get("mode", "data_parallel")
        # Validate HF
        try:
            transformers.AutoConfig.from_pretrained(model)
        except Exception as e: return f"Invalid Model: {e}", 400

        job_name = f"JOB_{int(time.time())}"
        spec = {
            "TrainingJobName": job_name,
            "AlgorithmSpecification": {
                "Framework": "pytorch",
                "ContainerEntrypoint": ["train.py"] # Default
            },
            "HyperParameters": {"mode": mode, "model_name": model},
            "InputDataConfig": {
                "train": {"DataSource": {"S3DataSource": {"S3Uri": f"s3://mock-bucket/{uuid.uuid4()}"}}}
            },
            "OutputDataConfig": {"S3OutputPath": "s3://elysium-output"}
        }

    CURRENT_MISSION["job_id"] = job_name
    CURRENT_MISSION["status"] = "ACTIVE"
    CURRENT_MISSION["spec"] = spec

    log_master(f"🚀 Job Launched: {job_name}")
    SCHEDULER.schedule_job(spec)

    if request.is_json: return jsonify({"status": "ok", "job_id": job_name})
    return flask.redirect('/dashboard')

# --- UI ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in flask.session:
        return render_template_string("""
<!DOCTYPE html><html><head><title>Elysium Cloud</title><style>body{background:#0f172a;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.card{background:#1e293b;padding:40px;border-radius:12px;width:300px}input,button{width:100%;padding:10px;margin:5px 0;border-radius:5px;border:none}button{background:#2563eb;color:#fff;font-weight:bold;cursor:pointer}</style></head><body><div class="card"><h2 style="text-align:center">Elysium</h2><form onsubmit="event.preventDefault(); fetch('/api/auth/login', {method:'POST', body:new FormData(this)}).then(r=>r.json()).then(d=>{if(d.status=='ok')location.href=d.redirect; else alert('Error')})"><input name="email" placeholder="Email"><input type="password" name="password" placeholder="Password"><button>Login</button></form><div style="text-align:center;margin-top:10px;font-size:12px;cursor:pointer" onclick="fetch('/api/auth/signup', {method:'POST', body:new FormData(document.querySelector('form'))}).then(r=>r.json()).then(d=>{if(d.status=='ok')location.href=d.redirect})">No account? Sign Up</div></div></body></html>""")
    return flask.redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in flask.session: return flask.redirect('/')

    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE email=?", (flask.session['user_id'],)).fetchone()
    workers = conn.execute("SELECT * FROM workers WHERE owner_wallet_id=?", (user['wallet_id'],)).fetchall()
    txs = conn.execute("SELECT * FROM withdrawals WHERE wallet_id=? ORDER BY timestamp DESC", (user['wallet_id'],)).fetchall()
    conn.close()

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Elysium Mission Control</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --primary: #2563eb; --success: #22c55e; --bg: #0f172a; --surface: #1e293b; --text: #f1f5f9; --border: #334155; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin:0; display: flex; height: 100vh; overflow: hidden; }
        .sidebar { width: 250px; background: #020617; border-right: 1px solid var(--border); padding: 20px; display:flex; flex-direction:column; }
        .nav-item { padding: 12px; margin: 5px 0; border-radius: 8px; color: #94a3b8; cursor: pointer; transition: 0.2s; font-weight: 500; }
        .nav-item:hover, .nav-item.active { background: rgba(37, 99, 235, 0.1); color: var(--primary); }
        .main { flex: 1; padding: 30px; overflow-y: auto; }
        .page { display: none; } .page.active { display: block; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; position:relative; margin-bottom: 20px; }
        .metric-val { font-size: 32px; font-weight: 700; margin: 10px 0 5px; }
        .metric-label { font-size: 13px; color: #94a3b8; font-weight: 500; text-transform: uppercase; }
        .btn { background: var(--primary); color: #fff; border: none; padding: 10px 20px; border-radius: 6px; font-weight: 600; cursor: pointer; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; color: #94a3b8; font-size: 12px; padding: 15px; border-bottom: 1px solid var(--border); }
        td { padding: 15px; border-bottom: 1px solid var(--border); font-size: 14px; }
    </style>
    <script>
        function show(id) {
            document.querySelectorAll('.page').forEach(e => e.classList.remove('active'));
            document.getElementById('p-'+id).classList.add('active');
        }
    </script>
</head>
<body>
    <div class="sidebar">
        <h2 style="color:#fff; margin-bottom:30px">ELYSIUM</h2>
        <div class="nav-item active" onclick="show('overview')">📊 Overview</div>
        <div class="nav-item" onclick="show('payouts')">💸 Payouts</div>
        <div class="nav-item" onclick="show('fleet')">💻 Fleet</div>
        <div style="margin-top:auto"><a href="/api/auth/logout" style="color:#ef4444;text-decoration:none">Sign Out</a></div>
    </div>

    <div class="main">
        <div id="p-overview" class="page active">
            <div style="display:flex; justify-content:space-between; margin-bottom:20px">
                <h1>Mission Control</h1>
                <button class="btn" onclick="document.getElementById('modal').style.display='flex'">+ NEW MISSION</button>
            </div>

            <div class="card" style="display:flex; gap:20px">
                <div style="flex:1">
                    <div class="metric-label">Operating Balance</div>
                    <div class="metric-val" style="color:var(--success)">$ {{ "%.2f"|format(user.balance) }}</div>
                </div>
                <div style="flex:1">
                    <div class="metric-label">Active Nodes</div>
                    <div class="metric-val">{{ workers|length }}</div>
                </div>
            </div>
        </div>

        <div id="p-payouts" class="page">
            <h1>Payouts & Withdrawals</h1>
            <div class="card">
                <h3>Withdrawal History</h3>
                <table>
                    <thead><tr><th>ID</th><th>Address</th><th>Amount</th><th>Status</th></tr></thead>
                    <tbody>
                        {% for t in txs %}
                        <tr>
                            <td>#{{ t[0] }}</td>
                            <td style="font-family:monospace">{{ t[2] }}</td>
                            <td style="color:var(--success)">${{ "%.2f"|format(t[3]) }}</td>
                            <td>{{ t[4] }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <div id="p-fleet" class="page">
            <h1>Compute Fleet</h1>
            <div class="card">
                <table>
                    <thead><tr><th>ID</th><th>Region</th><th>Hardware</th><th>Last Seen</th></tr></thead>
                    <tbody>
                        {% for w in workers %}
                        <tr>
                            <td>{{ w[0] }}</td>
                            <td>{{ w[6] }}</td>
                            <td>{{ (w[5]|string|from_json).get('gpu_name', 'CPU') }}</td>
                            <td>{{ "%.0f"|format(time.time() - w[1]) }}s ago</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <div id="modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); justify-content:center; align-items:center;">
            <div class="card" style="width:500px">
                <h2>Launch New Mission</h2>
                <form action="/api/job/start" method="POST">
                    <label>Source Type</label>
                    <select name="source_type" style="width:100%; padding:10px; background:#0f172a; color:#fff; border:1px solid #334155"><option value="hf">HuggingFace</option><option value="s3">S3 / MinIO</option></select>
                    <label>Model ID</label>
                    <input name="model" placeholder="e.g. meta-llama/Llama-2-7b" style="background:#0f172a; color:#fff; border:1px solid #334155" required>
                    <label>Mode</label>
                    <select name="mode" style="width:100%; padding:10px; background:#0f172a; color:#fff; border:1px solid #334155"><option value="data_parallel">Data Parallel</option><option value="model_parallel">Model Parallel</option></select>
                    <div style="display:flex; gap:10px; margin-top:20px">
                        <button type="button" onclick="document.getElementById('modal').style.display='none'" style="background:transparent; border:1px solid #334155">CANCEL</button>
                        <button type="submit">LAUNCH</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
</body>
</html>
""", user=user, workers=workers, txs=txs, time=time, from_json=json.loads)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_dht_service, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
