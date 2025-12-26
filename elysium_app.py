import sys
import os
import subprocess
import requests
import time
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel,
                             QVBoxLayout, QWidget, QTextEdit, QHBoxLayout, QFrame, QGraphicsDropShadowEffect)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QTimer

# --- CONFIGURAÇÃO DE REDE ---
FORCE_MASTER_IP = "127.0.0.1"

# --- NODE THREAD ---
class NodeThread(QThread):
    log_sig = pyqtSignal(str)
    worker_id_sig = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.process = None
        self.running = True

    def run(self):
        self.log_sig.emit("🔄 SYSTEM STARTUP SEQUENCE...")
        master_url = ""
        if FORCE_MASTER_IP != "AUTO":
            master_url = f"http://{FORCE_MASTER_IP}:5000"
        else:
            try:
                raw = subprocess.check_output(["wsl", "bash", "-c", "ip route show | grep default"]).decode()
                host_ip = raw.split()[2]
                master_url = f"http://{host_ip}:5000"
            except:
                master_url = "http://127.0.0.1:5000"

        self.log_sig.emit(f"📡 UPLINK ESTABLISHED: {master_url}")

        # In V4.0, Node runs on Host (Python), manages Docker
        # We assume dependencies are installed on host (or we use a venv on host)

        # If running in Windows, we might need to invoke python directly if not using WSL for Node logic anymore (or hybrid)
        # But previous logic used WSL. Let's stick to invoking python.
        # Since elysium_node.py now uses `docker` python sdk, it must run where docker socket is available.

        cmd = [sys.executable, "elysium_node.py", "--master_url", master_url]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', bufsize=1,
                cwd=os.getcwd()
            )

            for line in self.process.stdout:
                if not self.running: break
                l = line.strip()
                self.log_sig.emit(l)

                # Detect Worker ID from logs
                if "[INIT] 📝 Registering" in l and "with Master" in l:
                    try:
                        # Log format: "[INIT] 📝 Registering <wid> with Master..."
                        parts = l.split("Registering ")[1].split(" with")
                        wid = parts[0].strip()
                        self.worker_id_sig.emit(wid)
                    except: pass

        except Exception as e:
            self.log_sig.emit(f"❌ CRITICAL FAILURE: {e}")

    def stop(self):
        self.running = False
        if self.process: self.process.terminate()

# --- TESLA-STYLE GUI ---
class ElysiumGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ELYSIUM COMPUTE NODE V4.0")
        self.resize(1000, 650)

        self.setStyleSheet("""
            QMainWindow { background-color: #121212; }
            QWidget { font-family: 'Segoe UI', 'Roboto', sans-serif; }
            QTextEdit {
                background-color: #0a0a0a; border: 1px solid #333; color: #00ff9d;
                font-family: 'Consolas', 'Courier New', monospace; font-size: 11px;
                border-radius: 4px; padding: 10px;
            }
            QLabel { color: #e0e0e0; }
        """)

        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(20)

        # --- HEADER / STATUS ---
        header = QHBoxLayout()

        # Coluna da Esquerda (Ganhos & Swarm)
        stats_box = QVBoxLayout()

        lbl_earn_title = QLabel("LIFETIME EARNINGS")
        lbl_earn_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold;")
        self.lbl_earn = QLabel("$ 0.0000")
        self.lbl_earn.setStyleSheet("color: #ffffff; font-size: 32px; font-weight: 300;")

        lbl_swarm_title = QLabel("SWARM VELOCITY")
        lbl_swarm_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold; margin-top: 10px;")
        self.lbl_swarm = QLabel("0.0 step/s")
        self.lbl_swarm.setStyleSheet("color: #00E676; font-size: 24px; font-weight: 300;")

        stats_box.addWidget(lbl_earn_title)
        stats_box.addWidget(self.lbl_earn)
        stats_box.addWidget(lbl_swarm_title)
        stats_box.addWidget(self.lbl_swarm)

        # Coluna da Direita (Status)
        status_box = QVBoxLayout()
        status_box.setAlignment(Qt.AlignRight)

        lbl_status_title = QLabel("SYSTEM STATUS")
        lbl_status_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold; text-align: right;")
        self.lbl_status = QLabel("STANDBY")
        self.lbl_status.setStyleSheet("color: #666; font-size: 18px; font-weight: bold;")

        lbl_active_peers_title = QLabel("ACTIVE PEERS")
        lbl_active_peers_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold; text-align: right; margin-top: 10px;")
        self.lbl_active_peers = QLabel("0")
        self.lbl_active_peers.setStyleSheet("color: #fff; font-size: 18px; font-weight: bold;")

        status_box.addWidget(lbl_status_title)
        status_box.addWidget(self.lbl_status)
        status_box.addWidget(lbl_active_peers_title)
        status_box.addWidget(self.lbl_active_peers)

        header.addLayout(stats_box)
        header.addStretch()
        header.addLayout(status_box)
        layout.addLayout(header)

        # Separator Line
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("background-color: #222; max-height: 1px; border: none;")
        layout.addWidget(line)

        # --- LOGS AREA ---
        layout.addWidget(QLabel("TELEMETRY"))
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(15)
        self.logs.setGraphicsEffect(shadow)
        layout.addWidget(self.logs)

        # --- CONTROL BUTTON ---
        self.btn = QPushButton("INITIALIZE SYSTEM")
        self.btn.setFixedHeight(60)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.update_btn_style(False)
        self.btn.clicked.connect(self.toggle)
        layout.addWidget(self.btn)

        self.worker_thread = None
        self.node_active = False

        # Timer to poll Master for Balance/Stats
        self.poll_thread = PollThread()
        self.poll_thread.stats_sig.connect(self.update_stats)
        self.poll_thread.balance_sig.connect(self.update_balance)
        self.poll_thread.start()

    def update_btn_style(self, active):
        if active:
            self.btn.setStyleSheet("""
                QPushButton { background-color: #1a1a1a; color: #ff4444; border: 1px solid #ff4444; font-size: 14px; font-weight: bold; letter-spacing: 1px; border-radius: 6px; }
                QPushButton:hover { background-color: #2a1a1a; }
            """)
        else:
            self.btn.setStyleSheet("""
                QPushButton { background-color: #00E676; color: #000000; border: none; font-size: 14px; font-weight: bold; letter-spacing: 1px; border-radius: 6px; }
                QPushButton:hover { background-color: #00C853; }
            """)

    def toggle(self):
        if self.node_active:
            if self.worker_thread: self.worker_thread.stop()
            self.node_active = False
            self.btn.setText("INITIALIZE SYSTEM")
            self.update_btn_style(False)
            self.lbl_status.setText("STANDBY")
            self.lbl_status.setStyleSheet("color: #666; font-size: 18px; font-weight: bold;")
        else:
            self.worker_thread = NodeThread()
            self.worker_thread.log_sig.connect(self.logs.append)
            self.worker_thread.worker_id_sig.connect(self.poll_thread.set_worker_id)
            self.worker_thread.start()
            self.node_active = True
            self.btn.setText("TERMINATE PROCESS")
            self.update_btn_style(True)
            self.lbl_status.setText("COMPUTING")
            self.lbl_status.setStyleSheet("color: #00E676; font-size: 18px; font-weight: bold;")

    def update_stats(self, stats):
        self.lbl_active_peers.setText(str(stats.get("active_peers", 0)))
        self.lbl_swarm.setText(f"{stats.get('swarm_velocity', 0):.2f} step/s")

    def update_balance(self, amount):
        self.lbl_earn.setText(f"$ {amount:.5f}")

# --- POLLING THREAD ---
class PollThread(QThread):
    stats_sig = pyqtSignal(dict)
    balance_sig = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.worker_id = None

    def set_worker_id(self, wid):
        self.worker_id = wid

    def run(self):
        while True:
            try:
                # 1. Global Stats
                r = requests.get("http://127.0.0.1:5000/api/job/current", timeout=2)
                if r.status_code == 200:
                    self.stats_sig.emit(r.json())

                # 2. Wallet Balance (if we know who we are)
                if self.worker_id:
                    r2 = requests.get(f"http://127.0.0.1:5000/api/wallet/balance/{self.worker_id}", timeout=2)
                    if r2.status_code == 200:
                        self.balance_sig.emit(r2.json().get('balance', 0.0))
            except:
                pass
            time.sleep(5)

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        win = ElysiumGUI()
        win.show()
        sys.exit(app.exec_())
    except Exception as e:
        print("Fatal Error:", e)
