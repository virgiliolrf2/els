import sys, os, subprocess, time, threading, sqlite3, json, math, uuid
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
import flask
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import elysium_crypto
import elysium_bank
import elysium_security

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

# --- DESIGN ASSETS ---
LOGO_SVG = """
<svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
<path d="M12 2L2 12L12 22L22 12L12 2Z" fill="#10B981" stroke="#059669" stroke-width="2" stroke-linejoin="round"/>
<path d="M12 6L6 12L12 18L18 12L12 6Z" fill="#D1FAE5" stroke="#10B981" stroke-width="1.5" stroke-linejoin="round"/>
</svg>
"""

# --- CONFIG ---
DB_FILE = "elysium_ledger_v4.db"
STORAGE_DIR = Path("./elysium_storage")
for p in [STORAGE_DIR]: p.mkdir(parents=True, exist_ok=True)

# Generate Payment Keys on Startup
if not os.path.exists("master_payment_private.pem"):
    print("[MASTER] 🔑 Generating Payment Keys...")
    elysium_security.generate_master_keys()

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
    # Withdrawals: Financial Ledger - Added tx_hash
    conn.execute("CREATE TABLE IF NOT EXISTS withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id TEXT, address TEXT, amount REAL, status TEXT, timestamp REAL, tx_hash TEXT)")
    # Transactions: History
    conn.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id TEXT, amount REAL, timestamp REAL, description TEXT)")
    conn.commit(); conn.close()

def register_user(email, password, wallet_id=None):
    conn = sqlite3.connect(DB_FILE)
    try:
        if conn.execute("SELECT email FROM users WHERE email=?", (email,)).fetchone(): return None
        pw_hash = generate_password_hash(password)
        if not wallet_id:
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
BANK = elysium_bank.ElysiumBank(DB_FILE, real_money=False) # Start in Simulation Mode

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
    # Check User Account
    res = conn.execute("SELECT balance FROM users WHERE wallet_id=?", (wallet_id,)).fetchone()
    if res:
        bal = res[0]
    else:
        # Check Standalone Worker Aggregation
        # Sum balances of all workers owned by this wallet
        res = conn.execute("SELECT SUM(balance) FROM workers WHERE owner_wallet_id=?", (wallet_id,)).fetchone()
        bal = res[0] if res[0] else 0.0

    conn.close()
    return jsonify({"wallet_id": wallet_id, "balance": bal})

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

@app.route('/api/config/public_key')
def api_public_key():
    if not os.path.exists("master_payment_public.pem"):
        # Auto-heal: Regenerate keys if missing
        log_master("⚠️ Public Key missing. Regenerating...")
        elysium_security.generate_master_keys()

    if os.path.exists("master_payment_public.pem"):
        return flask.send_file("master_payment_public.pem")
    return "Not Found", 404

@app.route('/api/wallet/withdraw', methods=['POST'])
def api_withdraw():
    addr = request.form.get("address")
    wallet_id = request.form.get("wallet_id")

    # 1. Authenticated User (Session)
    if 'user_id' in flask.session:
        conn = sqlite3.connect(DB_FILE)
        try:
            user = conn.execute("SELECT * FROM users WHERE email=?", (flask.session['user_id'],)).fetchone()
            if user and user[3] >= 10.0:
                conn.execute("UPDATE users SET balance = balance - ? WHERE email=?", (user[3], user[0]))
                conn.execute("INSERT INTO withdrawals (wallet_id, address, amount, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                             (user[2], addr, user[3], 'PENDING', time.time()))
                conn.commit()
                return jsonify({"status": "ok", "message": "Withdrawal Pending"})
        finally: conn.close()

    # 2. Standalone Worker (App Request)
    elif wallet_id:
        conn = sqlite3.connect(DB_FILE)
        try:
            # Check for Encrypted Wallet ID
            real_dest = addr
            if wallet_id.startswith("ELYS-SECure-"):
                try:
                    info = elysium_security.decrypt_payment_info(wallet_id, "master_payment_private.pem")
                    log_master(f"🔓 Decrypted Payment Info: {info['type']} -> {info['account']}")
                    # For MVP, we still record the public 'addr' request in DB but log the real dest internally
                    # In real prod, 'addr' in DB should be the decrypted one or kept encrypted
                except Exception as e:
                    log_master(f"❌ Decryption Failed: {e}")
                    return jsonify({"status": "error", "message": "Invalid Secure Wallet ID"}), 400

            # Sum Balance
            res = conn.execute("SELECT SUM(balance) FROM workers WHERE owner_wallet_id=?", (wallet_id,)).fetchone()
            total = res[0] if res[0] else 0.0

            if total >= 10.0:
                # Deduct from all workers proportionally or reset to 0
                conn.execute("UPDATE workers SET balance = 0 WHERE owner_wallet_id=?", (wallet_id,))
                conn.execute("INSERT INTO withdrawals (wallet_id, address, amount, status, timestamp) VALUES (?, ?, ?, ?, ?)",
                             (wallet_id, addr, total, 'PENDING', time.time()))
                conn.commit()
                return jsonify({"status": "ok", "message": "Withdrawal Pending"})
        finally: conn.close()

    return jsonify({"status": "error", "message": "Insufficient Funds or Invalid Auth"}), 400

@app.route('/api/admin/payout', methods=['POST'])
def api_admin_payout():
    # In real prod, add Auth check here (admin only)
    count = BANK.process_pending_withdrawals()
    return jsonify({"status": "ok", "processed": count})

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
    wid = register_user(request.form.get('email'), request.form.get('password'), request.form.get('wallet_id'))
    if wid:
        flask.session['user_id'] = request.form.get('email')
        flask.session['wallet_id'] = wid
        return jsonify({"status": "ok", "redirect": "/dashboard"})
    return jsonify({"status": "error"}), 400

@app.route('/api/auth/logout')
def api_logout():
    flask.session.clear(); return flask.redirect('/')

@app.route('/api/storage/jobs/<job_id>/source.tar.gz')
def serve_source(job_id):
    path = STORAGE_DIR / "jobs" / job_id / "source.tar.gz"
    if path.exists():
        return flask.send_file(path)
    return "Not Found", 404

@app.route('/api/job/start', methods=['POST'])
def api_job_start():
    # Handle Multipart/Form-Data (SDK with File)
    if 'spec' in request.form:
        spec = json.loads(request.form['spec'])
        job_name = spec.get("TrainingJobName", f"job-{int(time.time())}")

        # Handle Code Upload
        if 'code' in request.files:
            f = request.files['code']
            save_dir = STORAGE_DIR / "jobs" / job_name
            save_dir.mkdir(parents=True, exist_ok=True)
            f.save(save_dir / "source.tar.gz")
            spec['CodeUrl'] = f"/api/storage/jobs/{job_name}/source.tar.gz"

    # Handle Raw JSON (Legacy SDK)
    elif request.is_json:
        spec = request.json
        job_name = spec.get("TrainingJobName", f"job-{int(time.time())}")

    # Handle UI Form Submission
    else:
        if 'user_id' not in flask.session: return jsonify({"status": "forbidden"}), 403
        model = request.form.get("model", "gpt2")
        mode = request.form.get("mode", "data_parallel")
        # Validate HF
        try: transformers.AutoConfig.from_pretrained(model)
        except Exception as e: return f"Invalid Model: {e}", 400

        job_name = f"JOB_{int(time.time())}"
        spec = {
            "TrainingJobName": job_name,
            "AlgorithmSpecification": {
                "Framework": "pytorch",
                "ContainerEntrypoint": ["train.py"]
            },
            "HyperParameters": {"mode": mode, "model_name": model},
            "InputDataConfig": {
                "train": {"DataSource": {"Uri": f"hf://mock-bucket/{uuid.uuid4()}"}}
            },
            "OutputDataConfig": {"OutputPath": "/tmp/output"}
        }

    CURRENT_MISSION["job_id"] = job_name
    CURRENT_MISSION["status"] = "ACTIVE"
    CURRENT_MISSION["spec"] = spec

    log_master(f"🚀 Job Launched: {job_name}")
    SCHEDULER.schedule_job(spec)

    if request.is_json or 'spec' in request.form:
        return jsonify({"status": "ok", "job_id": job_name})
    return flask.redirect('/dashboard')

# --- UI ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in flask.session:
        return render_template_string("""
<!DOCTYPE html>
<html lang="en" class="h-full bg-slate-50">
<head>
    <meta charset="UTF-8">
    <title>Elysium Cloud | Sign In</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style> body { font-family: 'Inter', sans-serif; } </style>
</head>
<body class="h-full flex items-center justify-center">
    <div class="bg-white p-8 rounded-2xl shadow-xl border border-slate-100 w-96">
        <div class="flex flex-col items-center mb-6">
            <div class="w-12 h-12 mb-2">{{ logo|safe }}</div>
            <h2 class="text-2xl font-bold text-slate-900 tracking-tight">Elysium Cloud</h2>
            <p class="text-sm text-slate-500">Enterprise Infrastructure Control</p>
        </div>

        <form onsubmit="event.preventDefault(); submitForm(this)" class="space-y-4">
            <div>
                <label class="block text-xs font-medium text-slate-500 uppercase mb-1">Email</label>
                <input name="email" type="email" class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:bg-white transition" placeholder="name@company.com" required>
            </div>
            <div>
                <label class="block text-xs font-medium text-slate-500 uppercase mb-1">Password</label>
                <input name="password" type="password" class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-emerald-500 focus:bg-white transition" placeholder="••••••••" required>
            </div>
            <button type="submit" class="w-full py-2.5 bg-emerald-500 hover:bg-emerald-600 text-white font-semibold rounded-lg shadow-sm transition">Access Console</button>
        </form>

        <div class="mt-6 text-center">
            <p class="text-xs text-slate-400 cursor-pointer hover:text-emerald-600" onclick="toggleMode()">Don't have an account? Create one</p>
        </div>
    </div>

    <script>
        let isSignup = false;
        const logo = `{{ logo|safe }}`;

        function toggleMode() {
            isSignup = !isSignup;
            const btn = document.querySelector('button');
            const link = document.querySelector('p.text-xs');
            if(isSignup) {
                btn.innerText = "Create Workspace";
                link.innerText = "Already have an account? Sign In";
            } else {
                btn.innerText = "Access Console";
                link.innerText = "Don't have an account? Create one";
            }
        }

        function submitForm(form) {
            const endpoint = isSignup ? '/api/auth/signup' : '/api/auth/login';
            fetch(endpoint, {method:'POST', body:new FormData(form)})
            .then(r=>r.json())
            .then(d=>{
                if(d.status=='ok') location.href = d.redirect;
                else alert(d.message || "Authentication Failed");
            });
        }
    </script>
</body>
</html>""", logo=LOGO_SVG)
    return flask.redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in flask.session: return flask.redirect('/')

    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE email=?", (flask.session['user_id'],)).fetchone()
    workers = conn.execute("SELECT * FROM workers WHERE owner_wallet_id=?", (user['wallet_id'],)).fetchall()
    txs = conn.execute("SELECT * FROM withdrawals WHERE wallet_id=? ORDER BY timestamp DESC", (user['wallet_id'],)).fetchall()
    conn.close()

    # Calculate Metrics
    active_nodes = len([w for w in workers if time.time() - w['last_seen'] < 60])
    hashrate = sum([json.loads(w['hardware_specs']).get('compute_score', 0) for w in workers])
    payouts = sum([t['amount'] for t in txs if t['status']=='PAID'])

    return render_template_string("""
<!DOCTYPE html>
<html lang="en" class="bg-slate-50">
<head>
    <meta charset="UTF-8">
    <title>Elysium Console</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="//unpkg.com/alpinejs" defer></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style> body { font-family: 'Inter', sans-serif; } </style>
</head>
<body class="flex h-screen overflow-hidden" x-data="{ page: 'dashboard' }">

    <!-- SIDEBAR -->
    <aside class="w-64 bg-white border-r border-slate-200 flex flex-col">
        <div class="h-16 flex items-center px-6 border-b border-slate-100">
            <div class="w-8 h-8 mr-3">{{ logo|safe }}</div>
            <span class="font-bold text-slate-800 tracking-tight">ELYSIUM</span>
        </div>

        <nav class="flex-1 p-4 space-y-1">
            <a @click="page='dashboard'" :class="page==='dashboard' ? 'bg-emerald-50 text-emerald-700' : 'text-slate-600 hover:bg-slate-50'" class="flex items-center px-3 py-2.5 rounded-lg text-sm font-medium cursor-pointer transition">
                <span class="mr-3">📊</span> Dashboard
            </a>
            <a @click="page='instances'" :class="page==='instances' ? 'bg-emerald-50 text-emerald-700' : 'text-slate-600 hover:bg-slate-50'" class="flex items-center px-3 py-2.5 rounded-lg text-sm font-medium cursor-pointer transition">
                <span class="mr-3">🖥️</span> Instances
            </a>
            <a @click="page='payouts'" :class="page==='payouts' ? 'bg-emerald-50 text-emerald-700' : 'text-slate-600 hover:bg-slate-50'" class="flex items-center px-3 py-2.5 rounded-lg text-sm font-medium cursor-pointer transition">
                <span class="mr-3">💳</span> Billing & Costs
            </a>
        </nav>

        <div class="p-4 border-t border-slate-100">
            <div class="text-xs font-semibold text-slate-400 uppercase mb-2">Workspace</div>
            <div class="flex items-center mb-3">
                <div class="w-8 h-8 rounded-full bg-slate-200 flex items-center justify-center text-xs font-bold text-slate-600 mr-2">
                    {{ user.email[0]|upper }}
                </div>
                <div class="overflow-hidden">
                    <p class="text-sm font-medium text-slate-700 truncate">{{ user.email }}</p>
                    <p class="text-xs text-slate-400 truncate">ID: {{ user.wallet_id }}</p>
                </div>
            </div>
            <a href="/api/auth/logout" class="block text-center text-xs text-red-500 hover:text-red-700 font-medium">Sign Out</a>
        </div>
    </aside>

    <!-- MAIN CONTENT -->
    <main class="flex-1 flex flex-col relative">
        <!-- GLOBAL HEADER -->
        <header class="h-16 bg-white border-b border-slate-200 flex items-center justify-between px-8">
            <div class="flex items-center bg-slate-100 rounded-md px-3 py-1.5 w-96">
                <span class="text-slate-400 text-sm mr-2">🔍</span>
                <input class="bg-transparent border-none focus:outline-none text-sm w-full text-slate-600" placeholder="Search resources, jobs, or docs...">
            </div>
            <div class="flex items-center gap-4">
                <div class="flex items-center px-3 py-1 bg-emerald-50 text-emerald-700 rounded-full text-xs font-bold border border-emerald-100">
                    <span class="w-2 h-2 rounded-full bg-emerald-500 mr-2 animate-pulse"></span>
                    US-East-1
                </div>
                <button class="bg-slate-900 hover:bg-slate-800 text-white text-sm font-medium px-4 py-2 rounded-lg transition shadow-sm" onclick="document.getElementById('modal').showModal()">
                    + Launch Training Job
                </button>
            </div>
        </header>

        <!-- DASHBOARD VIEW -->
        <div class="flex-1 overflow-y-auto p-8 bg-slate-50" x-show="page==='dashboard'">
            <div class="mb-8">
                <h1 class="text-2xl font-bold text-slate-900">Platform Overview</h1>
                <p class="text-slate-500">Real-time infrastructure telemetry.</p>
            </div>

            <!-- HERO METRICS -->
            <div class="grid grid-cols-4 gap-6 mb-8">
                <div class="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-1">Active Nodes</p>
                    <p class="text-3xl font-bold text-slate-900">{{ active_nodes }}</p>
                </div>
                <div class="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-1">Total Hashrate (Est.)</p>
                    <p class="text-3xl font-bold text-slate-900">{{ "%.0f"|format(hashrate) }} <span class="text-lg text-slate-400 font-normal">TFLOPS</span></p>
                </div>
                <div class="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-1">Operating Balance</p>
                    <p class="text-3xl font-bold text-emerald-600">${{ "%.2f"|format(user.balance) }}</p>
                </div>
                <div class="bg-white p-6 rounded-xl border border-slate-200 shadow-sm">
                    <p class="text-xs font-bold text-slate-400 uppercase tracking-wider mb-1">Total Payouts</p>
                    <p class="text-3xl font-bold text-slate-900">${{ "%.2f"|format(payouts) }}</p>
                </div>
            </div>

            <!-- INSTANCE TABLE -->
            <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
                <div class="px-6 py-4 border-b border-slate-100 flex justify-between items-center">
                    <h3 class="font-bold text-slate-800">Instance List</h3>
                    <span class="text-xs font-medium text-slate-500 bg-slate-100 px-2 py-1 rounded">{{ workers|length }} Total</span>
                </div>
                <table class="w-full text-left">
                    <thead class="bg-slate-50 text-slate-500 text-xs uppercase font-semibold">
                        <tr>
                            <th class="px-6 py-3">Instance ID</th>
                            <th class="px-6 py-3">Status</th>
                            <th class="px-6 py-3">Hardware Type</th>
                            <th class="px-6 py-3">Region</th>
                            <th class="px-6 py-3">Last Seen</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {% for w in workers %}
                        <tr class="hover:bg-slate-50 transition">
                            <td class="px-6 py-4 font-mono text-sm text-slate-700">{{ w.worker_id }}</td>
                            <td class="px-6 py-4">
                                <span class="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-800">
                                    <span class="w-1.5 h-1.5 rounded-full bg-emerald-500 mr-1.5"></span> Running
                                </span>
                            </td>
                            <td class="px-6 py-4 text-sm text-slate-600">{{ (w.hardware_specs|string|from_json).get('gpu_name', 'CPU Instance') }}</td>
                            <td class="px-6 py-4 text-sm text-slate-600">{{ w.region }}</td>
                            <td class="px-6 py-4 text-sm text-slate-400">{{ "%.0f"|format(time.time() - w.last_seen) }}s ago</td>
                        </tr>
                        {% else %}
                        <tr><td colspan="5" class="px-6 py-8 text-center text-slate-400 text-sm">No instances provisioned. Run a worker node to see it here.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- PAYOUTS VIEW -->
        <div class="flex-1 overflow-y-auto p-8 bg-slate-50" x-show="page==='payouts'">
            <div class="mb-8 flex justify-between items-center">
                <div>
                    <h1 class="text-2xl font-bold text-slate-900">Billing & Payouts</h1>
                    <p class="text-slate-500">Manage operating costs and worker compensation.</p>
                </div>
                <button class="text-sm font-bold text-emerald-600 hover:text-emerald-800" onclick="fetch('/api/admin/payout', {method:'POST'}).then(r=>r.json()).then(d=>alert('Processed: '+d.processed))">Process Pending Batches</button>
            </div>

            <div class="bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden">
                <table class="w-full text-left">
                    <thead class="bg-slate-50 text-slate-500 text-xs uppercase font-semibold">
                        <tr>
                            <th class="px-6 py-3">Transaction ID</th>
                            <th class="px-6 py-3">Date</th>
                            <th class="px-6 py-3">Destination</th>
                            <th class="px-6 py-3">Amount</th>
                            <th class="px-6 py-3">Status</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {% for t in txs %}
                        <tr>
                            <td class="px-6 py-4 text-sm text-slate-500">#{{ t.id }}</td>
                            <td class="px-6 py-4 text-sm text-slate-700">{{ time.ctime(t.timestamp) }}</td>
                            <td class="px-6 py-4 text-sm font-mono text-slate-500">{{ t.address }}</td>
                            <td class="px-6 py-4 text-sm font-bold text-slate-900">${{ "%.2f"|format(t.amount) }}</td>
                            <td class="px-6 py-4">
                                <span class="text-xs font-bold px-2 py-1 rounded {{ 'bg-green-100 text-green-700' if t.status=='PAID' else 'bg-yellow-100 text-yellow-700' }}">
                                    {{ t.status }}
                                </span>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

    </main>

    <!-- MODAL -->
    <dialog id="modal" class="rounded-xl shadow-2xl p-0 w-[600px] backdrop:bg-slate-900/50">
        <div class="bg-white p-6">
            <h3 class="text-xl font-bold text-slate-900 mb-1">Launch Training Job</h3>
            <p class="text-sm text-slate-500 mb-6">Configure your distributed training mission.</p>

            <form action="/api/job/start" method="POST" class="space-y-4">
                <div class="grid grid-cols-2 gap-4">
                    <div>
                        <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Source Type</label>
                        <select name="source_type" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
                            <option value="hf">HuggingFace Hub</option>
                            <option value="s3">S3 / MinIO</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Training Mode</label>
                        <select name="mode" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm">
                            <option value="data_parallel">Data Parallel</option>
                            <option value="model_parallel">Model Parallel</option>
                        </select>
                    </div>
                </div>

                <div>
                    <label class="block text-xs font-bold text-slate-500 uppercase mb-1">Model Path / ID</label>
                    <input name="model" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono" placeholder="meta-llama/Llama-2-7b" required>
                </div>

                <div>
                    <label class="block text-xs font-bold text-slate-500 uppercase mb-1">JSON Configuration</label>
                    <textarea name="config_json" rows="3" class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm font-mono text-slate-600" placeholder='{"lr": 2e-5, "optimizer": "adamw"}'></textarea>
                </div>

                <div class="flex justify-end gap-3 mt-6 pt-4 border-t border-slate-100">
                    <button type="button" class="px-4 py-2 text-sm font-semibold text-slate-500 hover:text-slate-700" onclick="document.getElementById('modal').close()">Cancel</button>
                    <button type="submit" class="px-4 py-2 text-sm font-bold text-white bg-emerald-500 hover:bg-emerald-600 rounded-lg shadow-sm">Launch Mission</button>
                </div>
            </form>
        </div>
    </dialog>

</body>
</html>
""", user=user, workers=workers, txs=txs, time=time, from_json=json.loads, logo=LOGO_SVG, active_nodes=active_nodes, hashrate=hashrate, payouts=payouts)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_dht_service, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
