import os
import time
import sys
import argparse
import requests
import shutil
import zipfile
import subprocess
import importlib.util
import threading
import queue
from pathlib import Path

# --- AUTO-INSTALL DEPENDENCIES ---
def install_system_deps():
    try:
        import libtorrent
    except ImportError:
        try:
            print("[SYS] 🛠️ Instalando dependências do sistema...", flush=True)
            subprocess.check_call(['sudo', 'apt', 'update', '&&', 'sudo', 'apt', 'install', '-y', 'python3-libtorrent'], shell=True)
            os.execv(sys.executable, ['python3'] + sys.argv)
        except Exception as e:
            print(f"[SYS] ❌ Falha na auto-instalação: {e}", flush=True)
            sys.exit(1)

install_system_deps()
import libtorrent as lt

# --- ARGUMENTS & CONFIG ---
parser = argparse.ArgumentParser()
parser.add_argument("--master_url", type=str, default="http://127.0.0.1:5000")
args = parser.parse_args()

BASE_DIR = Path.cwd().resolve()
CACHE = BASE_DIR / "node_cache"
WORKSPACE = BASE_DIR / "node_workspace"
STAGING_AREA = WORKSPACE / "staging"
EXECUTION_AREA = WORKSPACE / "exec"
TORRENT_DIR = WORKSPACE / "torrents"

for p in [CACHE, WORKSPACE, STAGING_AREA, EXECUTION_AREA, TORRENT_DIR]:
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)

print(f"[SYS] 🚀 Node iniciado. Workspace: {WORKSPACE}", flush=True)

# --- BITTORRENT SESSION ---
BT_SESSION = lt.session()
BT_SESSION.listen_on(4881, 4891)
settings = {
    'active_downloads': 5,
    'active_seeds': 5,
    'download_rate_limit': 0,
    'upload_rate_limit': 0,
}
BT_SESSION.apply_settings(settings)

# --- GLOBAL STATS ---
STATS = {
    "shards_completed": 0,
    "total_time": 0.0,
    "avg_time": 0.0,
    "lock": threading.Lock()
}

PRESETS = {
    "CV": ["opencv-python-headless", "torchvision", "pillow", "ultralytics"],
    "AUDIO": ["librosa", "soundfile", "torchaudio"],
    "DATA": ["pandas", "scikit-learn", "numpy", "xgboost", "hivemind"]
}

# --- HELPER FUNCTIONS ---

def smart_install(req_file):
    if not req_file or not (WORKSPACE/req_file).exists(): return
    try:
        with open(WORKSPACE/req_file, 'r') as f: 
            content = f.read().lower()
        to_install = set()
        for cat, libs in PRESETS.items():
            if any(lib_base in content for lib_base in [l.split('-')[0] for l in libs]):
                print(f"[ENV] 🧠 Detectado perfil {cat}. Otimizando ambiente...", flush=True)
                for lib in libs:
                    if not importlib.util.find_spec(lib.replace("-","_")):
                        to_install.add(lib)
        if to_install:
            print(f"[ENV] Instalando: {', '.join(to_install)}", flush=True)
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + list(to_install) + ["--quiet"])
    except Exception as e:
        print(f"[ENV] ⚠️ Aviso na instalação de deps: {e}", flush=True)

def download_http(url, dest):
    target_url = f"{args.master_url}/api/storage/{url}"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(target_url, stream=True, timeout=30) as r:
            if r.status_code != 200:
                print(f"[ERR] HTTP {r.status_code}: {target_url}", flush=True)
                return False
            with open(dest, 'wb') as f:
                shutil.copyfileobj(r.raw, f)
        return True
    except Exception as e:
        print(f"[ERR] Falha HTTP ({dest.name}): {e}", flush=True)
        return False

def download_p2p_stream(url, save_path):
    try:
        t_name = f"temp_{int(time.time()*1000)}.torrent"
        torrent_file = TORRENT_DIR / t_name
        if not download_http(url, torrent_file): return None
        
        info = lt.torrent_info(str(torrent_file))
        params = {'save_path': str(save_path), 'ti': info, 'storage_mode': lt.storage_mode_t.storage_mode_sparse}
        h = BT_SESSION.add_torrent(params)
        
        print(f"[P2P] 📡 Conectando ao Swarm para {save_path.name}...", flush=True)
        timeout_counter = 0
        while not h.is_seed():
            s = h.status()
            if s.state == lt.torrent_status.error:
                print(f"[P2P] ❌ Erro: {s.errc}", flush=True)
                BT_SESSION.remove_torrent(h)
                return None
            time.sleep(1)
            timeout_counter += 1
            if timeout_counter > 300: 
                print(f"[P2P] ⏰ Timeout no download.", flush=True)
                BT_SESSION.remove_torrent(h)
                return None
        if torrent_file.exists(): os.remove(torrent_file)
        return h 
    except Exception as e:
        print(f"[P2P] Erro Crítico: {e}", flush=True)
        return None

# --- PIPELINE ENGINE ---

class PipelineEngine:
    def __init__(self, master_url, worker_id):
        self.master_url = master_url
        self.wid = worker_id
        self.ready_queue = queue.Queue(maxsize=2) 
        self.upload_queue = queue.Queue()
        self.active = True
        self.current_job_id = None

    def prefetch_worker(self):
        print("[THREAD] 🔮 Prefetcher iniciado.", flush=True)
        while self.active:
            if self.ready_queue.full():
                time.sleep(2); continue
            try:
                try:
                    r = requests.post(f"{self.master_url}/api/job/get_task", json={"worker_id": self.wid}, timeout=5)
                except requests.exceptions.ConnectionError:
                    time.sleep(10); continue

                if r.status_code != 200: time.sleep(5); continue
                task = r.json()
                if task['status'] == "TASK_FOUND":
                    sid = task['shard_id']
                    shard_staging_dir = STAGING_AREA / sid
                    shard_staging_dir.mkdir(exist_ok=True)
                    handle = download_p2p_stream(task['torrent_url'], shard_staging_dir)
                    if handle:
                        zip_path = shard_staging_dir / f"{sid}.zip"
                        data_dir = shard_staging_dir / "data"
                        data_dir.mkdir(exist_ok=True)
                        if zip_path.exists():
                            try:
                                with zipfile.ZipFile(zip_path, 'r') as z: z.extractall(data_dir)
                                os.remove(zip_path) 
                                BT_SESSION.remove_torrent(handle)
                                self.ready_queue.put({"meta": task, "data_path": data_dir, "staging_root": shard_staging_dir})
                                print(f"[PREFETCH] ✅ Shard {sid} pronto e na fila.", flush=True)
                            except zipfile.BadZipFile:
                                print(f"[ERR] Zip corrompido: {sid}", flush=True)
                                BT_SESSION.remove_torrent(handle)
                                shutil.rmtree(shard_staging_dir)
                    else:
                        shutil.rmtree(shard_staging_dir)
                        time.sleep(2)
                else: time.sleep(5)
            except Exception as e:
                print(f"[PREFETCH] Erro: {e}", flush=True)
                time.sleep(5)

    def upload_worker(self):
        print("[THREAD] ☁️ Uploader iniciado.", flush=True)
        while self.active:
            try:
                item = self.upload_queue.get()
                sid = item['shard_id']
                print(f"[UPLOAD] 🚀 Enviando resultados: {sid}...", flush=True)
                try:
                    with open(item['zip_path'], 'rb') as f:
                        r = requests.post(f"{self.master_url}/api/job/upload_result", files={'result_file': f}, data={'worker_id': self.wid, 'shard_id': sid}, timeout=60)
                    if r.status_code == 200:
                        requests.post(f"{self.master_url}/api/job/complete_task", json={"worker_id": self.wid, "shard_id": sid})
                        print(f"[UPLOAD] ✅ {sid} Concluído.", flush=True)
                    else: print(f"[UPLOAD] ❌ Falha HTTP {r.status_code}", flush=True)
                except Exception as e: print(f"[UPLOAD] ❌ Erro: {e}", flush=True)
                
                if os.path.exists(item['zip_path']): os.remove(item['zip_path'])
                if os.path.exists(item['staging_root']): shutil.rmtree(item['staging_root'])
                if os.path.exists(item['output_root']): shutil.rmtree(item['output_root'])
                self.upload_queue.task_done()
            except Exception as e: print(f"[UPLOAD] Erro: {e}", flush=True)

    def start(self):
        t1 = threading.Thread(target=self.prefetch_worker, daemon=True)
        t2 = threading.Thread(target=self.upload_worker, daemon=True)
        t1.start()
        t2.start()

# --- MAIN LOOP ---

def main():
    print("--- ELYSIUM NODE v2.2 (Smart Parsing) ---", flush=True)
    wid = f"node_{os.urandom(3).hex()}"
    print(f"[ID] Worker ID: {wid}", flush=True)
    
    engine = PipelineEngine(args.master_url, wid)
    engine.start()
    last_job_id = None
    
    while True:
        try:
            try:
                r = requests.get(f"{args.master_url}/api/job/current", timeout=5)
                if r.status_code == 200:
                    job_meta = r.json().get('meta', {})
                    if job_meta.get('status') == 'ACTIVE':
                        current_jid = job_meta.get('job_id')
                        if current_jid != last_job_id:
                            print(f"[NEW JOB] 🆕 Configurando Job: {current_jid}", flush=True)
                            code_zip = WORKSPACE / "code.zip"
                            if download_http(job_meta['bundle_file'], code_zip):
                                with zipfile.ZipFile(code_zip, 'r') as z: z.extractall(WORKSPACE)
                                smart_install(job_meta.get('requirements'))
                                last_job_id = current_jid
                                engine.current_job_id = current_jid
                            else:
                                print("[ERR] Falha ao baixar código.", flush=True)
                                time.sleep(10); continue
            except requests.exceptions.ConnectionError:
                print(f"[NET] ⚠️ Master offline ({args.master_url})...", flush=True)
                time.sleep(10); continue

            if engine.ready_queue.empty():
                print(f"[CPU] 💤 Aguardando pipeline... (Avg: {STATS['avg_time']:.1f}s)", end='\r', flush=True)
                time.sleep(1); continue
            
            package = engine.ready_queue.get()
            meta = package['meta']
            data_path = package['data_path']
            staging_root = package['staging_root']
            sid = meta['shard_id']
            
            print(f"\n--- [EXEC] Processando {sid} ---", flush=True)
            start_time = time.time()
            job_output_dir = EXECUTION_AREA / f"out_{sid}"
            job_output_dir.mkdir(parents=True, exist_ok=True)
            
            env = os.environ.copy()
            env["ELYSIUM_DATA_DIR"] = str(data_path.resolve())
            env["ELYSIUM_OUTPUT_DIR"] = str(job_output_dir.resolve())
            env["PYTHONPATH"] = str(WORKSPACE.resolve())
            if "initial_peers" in job_meta: env["ELYSIUM_INITIAL_PEERS"] = ",".join(job_meta["initial_peers"])
            
            # --- CORREÇÃO DE PARSING (V2) ---
            raw_entry = job_meta.get('entry_point', 'main.py').strip()
            parts = raw_entry.split()
            script_name = "main.py" 
            
            if len(parts) == 1:
                script_name = parts[0]
            elif parts[0].startswith("python"):
                if len(parts) > 1: script_name = parts[1]
            else:
                for p in parts:
                    if p.endswith(".py"):
                        script_name = p
                        break
            
            script_name = script_name.replace('"', '').replace("'", "")
            script_path = WORKSPACE / script_name

            if not script_path.exists():
                print(f"[ERR] ❌ Script não encontrado: {script_path}", flush=True)
                print(f"[DEBUG] EntryPoint Original: '{raw_entry}' | Interpretado: '{script_name}'", flush=True)
                print(f"[DEBUG] Arquivos no Workspace: {[f.name for f in WORKSPACE.glob('*')]}", flush=True)
                shutil.rmtree(staging_root)
                shutil.rmtree(job_output_dir)
                continue

            cmd = [sys.executable, str(script_path), "--data_dir", str(data_path), "--output_dir", str(job_output_dir)]
            # Se quiser passar os argumentos extras do entry_point (ex: --epochs 5)
            # podemos adicionar aqui, mas por segurança vamos rodar só o script base por enquanto.
            # Se precisar passar argumentos, a lógica de parsing terá que ser mais complexa.

            process = subprocess.Popen(cmd, shell=False, cwd=str(WORKSPACE), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            
            last_hb = time.time()
            for line in process.stdout:
                line = line.strip()
                if line: print(f"[JOB] {line}", flush=True)
                if time.time() - last_hb > 5:
                    try:
                        requests.post(f"{args.master_url}/api/job/heartbeat", json={"worker_id": wid, "avg_time": STATS['avg_time'], "queue_len": engine.ready_queue.qsize()}, timeout=1)
                        last_hb = time.time()
                    except: pass
            
            process.wait()
            duration = time.time() - start_time
            
            if process.returncode == 0:
                print(f"[CPU] ⚡ {sid} Finalizado em {duration:.2f}s.", flush=True)
                with STATS['lock']:
                    STATS['shards_completed'] += 1
                    STATS['total_time'] += duration
                    STATS['avg_time'] = STATS['total_time'] / STATS['shards_completed']

                result_zip_path = WORKSPACE / f"result_{sid}.zip"
                has_results = False
                with zipfile.ZipFile(result_zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
                    for root, _, files in os.walk(job_output_dir):
                        for file in files:
                            z.write(os.path.join(root, file), file)
                            has_results = True
                if not has_results:
                    with zipfile.ZipFile(result_zip_path, 'w') as z: z.writestr("log.txt", "Job finished with no output files.")

                engine.upload_queue.put({"shard_id": sid, "zip_path": result_zip_path, "staging_root": staging_root, "output_root": job_output_dir})
            else:
                print(f"[ERR] Job falhou com código {process.returncode}", flush=True)
                shutil.rmtree(staging_root)
                shutil.rmtree(job_output_dir)

        except KeyboardInterrupt:
            print("\n[SYS] Parando Node...", flush=True); engine.active = False; break
        except Exception as e:
            print(f"[CRITICAL] Erro no loop: {e}", flush=True); time.sleep(5)

if __name__ == "__main__":
    main()