import sys, os, subprocess, time

# --- 1. BOOTLOADER HÍBRIDO (WINDOWS -> WSL) ---
# Se estiver rodando no Windows, ele se "auto-injeta" no WSL
if os.name == 'nt':
    print("[BOOT] 🖥️ Detectado Windows. Preparando ambiente WSL Linux...", flush=True)
    
    current_script = os.path.abspath(__file__)
    drive, path = os.path.splitdrive(current_script)
    wsl_path = f"/mnt/{drive.lower().replace(':', '')}{path.replace(os.sep, '/')}"
    
    print("[BOOT] 🛠️ Verificando e Instalando Dependências no WSL (pode pedir senha)...")
    try:
        install_cmd = "sudo apt update && sudo apt install -y python3 python3-pip python3-flask python3-libtorrent zip unzip"
        subprocess.check_call(["wsl", "bash", "-c", install_cmd])
    except subprocess.CalledProcessError:
        print("[BOOT] ❌ Falha na instalação automática. Verifique se o WSL está funcionando.")
        sys.exit(1)

    print(f"[BOOT] 🚀 Lançando Master dentro do Linux: {wsl_path}")
    print("="*60)
    subprocess.call(["wsl", "python3", wsl_path])
    sys.exit(0)

# ==============================================================================
# DAQUI PARA BAIXO É CÓDIGO LINUX (RODANDO DENTRO DO WSL)
# ==============================================================================

import flask
from flask import Flask, jsonify, request, render_template_string, send_from_directory
from werkzeug.utils import secure_filename
import sqlite3, threading, zipfile, json, math, shutil, random
from pathlib import Path
from datetime import datetime

try:
    import libtorrent as lt
except ImportError:
    print("[LINUX] ❌ Erro Crítico: libtorrent não carregou. O Bootloader falhou?")
    sys.exit(1)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- CONFIGURAÇÃO E ESTRUTURA ---
DB_FILE = "elysium_ledger.db"
STORAGE_DIR = Path("./elysium_storage")
SHARDS_DIR = STORAGE_DIR / "shards"
ARTIFACTS_DIR = STORAGE_DIR / "artifacts" # Nova estrutura de artefatos
TORRENTS_DIR = STORAGE_DIR / "torrents"
BUNDLE_DIR = STORAGE_DIR / "bundles"

# Garante a existência de todas as pastas críticas
for p in [STORAGE_DIR, SHARDS_DIR, ARTIFACTS_DIR, TORRENTS_DIR, BUNDLE_DIR]: 
    p.mkdir(parents=True, exist_ok=True)

WSL_VENV_PATH = "~/.elysium_venv_master"

# --- CONFIGURAÇÃO DE ALOCAÇÃO AVANÇADA ---
BATCH_TARGET_MB = 1024  # Meta de 1GB por lote
MIN_STEAL_TIME = 60     # Só rouba se o node vitima for demorar mais de 60s
STEAL_RATIO = 0.3       # Rouba 30% da fila da vítima

# --- ESTADO GLOBAL (IN-MEMORY CACHE) ---
CURRENT_MISSION = {
    "job_id": None,
    "job_start_time": None,
    "shards_registry": {}, # { 'shard_0': {'status': 'PENDING', 'worker': None, 'size_mb': 50.5, 'torrent': 'file.torrent'} }
    "worker_queues": {},    # { 'worker_A': ['shard_1', 'shard_2'] }
    "worker_stats": {},     # { 'worker_A': {'avg_time': 12.5, 'last_seen': 123456, 'resources': {}} }
    "total_shards": 0,
    "initial_peers": [], 
    "status": "IDLE", 
    "logs": []
}
BT_SESSION = None

def log_master(msg):
    ts = time.strftime('%H:%M:%S')
    print(f"[MASTER] {msg}", flush=True)
    CURRENT_MISSION["logs"].append(f"{ts} - {msg}")
    if len(CURRENT_MISSION["logs"]) > 100: CURRENT_MISSION["logs"].pop(0)

# --- MOTOR BITTORRENT (SEEDER) ---
def start_bittorrent_seeder():
    global BT_SESSION
    log_master("🚀 Iniciando Motor BitTorrent (Seeder)...")
    BT_SESSION = lt.session()
    try: 
        BT_SESSION.listen_on(6881, 6891)
        log_master("✅ BitTorrent ouvindo nas portas 6881-6891")
    except Exception as e: 
        log_master(f"⚠️ Aviso BitTorrent: {e}")
    while True: time.sleep(1)

def create_and_seed_torrent(file_path):
    try:
        fs = lt.file_storage()
        lt.add_files(fs, str(file_path))
        t = lt.create_torrent(fs)
        t.set_creator('Elysium Master Node')
        lt.set_piece_hashes(t, str(file_path.parent))
        
        torrent_path = TORRENTS_DIR / (file_path.name + ".torrent")
        with open(torrent_path, "wb") as f: f.write(lt.bencode(t.generate()))
            
        params = {
            'save_path': str(file_path.parent),
            'ti': lt.torrent_info(str(torrent_path))
        }
        BT_SESSION.add_torrent(params)
        return torrent_path.name
    except Exception as e:
        log_master(f"❌ Erro ao criar Torrent para {file_path.name}: {e}"); return None

# --- INTEGRAÇÃO HIVEMIND (DHT) ---
def start_dht_bridge():
    dht_script = r'''import time, hivemind; dht = hivemind.DHT(start=True, host_maddrs=["/ip4/0.0.0.0/tcp/8001"]); print(f"__ADDR_START__{dht.get_visible_maddrs()[0]}__ADDR_END__", flush=True); while True: time.sleep(10)'''
    with open("elysium_dht_daemon.py", "w") as f: f.write(dht_script)
    
    setup = f"if [ ! -d {WSL_VENV_PATH} ]; then python3 -m venv --system-site-packages {WSL_VENV_PATH}; fi && " \
            f"source {WSL_VENV_PATH}/bin/activate && pip install hivemind torch --quiet 2>/dev/null"
    subprocess.call(["bash", "-c", setup])
    
    cmd = f"source {WSL_VENV_PATH}/bin/activate && python3 {os.getcwd()}/elysium_dht_daemon.py"
    proc = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
    for line in proc.stdout:
        if "__ADDR_START__" in line:
            try:
                addr = line.split("__ADDR_START__")[1].split("__ADDR_END__")[0]
                CURRENT_MISSION["initial_peers"] = [addr]
                log_master(f"🌐 DHT Anchor Online: {addr}")
            except: pass

# --- SHARDING INTELIGENTE ---
def shard_dataset(zip_path, chunks=20):
    log_master(f"🔪 Fatiando Dataset {zip_path.name} em {chunks} partes estocásticas...")
    
    # Limpeza de torrents antigos da sessão
    if BT_SESSION:
        for t in BT_SESSION.get_torrents(): BT_SESSION.remove_torrent(t)
            
    # Limpa diretórios temporários
    for f in SHARDS_DIR.glob("*"): os.remove(f)
    for f in TORRENTS_DIR.glob("*"): os.remove(f)
    
    # Reset Total
    CURRENT_MISSION["shards_registry"] = {}
    CURRENT_MISSION["worker_queues"] = {}
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            files = [f for f in z.namelist() if not f.endswith('/')]
            random.shuffle(files) # Shuffle para evitar viés de dados sequenciais
            
            if not files: 
                log_master("❌ Erro: Dataset vazio.")
                return

            batch_size = math.ceil(len(files) / chunks)
            
            for i in range(chunks):
                batch = files[i*batch_size : (i+1)*batch_size]
                if not batch: break
                
                shard_name = f"shard_{i}"
                shard_path = SHARDS_DIR / f"{shard_name}.zip"
                
                with zipfile.ZipFile(shard_path, 'w', zipfile.ZIP_DEFLATED) as out:
                    for f in batch: out.writestr(f, z.read(f))
                
                size_mb = shard_path.stat().st_size / (1024 * 1024)
                t_name = create_and_seed_torrent(shard_path)
                
                if t_name: 
                    CURRENT_MISSION["shards_registry"][shard_name] = {
                        "status": "PENDING", 
                        "worker": None, 
                        "size_mb": size_mb,
                        "torrent": t_name,
                        "created_at": time.time()
                    }
        
        CURRENT_MISSION["total_shards"] = len(CURRENT_MISSION["shards_registry"])
        log_master(f"✅ Sharding concluído: {CURRENT_MISSION['total_shards']} fragmentos prontos para distribuição.")
    except Exception as e: log_master(f"❌ Erro Crítico no Sharding: {e}")

# --- BANCO DE DADOS (PERSISTÊNCIA) ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS workers (worker_id TEXT PRIMARY KEY, last_seen REAL, earnings REAL, total_shards INTEGER)")
    conn.commit(); conn.close()

def update_db(wid, amount=0, shards_inc=0):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("INSERT OR IGNORE INTO workers VALUES (?, ?, 0, 0)", (wid, time.time()))
    conn.execute("UPDATE workers SET last_seen=?, earnings=earnings+?, total_shards=total_shards+? WHERE worker_id=?", 
                 (time.time(), amount, shards_inc, wid))
    conn.commit(); conn.close()

# --- ENGINE DE ORQUESTRAÇÃO (WORK STEALING) ---
def try_steal_work(thief_wid):
    """
    Algoritmo Robin Hood v2:
    Analisa a latência média de cada worker e rouba tarefas de quem 
    tem TTC (Time To Completion) muito alto.
    """
    candidates = []
    for victim_wid, queue in CURRENT_MISSION["worker_queues"].items():
        if victim_wid == thief_wid or not queue: continue
        
        stats = CURRENT_MISSION["worker_stats"].get(victim_wid, {'avg_time': 30.0})
        avg_time = stats.get('avg_time', 30.0)
        estimated_finish_time = len(queue) * avg_time
        
        if estimated_finish_time > MIN_STEAL_TIME:
            candidates.append((estimated_finish_time, victim_wid))
    
    candidates.sort(reverse=True, key=lambda x: x[0])
    
    if candidates:
        ttc, victim_id = candidates[0]
        victim_queue = CURRENT_MISSION["worker_queues"][victim_id]
        steal_count = max(1, int(len(victim_queue) * STEAL_RATIO))
        stolen_shards = victim_queue[-steal_count:]
        
        # Atomically transfer
        CURRENT_MISSION["worker_queues"][victim_id] = victim_queue[:-steal_count]
        log_master(f"⚖️ STEAL: {thief_wid} roubou {steal_count} shards de {victim_id} (Load Balancing).")
        return stolen_shards
    return None

def assign_next_task(wid):
    # 1. Fila Pessoal
    if wid in CURRENT_MISSION["worker_queues"] and CURRENT_MISSION["worker_queues"][wid]:
        return CURRENT_MISSION["worker_queues"][wid].pop(0), "Local Queue"

    # 2. Alocação em Lote (Batching)
    pending = [sid for sid, data in CURRENT_MISSION["shards_registry"].items() if data['status'] == "PENDING"]
    if pending:
        new_batch = []
        current_mb = 0
        for sid in pending:
            if current_mb >= BATCH_TARGET_MB: break
            s_data = CURRENT_MISSION["shards_registry"][sid]
            new_batch.append(sid)
            current_mb += s_data['size_mb']
            CURRENT_MISSION["shards_registry"][sid]['status'] = "ASSIGNED"
            CURRENT_MISSION["shards_registry"][sid]['worker'] = wid
        
        target = new_batch[0]
        if len(new_batch) > 1:
            CURRENT_MISSION["worker_queues"][wid] = new_batch[1:]
        log_master(f"📦 BATCH: {wid} recebeu {len(new_batch)} shards ({current_mb:.1f} MB).")
        return target, "New Batch"

    # 3. Work Stealing
    stolen = try_steal_work(wid)
    if stolen:
        target = stolen[0]
        for sid in stolen: CURRENT_MISSION["shards_registry"][sid]['worker'] = wid
        if len(stolen) > 1: CURRENT_MISSION["worker_queues"][wid] = stolen[1:]
        return target, "Stolen Work"

    return None, None

# --- AGREGADOR DE RESULTADOS (NEURAL REDUCER) ---
def finalize_job_artifacts(job_id):
    """
    Compila todos os resultados parciais em um único 'Super Artifact'.
    Isso simula o 'reduce' do MapReduce ou Federated Averaging simples.
    """
    job_dir = ARTIFACTS_DIR / job_id
    if not job_dir.exists(): return
    
    log_master(f"🔄 Iniciando agregação final para Job: {job_id}")
    
    final_zip_name = f"MERGED_RESULTS_{job_id}.zip"
    final_zip_path = ARTIFACTS_DIR / final_zip_name
    
    try:
        with zipfile.ZipFile(final_zip_path, 'w', zipfile.ZIP_DEFLATED) as master_zip:
            # Varre todos os zips de resultados dentro da pasta do Job
            for result_zip in job_dir.glob("*.zip"):
                # Opção A: Apenas concatenar os ZIPs (mais rápido)
                master_zip.write(result_zip, arcname=f"parts/{result_zip.name}")
                
                # Opção B (Avançada): Extrair e renomear pesos (futuro)
                # Isso seria onde a lógica de PyTorch State Dict Merge entraria
        
        log_master(f"✅ Agregação concluída: {final_zip_name} criado com sucesso.")
        CURRENT_MISSION["logs"].append(f"JOB COMPLETED: Download {final_zip_name}")
        
    except Exception as e:
        log_master(f"❌ Falha na agregação final: {e}")

# --- ROTAS DA API FLASK ---

@app.route('/api/job/current')
def api_job(): 
    total = CURRENT_MISSION["total_shards"]
    done = len([s for s in CURRENT_MISSION["shards_registry"].values() if s['status'] == "COMPLETED"])
    prog = int((done/total)*100) if total > 0 else 0
    
    # Trigger de Finalização
    if total > 0 and done == total and CURRENT_MISSION.get("status") == "ACTIVE":
        CURRENT_MISSION["status"] = "COMPLETED"
        threading.Thread(target=finalize_job_artifacts, args=(CURRENT_MISSION["job_id"],)).start()
        
    return jsonify({
        "meta": CURRENT_MISSION, 
        "progress": prog, 
        "done": done, 
        "total": total, 
        "logs": CURRENT_MISSION["logs"]
    })

@app.route('/api/storage/<path:filename>')
def api_download(filename):
    # Roteamento inteligente de arquivos
    if "torrents/" in filename: return send_from_directory(TORRENTS_DIR, filename.replace("torrents/", ""))
    if "bundles/" in filename: return send_from_directory(BUNDLE_DIR, filename.replace("bundles/", ""))
    if "results/" in filename: 
        # Suporte para baixar merged results
        return send_from_directory(ARTIFACTS_DIR, filename.replace("results/", ""))
    return send_from_directory(STORAGE_DIR, filename)

@app.route('/api/job/get_task', methods=['POST'])
def api_get_task():
    wid = request.json.get('worker_id')
    if CURRENT_MISSION["status"] != "ACTIVE":
        return jsonify({"status": "NO_JOB"})

    shard_id, reason = assign_next_task(wid)
    
    if shard_id:
        s_data = CURRENT_MISSION["shards_registry"][shard_id]
        remaining = len(CURRENT_MISSION["worker_queues"].get(wid, []))
        return jsonify({
            "status": "TASK_FOUND", 
            "shard_id": shard_id, 
            "torrent_url": f"torrents/{s_data['torrent']}",
            "info": reason,
            "queue_len": remaining
        })
    return jsonify({"status": "NO_TASKS"})

@app.route('/api/job/upload_result', methods=['POST'])
def api_upload_result():
    try:
        f = request.files['result_file']
        wid = request.form.get('worker_id')
        sid = request.form.get('shard_id')
        jid = CURRENT_MISSION.get('job_id', 'unknown_job')
        
        # 1. Cria pasta específica para o Job ID (Isolamento de Artefatos)
        job_dir = ARTIFACTS_DIR / jid
        job_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. Salva o arquivo de forma organizada
        s_name = secure_filename(f"{sid}_by_{wid}.zip")
        save_path = job_dir / s_name
        f.save(save_path)
        
        log_master(f"📥 Recebido resultado de {wid} para {sid}")
        return jsonify({"status": "saved", "path": str(save_path)})
    except Exception as e:
        log_master(f"❌ Erro no Upload: {e}")
        return jsonify({"status": "error"}), 500

@app.route('/api/job/complete_task', methods=['POST'])
def api_complete():
    d = request.json; sid, wid = d.get('shard_id'), d.get('worker_id')
    if sid in CURRENT_MISSION["shards_registry"]:
        CURRENT_MISSION["shards_registry"][sid]['status'] = "COMPLETED"
        update_db(wid, 0.0050, 1) # Paga $0.0050 e incrementa contador
    return jsonify({"status": "ACK"})

@app.route('/api/job/heartbeat', methods=['POST'])
def api_heartbeat(): 
    d = request.json
    wid = d.get('worker_id')
    
    # Armazena telemetria avançada
    if wid:
        CURRENT_MISSION["worker_stats"][wid] = {
            'avg_time': d.get('avg_time', 0),
            'resources': d.get('resources', {}), # CPU, RAM, etc
            'last_seen': time.time()
        }
        update_db(wid, 0.0001)
    return jsonify({"status":"ok"})

@app.route('/api/storage/delete', methods=['POST'])
def api_delete():
    # Segurança básica para deleção
    try:
        fname = request.form.get('filename')
        ftype = request.form.get('type')
        target = None
        if ftype == 'bundle': target = BUNDLE_DIR / fname
        elif ftype == 'data': target = STORAGE_DIR / fname # Datasets root
        elif ftype == 'artifact': target = ARTIFACTS_DIR / fname
        
        if target and target.exists():
            if target.is_dir(): shutil.rmtree(target) # Suporte a deletar pastas de jobs
            else: os.remove(target)
            log_master(f"🗑️ Deletado: {fname}")
            return jsonify({"status": "ok"})
        return jsonify({"status": "not_found"}), 404
    except Exception as e: return jsonify({"status": "error", "msg": str(e)}), 500

# --- INTERFACE WEB REFORMULADA ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        try:
            # Lógica de Upload e Inicialização de Job
            bundle_path = None; data_path = None
            
            # Code Handling
            if 'existing_code' in request.form and request.form['existing_code']:
                bundle_path = BUNDLE_DIR / request.form['existing_code']
            elif 'project_bundle' in request.files and request.files['project_bundle'].filename:
                f = request.files['project_bundle']
                name = secure_filename(f.filename)
                bundle_path = BUNDLE_DIR / name; f.save(bundle_path)
                
            # Data Handling
            if 'existing_data' in request.form and request.form['existing_data']:
                data_path = STORAGE_DIR / request.form['existing_data']
            elif 'dataset_file' in request.files and request.files['dataset_file'].filename:
                f = request.files['dataset_file']
                name = secure_filename(f.filename)
                data_path = STORAGE_DIR / name; f.save(data_path)
                
            if bundle_path and data_path:
                job_name = f"JOB_{int(time.time())}"
                
                # Tenta ler manifesto se existir, senão usa defaults
                entry_point = "main.py"
                try:
                    with zipfile.ZipFile(bundle_path, 'r') as z: 
                        if 'elysium.json' in z.namelist():
                            m = json.load(z.open('elysium.json'))
                            entry_point = m.get('entry_point', entry_point)
                except: pass

                CURRENT_MISSION.update({
                    'job_id': job_name, 
                    'job_start_time': time.time(),
                    'bundle_file': f"bundles/{bundle_path.name}",
                    'entry_point': entry_point, 
                    'status': "ACTIVE"
                })
                
                # Inicia Sharding em Thread separada para não travar UI
                threading.Thread(target=shard_dataset, args=(data_path, 50)).start()
                
        except Exception as e: log_master(f"Erro no Deploy: {e}")
    
    # Listagens para UI
    bundles = sorted([f.name for f in BUNDLE_DIR.glob("*.zip")])
    datasets = sorted([f.name for f in STORAGE_DIR.glob("*.zip")])
    # Lista apenas os arquivos merged ou pastas de jobs
    artifacts = sorted([f.name for f in ARTIFACTS_DIR.iterdir()], reverse=True)
    
    conn = sqlite3.connect(DB_FILE); conn.row_factory = sqlite3.Row
    workers_db = conn.execute("SELECT * FROM workers WHERE last_seen > ?", (time.time()-120,)).fetchall()
    conn.close()

    # HTML/CSS Enterprise
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Elysium Cloud | Enterprise Command</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
    <style>
        :root {
            --primary: #2563eb; --success: #22c55e; --bg: #f1f5f9;
            --surface: #ffffff; --text: #0f172a; --sidebar: #0f172a;
        }
        body { font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); display: flex; height: 100vh; margin:0; overflow:hidden;}
        .sidebar { width: 240px; background: var(--sidebar); color: #fff; display: flex; flex-direction: column; padding: 20px; }
        .logo { font-size: 18px; font-weight: 700; margin-bottom: 40px; display: flex; align-items: center; gap: 10px; }
        .logo span { color: var(--success); }
        .nav-item { padding: 12px; margin-bottom: 5px; border-radius: 6px; cursor: pointer; color: #94a3b8; transition: 0.2s; }
        .nav-item:hover, .nav-item.active { background: rgba(255,255,255,0.1); color: #fff; }
        .content { flex: 1; padding: 30px; overflow-y: auto; display: none; }
        .content.active { display: block; animation: fadeIn 0.3s; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(5px); } to { opacity: 1; transform: translateY(0); } }
        
        .header { display: flex; justify-content: space-between; margin-bottom: 30px; }
        .card { background: var(--surface); border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 24px; }
        .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; }
        
        .metric { font-size: 32px; font-weight: 700; color: var(--text); }
        .metric-label { font-size: 13px; color: #64748b; font-weight: 500; text-transform: uppercase; }
        
        .terminal { background: #1e293b; color: #f8fafc; font-family: 'JetBrains Mono'; padding: 20px; border-radius: 8px; height: 300px; overflow-y: auto; font-size: 12px; }
        .log { border-bottom: 1px solid #334155; padding: 4px 0; }
        
        .btn { background: var(--primary); color: #fff; border: none; padding: 12px 24px; border-radius: 6px; font-weight: 600; cursor: pointer; width: 100%; }
        .btn:hover { background: #1d4ed8; }
        .btn-del { background: transparent; border: 1px solid #ef4444; color: #ef4444; padding: 4px 8px; font-size: 11px; border-radius: 4px; cursor: pointer; }
        .btn-del:hover { background: #ef4444; color: #fff; }

        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; color: #64748b; font-size: 12px; padding: 10px; border-bottom: 1px solid #e2e8f0; }
        td { padding: 12px 10px; border-bottom: 1px solid #e2e8f0; font-size: 14px; }
        
        .progress-bar { height: 6px; background: #e2e8f0; border-radius: 99px; overflow: hidden; margin-top: 10px; }
        .fill { height: 100%; background: var(--success); width: 0%; transition: width 0.5s; }
    </style>
    <script>
        function show(id) {
            document.querySelectorAll('.content').forEach(e => e.classList.remove('active'));
            document.querySelectorAll('.nav-item').forEach(e => e.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            event.currentTarget.classList.add('active');
        }
        function refresh() {
            fetch('/api/job/current').then(r=>r.json()).then(d=>{
                document.getElementById('m-job').innerText = d.meta.job_id || "IDLE";
                document.getElementById('m-shards').innerText = `${d.done}/${d.total}`;
                document.getElementById('p-fill').style.width = d.progress + '%';
                const logs = d.logs.map(l=>`<div class="log">${l}</div>`).join('');
                const el = document.getElementById('term');
                if(el.innerHTML != logs) { el.innerHTML = logs; el.scrollTop = el.scrollHeight; }
            })
        }
        setInterval(refresh, 2000);
        
        function del(name, type) {
            if(!confirm("Confirm delete?")) return;
            const fd = new FormData(); fd.append('filename', name); fd.append('type', type);
            fetch('/api/storage/delete', {method:'POST', body:fd}).then(r=>r.json()).then(d=>{
                if(d.status=='ok') location.reload(); else alert('Error');
            })
        }
    </script>
</head>
<body>
    <div class="sidebar">
        <div class="logo"><span>◆</span> ELYSIUM</div>
        <div class="nav-item active" onclick="show('dash')">📊 Dashboard</div>
        <div class="nav-item" onclick="show('deploy')">🚀 Deploy</div>
        <div class="nav-item" onclick="show('fleet')">💻 Fleet</div>
        <div class="nav-item" onclick="show('data')">🗄️ Artifacts</div>
    </div>
    
    <div id="dash" class="content active">
        <div class="header"><h1>Mission Control</h1></div>
        <div class="grid-3">
            <div class="card"><div class="metric-label">Active Job</div><div class="metric" id="m-job">---</div></div>
            <div class="card"><div class="metric-label">Progress</div><div class="metric" id="m-shards">0/0</div><div class="progress-bar"><div class="fill" id="p-fill"></div></div></div>
            <div class="card"><div class="metric-label">Active Workers</div><div class="metric">{{ workers|length }}</div></div>
        </div>
        <div class="card" style="background:#1e293b; padding:0">
            <div id="term" class="terminal"></div>
        </div>
    </div>
    
    <div id="deploy" class="content">
        <div class="header"><h1>New Mission</h1></div>
        <div class="card" style="max-width: 600px">
            <form method="post" enctype="multipart/form-data">
                <label class="metric-label">Code Bundle (.zip)</label>
                <div style="display:flex; gap:10px; margin: 10px 0 20px;">
                    <select name="existing_code" style="flex:1"><option value="">Select Existing...</option>{% for b in bundles %}<option>{{b}}</option>{% endfor %}</select>
                    <input type="file" name="project_bundle">
                </div>
                
                <label class="metric-label">Dataset (.zip)</label>
                <div style="display:flex; gap:10px; margin: 10px 0 20px;">
                    <select name="existing_data" style="flex:1"><option value="">Select Existing...</option>{% for d in datasets %}<option>{{d}}</option>{% endfor %}</select>
                    <input type="file" name="dataset_file">
                </div>
                <button class="btn">INITIALIZE SWARM</button>
            </form>
        </div>
    </div>
    
    <div id="fleet" class="content">
        <div class="header"><h1>Compute Fleet</h1></div>
        <div class="card">
            <table>
                <thead><tr><th>ID</th><th>Last Seen</th><th>Shards Done</th><th>Total Earnings</th></tr></thead>
                <tbody>
                    {% for w in workers %}
                    <tr>
                        <td>{{ w.worker_id }}</td>
                        <td>{{ "%.0f"|format(time.time() - w.last_seen) }}s ago</td>
                        <td>{{ w.total_shards }}</td>
                        <td style="color:var(--success); font-weight:bold">${{ "%.4f"|format(w.earnings) }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    
    <div id="data" class="content">
        <div class="header"><h1>Artifacts & Results</h1></div>
        <div class="card">
            <table>
                <thead><tr><th>Name</th><th>Type</th><th>Action</th></tr></thead>
                <tbody>
                    {% for a in artifacts %}
                    <tr>
                        <td style="font-weight:600">{{ a }}</td>
                        <td>{% if 'MERGED' in a %}📦 Combined Result{% else %}📂 Job Folder{% endif %}</td>
                        <td>
                            {% if 'MERGED' in a %}
                                <a href="/api/storage/results/{{a}}" style="text-decoration:none; margin-right:10px">⬇ Download</a>
                            {% endif %}
                            <button class="btn-del" onclick="del('{{a}}', 'artifact')">DELETE</button>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</body>
</html>
    """, bundles=bundles, datasets=datasets, artifacts=artifacts, workers=workers_db, time=time)

if __name__ == "__main__":
    init_db()
    # Daemons
    threading.Thread(target=start_dht_bridge, daemon=True).start()
    threading.Thread(target=start_bittorrent_seeder, daemon=True).start()
    print("=== ELYSIUM MASTER v2.0 (Enterprise Artifacts) ONLINE ===")
    app.run(host='0.0.0.0', port=5000)