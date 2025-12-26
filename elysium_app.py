import sys
import os
import subprocess
import requests
import time
import psutil
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel,
                             QVBoxLayout, QWidget, QTextEdit, QHBoxLayout, QFrame,
                             QGraphicsDropShadowEffect, QStackedWidget, QListWidget,
                             QListWidgetItem, QProgressBar, QGridLayout, QScrollArea, QSizePolicy)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QSize, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QBrush, QPen

# --- CONFIGURAÇÃO DE REDE ---
FORCE_MASTER_IP = "127.0.0.1"

# --- THREADS (LOGIC) ---

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
                        parts = l.split("Registering ")[1].split(" with")
                        wid = parts[0].strip()
                        self.worker_id_sig.emit(wid)
                    except: pass

        except Exception as e:
            self.log_sig.emit(f"❌ CRITICAL FAILURE: {e}")

    def stop(self):
        self.running = False
        if self.process: self.process.terminate()

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

class HardwareThread(QThread):
    hw_sig = pyqtSignal(dict)

    def run(self):
        while True:
            try:
                # CPU / RAM
                cpu = psutil.cpu_percent()
                ram = psutil.virtual_memory().percent

                # GPU (Mock/Nvidia-SMI would go here)
                # Since psutil doesn't do GPU, we simulate "VRAM" based on load or keep static
                # unless we invoke nvidia-smi. For robustness in MVP without extra deps:
                gpu_load = cpu * 0.8 # Placeholder correlation
                vram_usage = 45.0 # Placeholder

                self.hw_sig.emit({
                    "cpu": cpu, "ram": ram, "gpu": gpu_load, "vram": vram_usage
                })
            except: pass
            time.sleep(2)

# --- UI COMPONENTS ---

class ModernCard(QFrame):
    def __init__(self, parent=None, dark=False):
        super().__init__(parent)
        self.setStyleSheet(f"""
            ModernCard {{
                background-color: {('#1c1c1e' if not dark else '#0a0a0a')};
                border: 1px solid #2c2c2e;
                border-radius: 12px;
            }}
        """)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 100))
        self.setGraphicsEffect(shadow)

class SidebarButton(QPushButton):
    def __init__(self, text, icon_char, active=False):
        super().__init__()
        self.setCheckable(True)
        self.setText(f"  {icon_char}   {text}")
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        self.update_style(active)

    def update_style(self, active):
        if active:
            self.setStyleSheet("""
                QPushButton {
                    background-color: rgba(0, 230, 118, 0.1);
                    color: #00E676;
                    border: none;
                    border-radius: 8px;
                    text-align: left;
                    padding-left: 20px;
                    font-size: 14px;
                    font-weight: bold;
                }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #8e8e93;
                    border: none;
                    border-radius: 8px;
                    text-align: left;
                    padding-left: 20px;
                    font-size: 14px;
                }
                QPushButton:hover {
                    color: #ffffff;
                    background-color: rgba(255,255,255,0.05);
                }
            """)

# --- TABS ---

class DashboardTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)

        # Top Row: Balance & Start
        row1 = QHBoxLayout()

        # Balance Card
        self.bal_card = ModernCard()
        bal_layout = QVBoxLayout(self.bal_card)
        lbl_bal_title = QLabel("TOTAL BALANCE")
        lbl_bal_title.setStyleSheet("color: #8e8e93; font-size: 12px; letter-spacing: 1px;")
        self.lbl_balance = QLabel("$ 0.0000")
        self.lbl_balance.setStyleSheet("color: #ffffff; font-size: 48px; font-weight: 600;")
        bal_layout.addWidget(lbl_bal_title)
        bal_layout.addWidget(self.lbl_balance)
        row1.addWidget(self.bal_card, 2)

        # Start Button Card
        self.start_card = ModernCard()
        start_layout = QVBoxLayout(self.start_card)
        start_layout.setAlignment(Qt.AlignCenter)
        self.btn_start = QPushButton("START MINING")
        self.btn_start.setFixedSize(200, 60)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #1c1c1e;
                color: #00E676;
                border: 2px solid #00E676;
                border-radius: 30px;
                font-size: 16px;
                font-weight: bold;
                letter-spacing: 1px;
            }
            QPushButton:hover {
                background-color: #00E676;
                color: #000000;
            }
        """)
        start_layout.addWidget(self.btn_start)
        row1.addWidget(self.start_card, 1)

        layout.addLayout(row1)

        # Middle Row: Metrics
        row2 = QHBoxLayout()

        # Active Peers
        p_card = ModernCard()
        p_layout = QVBoxLayout(p_card)
        p_layout.addWidget(QLabel("ACTIVE PEERS"))
        self.lbl_peers = QLabel("0")
        self.lbl_peers.setStyleSheet("color: #00E676; font-size: 32px; font-weight: bold;")
        p_layout.addWidget(self.lbl_peers)
        row2.addWidget(p_card)

        # Velocity
        v_card = ModernCard()
        v_layout = QVBoxLayout(v_card)
        v_layout.addWidget(QLabel("GLOBAL VELOCITY"))
        self.lbl_velocity = QLabel("0.00 step/s")
        self.lbl_velocity.setStyleSheet("color: #fff; font-size: 32px; font-weight: bold;")
        v_layout.addWidget(self.lbl_velocity)
        row2.addWidget(v_card)

        layout.addLayout(row2)
        layout.addStretch()

class TerminalTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.term = QTextEdit()
        self.term.setReadOnly(True)
        self.term.setStyleSheet("""
            QTextEdit {
                background-color: #0a0a0a;
                color: #00E676;
                font-family: 'Consolas', monospace;
                font-size: 12px;
                border: 1px solid #2c2c2e;
                border-radius: 12px;
                padding: 15px;
            }
        """)
        layout.addWidget(self.term)

class HardwareTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)

        # Helper to make a bar
        def make_bar(title):
            w = ModernCard()
            l = QVBoxLayout(w)
            h = QHBoxLayout()
            lbl = QLabel(title)
            val = QLabel("0%")
            h.addWidget(lbl)
            h.addStretch()
            h.addWidget(val)
            l.addLayout(h)

            bar = QProgressBar()
            bar.setFixedHeight(8)
            bar.setTextVisible(False)
            bar.setStyleSheet("""
                QProgressBar {
                    background-color: #2c2c2e;
                    border-radius: 4px;
                }
                QProgressBar::chunk {
                    background-color: #00E676;
                    border-radius: 4px;
                }
            """)
            l.addWidget(bar)
            return w, val, bar

        self.cpu_card, self.cpu_val, self.cpu_bar = make_bar("CPU Usage")
        self.ram_card, self.ram_val, self.ram_bar = make_bar("RAM Usage")
        self.gpu_card, self.gpu_val, self.gpu_bar = make_bar("GPU Load (Simulated)")

        layout.addWidget(self.cpu_card)
        layout.addWidget(self.ram_card)
        layout.addWidget(self.gpu_card)
        layout.addStretch()

class WalletTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Header
        h = QLabel("Recent Transactions")
        h.setStyleSheet("font-size: 18px; font-weight: bold; margin-bottom: 10px;")
        layout.addWidget(h)

        self.list = QListWidget()
        self.list.setStyleSheet("""
            QListWidget {
                background-color: #1c1c1e;
                border: 1px solid #2c2c2e;
                border-radius: 12px;
                outline: none;
            }
            QListWidget::item {
                padding: 15px;
                border-bottom: 1px solid #2c2c2e;
            }
        """)
        layout.addWidget(self.list)

        # Add dummy history for MVP visual
        for i in range(5):
            self.add_tx(f"Mining Reward #{1024+i}", "+ $0.0005", "Completed")

    def add_tx(self, desc, amount, status):
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 60))

        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(10, 0, 10, 0)

        lbl_desc = QLabel(desc)
        lbl_desc.setStyleSheet("color: #fff; font-weight: 500;")

        lbl_amt = QLabel(amount)
        lbl_amt.setStyleSheet("color: #00E676; font-weight: bold;")

        l.addWidget(lbl_desc)
        l.addStretch()
        l.addWidget(lbl_amt)

        self.list.addItem(item)
        self.list.setItemWidget(item, w)

# --- MAIN WINDOW ---

class ElysiumApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ELYSIUM COMPUTE NODE")
        self.resize(1100, 700)

        # Global Style
        self.setStyleSheet("""
            QMainWindow { background-color: #0a0a0a; }
            QLabel { font-family: 'Segoe UI', sans-serif; color: #8e8e93; }
        """)

        # Main Layout
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(240)
        self.sidebar.setStyleSheet("background-color: #0f0f10; border-right: 1px solid #1f1f20;")
        side_layout = QVBoxLayout(self.sidebar)
        side_layout.setContentsMargins(20, 40, 20, 20)
        side_layout.setSpacing(10)

        # Logo
        logo = QLabel("ELYSIUM")
        logo.setStyleSheet("color: #fff; font-size: 20px; font-weight: 900; letter-spacing: 2px; margin-bottom: 20px;")
        side_layout.addWidget(logo)

        # Buttons
        self.btn_dash = SidebarButton("Dashboard", "📊", True)
        self.btn_term = SidebarButton("Terminal", "💻")
        self.btn_hw = SidebarButton("Hardware", "🔋")
        self.btn_wall = SidebarButton("Wallet", "💰")

        for b in [self.btn_dash, self.btn_term, self.btn_hw, self.btn_wall]:
            side_layout.addWidget(b)
            b.clicked.connect(self.nav_click)

        side_layout.addStretch()
        main_layout.addWidget(self.sidebar)

        # Content Area
        self.stack = QStackedWidget()
        self.stack.setContentsMargins(30, 30, 30, 30)

        self.tab_dash = DashboardTab()
        self.tab_term = TerminalTab()
        self.tab_hw = HardwareTab()
        self.tab_wall = WalletTab()

        self.stack.addWidget(self.tab_dash)
        self.stack.addWidget(self.tab_term)
        self.stack.addWidget(self.tab_hw)
        self.stack.addWidget(self.tab_wall)

        main_layout.addWidget(self.stack)

        # Logic Wiring
        self.tab_dash.btn_start.clicked.connect(self.toggle_mining)

        # Threads
        self.node_active = False
        self.node_thread = None

        self.poll_thread = PollThread()
        self.poll_thread.stats_sig.connect(self.on_stats)
        self.poll_thread.balance_sig.connect(self.on_balance)
        self.poll_thread.start()

        self.hw_thread = HardwareThread()
        self.hw_thread.hw_sig.connect(self.on_hw)
        self.hw_thread.start()

    def nav_click(self):
        sender = self.sender()
        # Reset styles
        for b in [self.btn_dash, self.btn_term, self.btn_hw, self.btn_wall]:
            b.update_style(False)

        sender.update_style(True)

        if sender == self.btn_dash: self.stack.setCurrentIndex(0)
        elif sender == self.btn_term: self.stack.setCurrentIndex(1)
        elif sender == self.btn_hw: self.stack.setCurrentIndex(2)
        elif sender == self.btn_wall: self.stack.setCurrentIndex(3)

    def toggle_mining(self):
        if not self.node_active:
            # Start
            self.node_thread = NodeThread()
            self.node_thread.log_sig.connect(self.tab_term.term.append)
            self.node_thread.worker_id_sig.connect(self.poll_thread.set_worker_id)
            self.node_thread.start()

            self.node_active = True
            self.tab_dash.btn_start.setText("STOP MINING")
            self.tab_dash.btn_start.setStyleSheet("""
                QPushButton {
                    background-color: #1c1c1e;
                    color: #ff4444;
                    border: 2px solid #ff4444;
                    border-radius: 30px;
                    font-size: 16px; font-weight: bold;
                }
                QPushButton:hover { background-color: #ff4444; color: #fff; }
            """)
        else:
            # Stop
            if self.node_thread: self.node_thread.stop()
            self.node_active = False
            self.tab_dash.btn_start.setText("START MINING")
            self.tab_dash.btn_start.setStyleSheet("""
                QPushButton {
                    background-color: #1c1c1e;
                    color: #00E676;
                    border: 2px solid #00E676;
                    border-radius: 30px;
                    font-size: 16px; font-weight: bold;
                }
                QPushButton:hover { background-color: #00E676; color: #fff; }
            """)

    def on_stats(self, data):
        self.tab_dash.lbl_peers.setText(str(data.get("active_peers", 0)))
        self.tab_dash.lbl_velocity.setText(f"{data.get('swarm_velocity', 0.0):.2f} step/s")

    def on_balance(self, amount):
        self.tab_dash.lbl_balance.setText(f"$ {amount:.4f}")

    def on_hw(self, data):
        self.tab_hw.cpu_bar.setValue(int(data['cpu']))
        self.tab_hw.cpu_val.setText(f"{data['cpu']}%")

        self.tab_hw.ram_bar.setValue(int(data['ram']))
        self.tab_hw.ram_val.setText(f"{data['ram']}%")

        self.tab_hw.gpu_bar.setValue(int(data['gpu']))
        self.tab_hw.gpu_val.setText(f"{int(data['gpu'])}%")

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        font_db = QFont("Segoe UI", 10)
        app.setFont(font_db)
        win = ElysiumApp()
        win.show()
        sys.exit(app.exec_())
    except Exception as e:
        print("Fatal Error:", e)
