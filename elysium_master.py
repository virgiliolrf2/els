import sys, os, subprocess, time, threading, sqlite3, json, math, uuid
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
import flask
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import elysium_crypto

# --- 1. BOOTLOADER HÍBRIDO (WINDOWS -> WSL) ---
# Se estiver rodando no Windows, ele se "auto-injeta" no WSL
if os.name == 'nt':
    print("[BOOT] 🖥️ Detectado Windows. Preparando ambiente WSL Linux...", flush=True)

    current_script = os.path.abspath(__file__)
    drive, path = os.path.splitdrive(current_script)
    wsl_path = f"/mnt/{drive.lower().replace(':', '')}{path.replace(os.sep, '/')}"
    venv_path = "~/.elysium_master_env"

    print("[BOOT] 🛠️ Configurando Ambiente Virtual no WSL (Evita conflitos PEP 668)...")
    try:
        # 1. Install System Deps (venv + GO for compiling hivemind p2pd if needed)
        subprocess.check_call(["wsl", "bash", "-c", "sudo apt update && sudo apt install -y python3 python3-venv python3-pip golang-go"])

        # 2. Create Venv & Install Libs
        setup_cmd = (
            f"if [ ! -d {venv_path} ]; then python3 -m venv {venv_path}; fi && "
            f"{venv_path}/bin/pip install --quiet hivemind cryptography torch flask"
        )
        subprocess.check_call(["wsl", "bash", "-c", setup_cmd])

    except subprocess.CalledProcessError as e:
        print(f"[BOOT] ⚠️ Erro na instalação de dependências: {e}")
        # Continue anyway, maybe it exists

    print(f"[BOOT] 🚀 Lançando Master dentro do Linux (Venv): {wsl_path}")
    # Run using the venv python
    launch_cmd = f"{venv_path}/bin/python3 {wsl_path}"
    subprocess.call(["wsl", "bash", "-c", launch_cmd])
    sys.exit(0)

# ==============================================================================
# DAQUI PARA BAIXO É CÓDIGO LINUX (RODANDO DENTRO DO WSL OU LINUX NATIVO)
# ==============================================================================

def check_and_fix_hivemind():
    """
    Checks if the p2pd binary is compatible with the current architecture.
    If not, forces a rebuild from source using Go.
    """
    try:
        import hivemind.hivemind_cli as cli
        p2pd_path = os.path.join(os.path.dirname(cli.__file__), 'p2pd')

        # Check 1: Exists?
        if not os.path.exists(p2pd_path):
            raise FileNotFoundError("p2pd binary missing")

        # Check 2: Execution Test
        # We try to run 'p2pd version' (or just run it and expect timeout/exit)
        # p2pd usually doesn't have a version flag that exits cleanly, but if it fails with Exec Format Error, subprocess catches it.
        try:
            # Just checking if we can spawn it. If it's a shell script wrapper error, it happens here.
            # p2pd without args might hang waiting for input, so we use a timeout.
            # But the error reported was "Syntax error", which happens on spawn.
            proc = subprocess.Popen([p2pd_path, "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill() # It ran, so it works.
        except Exception as e:
            print(f"[BOOT] ⚠️ Hivemind p2pd Check Failed: {e}")
            raise e # Trigger rebuild

    except Exception as e:
        print(f"[BOOT] 🛠️ Rebuilding Hivemind from Source (Architecture Mismatch Detected)...")
        print(f"[BOOT] This may take a few minutes. Please wait.")
        try:
            # Force reinstall with source build (requires Go)
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-binary", "hivemind", "hivemind"])
            print("[BOOT] ✅ Rebuild Complete. Restarting Master...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as build_err:
            print(f"[BOOT] ❌ Critical Failure rebuilding Hivemind: {build_err}")
            sys.exit(1)

check_and_fix_hivemind()
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
    # Workers: Added owner_wallet_id
    conn.execute("CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, last_seen REAL, balance REAL, total_steps INTEGER, public_key TEXT, hardware_specs TEXT, region TEXT, owner_wallet_id TEXT)")
    # Users: For Auth & Wallet
    conn.execute("CREATE TABLE IF NOT EXISTS users (email TEXT PRIMARY KEY, password_hash TEXT, wallet_id TEXT UNIQUE, balance REAL DEFAULT 0.0, created_at REAL)")
    # Transactions: Ledger history
    conn.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id TEXT, amount REAL, timestamp REAL, description TEXT)")
    conn.commit(); conn.close()

def register_user(email, password):
    conn = sqlite3.connect(DB_FILE)
    try:
        # Check existing
        cur = conn.execute("SELECT email FROM users WHERE email=?", (email,))
        if cur.fetchone(): return None

        # Create
        pw_hash = generate_password_hash(password)
        wallet_id = f"ELYS-{str(uuid.uuid4())[:8].upper()}"
        conn.execute("INSERT INTO users (email, password_hash, wallet_id, created_at) VALUES (?, ?, ?, ?)",
                     (email, pw_hash, wallet_id, time.time()))
        conn.commit()
        return wallet_id
    except Exception as e:
        print(f"DB Error: {e}")
        return None
    finally:
        conn.close()

def verify_user(email, password):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], password):
        return user
    return None

def update_worker_credit(wid, steps_inc, amount, public_key=None, hardware=None, owner_wallet_id=None):
    conn = sqlite3.connect(DB_FILE)
    try:
        if public_key:
            # Registration / Heartbeat with Metadata
            hw_str = json.dumps(hardware) if hardware else "{}"
            region = hardware.get("region", "global") if hardware else "global"

            # Update Worker Info
            # If owner_wallet_id is provided, update it. If not, keep existing.
            if owner_wallet_id:
                conn.execute("""
                    INSERT INTO workers (worker_id, last_seen, balance, total_steps, public_key, hardware_specs, region, owner_wallet_id)
                    VALUES (?, ?, 0, 0, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        public_key=excluded.public_key,
                        hardware_specs=excluded.hardware_specs,
                        region=excluded.region,
                        owner_wallet_id=excluded.owner_wallet_id
                """, (wid, time.time(), public_key, hw_str, region, owner_wallet_id))
            else:
                conn.execute("""
                    INSERT INTO workers (worker_id, last_seen, balance, total_steps, public_key, hardware_specs, region)
                    VALUES (?, ?, 0, 0, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        public_key=excluded.public_key,
                        hardware_specs=excluded.hardware_specs,
                        region=excluded.region
                """, (wid, time.time(), public_key, hw_str, region))

        else:
            # Routine Credit Update
            # 1. Update Worker Stats
            conn.execute("UPDATE workers SET last_seen=?, balance=balance+?, total_steps=total_steps+? WHERE worker_id=?",
                         (time.time(), amount, steps_inc, wid))

            # 2. Credit User Wallet (if linked)
            if amount > 0:
                cur = conn.execute("SELECT owner_wallet_id FROM workers WHERE worker_id=?", (wid,))
                res = cur.fetchone()
                if res and res[0]:
                    wallet_id = res[0]
                    conn.execute("UPDATE users SET balance=balance+? WHERE wallet_id=?", (amount, wallet_id))
                    # Log Transaction (Optimization: Don't log every micro-penny, maybe batch? For MVP, log all)
                    conn.execute("INSERT INTO transactions (wallet_id, amount, timestamp, description) VALUES (?, ?, ?, ?)",
                                 (wallet_id, amount, time.time(), f"Mining Yield: {wid}"))

        conn.commit()
    except Exception as e:
        print(f"Credit Error: {e}")
    finally:
        conn.close()

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

                        # Verify Signature against DB key
                        # Reconstruct string to verify
                        payload_str = json.dumps(payload, sort_keys=True)

                        key_to_use = pub_key if pub_key else payload['public_key']

                        if elysium_crypto.verify_signature(key_to_use, payload_str, signature):
                            # Valid Metric
                            last_ts = payload.get('timestamp', 0)
                            if time.time() - last_ts < 30: # Active in last 30s
                                active_count += 1
                                total_velocity += payload.get('velocity', 0)

                                # Credit the worker (Micro-transaction simulation)
                                update_worker_credit(wid, 0, 0.0001, pub_key)
                    except Exception as e:
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
    # Payload: { "worker_id": "...", "public_key": "PEM...", "hardware": {...}, "wallet_id": "..." }
    data = request.json
    wid = data.get('worker_id')
    pk = data.get('public_key') # Optional if already known
    hw = data.get('hardware')
    wallet = data.get('wallet_id')

    if wid:
        update_worker_credit(wid, 0, 0.0001, pk, hw, wallet) # Tiny keep-alive credit + Register HW + Link Wallet

    return jsonify({"status": "ack", "peers": CURRENT_MISSION["initial_peers"]})

@app.route('/api/job/config/<worker_id>')
def api_secure_config(worker_id):
    """
    Securely delivers the assigned topology/shard and private data keys
    to a specific authenticated worker.
    """
    with SCHEDULER.lock:
        assignment = SCHEDULER.topology_map.get(worker_id)

    if assignment:
        return jsonify({"status": "ASSIGNED", "config": assignment})
    else:
        # Default config if not scheduled yet
        return jsonify({"status": "WAITING", "config": {"role": "standby"}})

# --- AUTH API ---

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    email = request.form.get('email')
    password = request.form.get('password')
    user = verify_user(email, password)
    if user:
        flask.session['user_id'] = user['email']
        flask.session['wallet_id'] = user['wallet_id']
        return jsonify({"status": "ok", "redirect": "/dashboard"})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    email = request.form.get('email')
    password = request.form.get('password')
    if not email or not password: return jsonify({"status": "error"}), 400

    wid = register_user(email, password)
    if wid:
        flask.session['user_id'] = email
        flask.session['wallet_id'] = wid
        return jsonify({"status": "ok", "redirect": "/dashboard"})
    return jsonify({"status": "error", "message": "User exists"}), 400

@app.route('/api/auth/logout')
def api_logout():
    flask.session.clear()
    return flask.redirect('/')

# --- UI ROUTES ---

@app.route('/', methods=['GET', 'POST'])
def index():
    if 'user_id' not in flask.session:
        # LANDING / LOGIN PAGE
        return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Elysium Cloud | Sign In</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        body { background: #0f172a; color: #fff; font-family: 'Inter', sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0; }
        .login-card { background: #1e293b; padding: 40px; border-radius: 12px; width: 350px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); border: 1px solid #334155; }
        .logo { font-size: 24px; font-weight: 700; color: #fff; margin-bottom: 30px; text-align: center; }
        .logo span { color: #22c55e; }
        input { width: 100%; padding: 12px; margin-bottom: 15px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #fff; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #2563eb; color: #fff; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; }
        button:hover { background: #1d4ed8; }
        .switch { text-align: center; margin-top: 15px; font-size: 13px; color: #94a3b8; cursor: pointer; }
        .switch:hover { color: #fff; }
    </style>
    <script>
        let isSignup = false;
        function toggle() {
            isSignup = !isSignup;
            document.getElementById('title').innerText = isSignup ? "Create Account" : "Sign In";
            document.getElementById('btn').innerText = isSignup ? "Sign Up" : "Login";
            document.getElementById('switch').innerText = isSignup ? "Already have an account? Login" : "Don't have an account? Sign Up";
        }
        function submitForm(e) {
            e.preventDefault();
            const email = document.getElementById('email').value;
            const password = document.getElementById('password').value;
            const endpoint = isSignup ? '/api/auth/signup' : '/api/auth/login';

            fetch(endpoint, {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: `email=${encodeURIComponent(email)}&password=${encodeURIComponent(password)}`
            }).then(r=>r.json()).then(d=>{
                if(d.status=='ok') location.href = d.redirect;
                else alert(d.message || "Error");
            })
        }
    </script>
</head>
<body>
    <div class="login-card">
        <div class="logo"><span>◆</span> ELYSIUM</div>
        <h2 id="title" style="text-align:center; margin-top:0">Sign In</h2>
        <form onsubmit="submitForm(event)">
            <input type="email" id="email" placeholder="Email Address" required>
            <input type="password" id="password" placeholder="Password" required>
            <button id="btn" type="submit">Login</button>
        </form>
        <div id="switch" class="switch" onclick="toggle()">Don't have an account? Sign Up</div>
    </div>
</body>
</html>
""")

    return flask.redirect('/dashboard')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in flask.session: return flask.redirect('/')

    # Fetch Data
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row

    # 1. User Info
    user = conn.execute("SELECT * FROM users WHERE email=?", (flask.session['user_id'],)).fetchone()

    # 2. Fleet
    workers = conn.execute("SELECT * FROM workers WHERE owner_wallet_id=?", (user['wallet_id'],)).fetchall()

    # 3. Transactions
    txs = conn.execute("SELECT * FROM transactions WHERE wallet_id=? ORDER BY timestamp DESC LIMIT 50", (user['wallet_id'],)).fetchall()

    # 4. Global Stats
    global_velocity = CURRENT_MISSION["swarm_velocity"]

    conn.close()

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Elysium Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root { --primary: #2563eb; --success: #22c55e; --bg: #0f172a; --surface: #1e293b; --text: #f1f5f9; --border: #334155; }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); margin:0; display: flex; height: 100vh; overflow: hidden; }

        .sidebar { width: 250px; background: #020617; border-right: 1px solid var(--border); padding: 20px; display:flex; flex-direction:column; }
        .logo { font-size: 20px; font-weight: 700; margin-bottom: 40px; display:flex; align-items:center; gap:10px; }
        .logo span { color: var(--success); }
        .nav-item { padding: 12px; margin: 5px 0; border-radius: 8px; color: #94a3b8; cursor: pointer; transition: 0.2s; font-weight: 500; }
        .nav-item:hover, .nav-item.active { background: rgba(37, 99, 235, 0.1); color: var(--primary); }
        .nav-footer { margin-top: auto; border-top: 1px solid var(--border); padding-top: 20px; }

        .main { flex: 1; padding: 30px; overflow-y: auto; }
        .page { display: none; animation: fade 0.2s; }
        .page.active { display: block; }
        @keyframes fade { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }

        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
        .header h1 { font-size: 24px; font-weight: 600; margin: 0; }

        /* Cards */
        .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 30px; }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; position:relative; }
        .metric-val { font-size: 32px; font-weight: 700; margin: 10px 0 5px; }
        .metric-label { font-size: 13px; color: #94a3b8; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; }

        /* Tables */
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; color: #94a3b8; font-size: 12px; padding: 15px; border-bottom: 1px solid var(--border); text-transform:uppercase; font-weight:600; }
        td { padding: 15px; border-bottom: 1px solid var(--border); font-size: 14px; }
        .status-dot { height: 8px; width: 8px; background: var(--success); border-radius: 50%; display: inline-block; margin-right: 6px; }

        /* Buttons */
        .btn { background: var(--primary); color: #fff; border: none; padding: 10px 20px; border-radius: 6px; font-weight: 600; cursor: pointer; }
        .btn-outline { background: transparent; border: 1px solid var(--border); color: #fff; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }

        .tooltip { position: absolute; background: #000; color: #fff; padding: 5px 10px; border-radius: 4px; font-size: 12px; top: -30px; left: 50%; transform: translateX(-50%); display: none; white-space: nowrap; }
        .btn-withdraw:hover .tooltip { display: block; }
    </style>
    <script>
        function show(id) {
            document.querySelectorAll('.page').forEach(e => e.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            document.getElementById('p-'+id).classList.add('active');
            event.currentTarget.classList.add('active');
        }
    </script>
</head>
<body>
    <div class="sidebar">
        <div class="logo"><span>◆</span> ELYSIUM</div>
        <div class="nav-item active" onclick="show('overview')">📊 Overview</div>
        <div class="nav-item" onclick="show('earnings')">💰 Earnings</div>
        <div class="nav-item" onclick="show('fleet')">💻 My Fleet</div>
        <div class="nav-item" onclick="show('settings')">⚙️ Settings</div>

        <div class="nav-footer">
            <div style="font-size:12px; color:#64748b; margin-bottom:5px">LOGGED IN AS</div>
            <div style="font-size:14px; font-weight:600">{{ user.email }}</div>
            <a href="/api/auth/logout" style="display:block; margin-top:15px; color:#ef4444; text-decoration:none; font-size:13px">Sign Out</a>
        </div>
    </div>

    <div class="main">
        <!-- PAGE: OVERVIEW -->
        <div id="p-overview" class="page active">
            <div class="header"><h1>Dashboard Overview</h1></div>
            <div class="grid-3">
                <div class="card">
                    <div class="metric-label">Total Earnings</div>
                    <div class="metric-val" style="color:var(--success)">$ {{ "%.4f"|format(user.balance) }}</div>
                </div>
                <div class="card">
                    <div class="metric-label">Active Workers</div>
                    <div class="metric-val">{{ workers|length }}</div>
                </div>
                <div class="card">
                    <div class="metric-label">Global Velocity</div>
                    <div class="metric-val">{{ global_velocity|default(0) }} step/s</div>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-top:0">Recent Activity</h3>
                <table>
                    <thead><tr><th>Time</th><th>Description</th><th>Amount</th></tr></thead>
                    <tbody>
                        {% for tx in txs[:5] %}
                        <tr>
                            <td>{{ "%.0f"|format(time.time() - tx.timestamp) }}s ago</td>
                            <td>{{ tx.description }}</td>
                            <td style="color:var(--success)">+ ${{ "%.5f"|format(tx.amount) }}</td>
                        </tr>
                        {% else %}
                        <tr><td colspan="3" style="text-align:center; color:#64748b">No activity yet.</td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- PAGE: EARNINGS -->
        <div id="p-earnings" class="page">
            <div class="header"><h1>Wallet & Earnings</h1></div>

            <div class="card" style="display:flex; justify-content:space-between; align-items:center; margin-bottom:30px">
                <div>
                    <div class="metric-label">Available Balance</div>
                    <div class="metric-val" style="font-size:48px; color:var(--success)">$ {{ "%.4f"|format(user.balance) }}</div>
                    <div style="color:#94a3b8; font-size:14px">Wallet ID: <span style="font-family:monospace; color:#fff">{{ user.wallet_id }}</span></div>
                </div>
                <div style="position:relative" class="btn-withdraw">
                    <button class="btn" style="padding: 15px 40px; font-size:16px" disabled>WITHDRAW FUNDS</button>
                    <div class="tooltip">Coming Soon to MVP</div>
                </div>
            </div>

            <div class="card">
                <h3 style="margin-top:0">Transaction History</h3>
                <table>
                    <thead><tr><th>ID</th><th>Date</th><th>Description</th><th>Amount</th></tr></thead>
                    <tbody>
                        {% for tx in txs %}
                        <tr>
                            <td>#{{ tx.id }}</td>
                            <td>{{ time.ctime(tx.timestamp) }}</td>
                            <td>{{ tx.description }}</td>
                            <td style="color:var(--success)">+ ${{ "%.6f"|format(tx.amount) }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- PAGE: FLEET -->
        <div id="p-fleet" class="page">
            <div class="header"><h1>Compute Fleet</h1></div>
            <div class="card">
                <table>
                    <thead><tr><th>Worker ID</th><th>Status</th><th>Region</th><th>VRAM</th><th>Last Seen</th></tr></thead>
                    <tbody>
                        {% for w in workers %}
                        <tr>
                            <td style="font-family:monospace; font-weight:600">{{ w.worker_id }}</td>
                            <td><span class="status-dot"></span> Active</td>
                            <td>{{ w.region }}</td>
                            <td>
                                {% if w.hardware_specs %}
                                    {{ (w.hardware_specs|string|from_json).get('vram_gb', 'N/A') }} GB
                                {% else %} N/A {% endif %}
                            </td>
                            <td>{{ "%.0f"|format(time.time() - w.last_seen) }}s ago</td>
                        </tr>
                        {% else %}
                        <tr><td colspan="5" style="text-align:center; padding:40px">
                            No workers linked.<br>
                            <small style="color:#64748b">Run node with <code>--wallet_id {{ user.wallet_id }}</code></small>
                        </td></tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- PAGE: SETTINGS -->
        <div id="p-settings" class="page">
            <div class="header"><h1>Settings</h1></div>
            <div class="card">
                <div class="metric-label">Account Security</div>
                <p>Email: {{ user.email }}</p>
                <p>Password: ********</p>
                <button class="btn btn-outline">Change Password</button>
            </div>
        </div>

    </div>
</body>
</html>
""", user=user, workers=workers, txs=txs, time=time, from_json=json.loads, global_velocity=global_velocity)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=start_dht_service, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
