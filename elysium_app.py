import sys
import os
import subprocess
import requests
import time
import psutil
import json
from pathlib import Path
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel,
                             QVBoxLayout, QWidget, QTextEdit, QHBoxLayout, QFrame,
                             QGraphicsDropShadowEffect, QStackedWidget, QListWidget,
                             QListWidgetItem, QProgressBar, QLineEdit)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QSize
from PyQt5.QtGui import QColor, QFont, QIcon

# --- CONFIG ---
FORCE_MASTER_IP = "127.0.0.1"
CONFIG_FILE = Path.cwd() / "elysium_config.json"

def load_config():
    if CONFIG_FILE.exists():
        try: return json.load(open(CONFIG_FILE))
        except: pass
    return {}

def save_config(key, value):
    cfg = load_config()
    cfg[key] = value
    json.dump(cfg, open(CONFIG_FILE, 'w'))

# --- THREADS ---

class NodeThread(QThread):
    log_sig = pyqtSignal(str)
    worker_id_sig = pyqtSignal(str)
    wallet_id_sig = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.process = None
        self.running = True

    def run(self):
        self.log_sig.emit("🔄 STARTING PRODUCTION ALPHA NODE...")
        master_url = f"http://{FORCE_MASTER_IP}:5000" if FORCE_MASTER_IP != "AUTO" else "http://127.0.0.1:5000"

        # Determine Python Executable
        if os.name == 'nt': python_exec = sys.executable
        else: python_exec = "python3"

        cmd = [python_exec, "elysium_node.py", "--master_url", master_url]

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

                # Detect IDs from logs: "[INIT] 📝 Registering {wid} (Wallet: {wallet})..."
                if "[INIT] 📝 Registering" in l and "(Wallet:" in l:
                    try:
                        # Parse: Registering node_xxx (Wallet: ELYS-YYY)
                        p1 = l.split("Registering ")[1]
                        wid = p1.split(" ")[0]
                        p2 = l.split("(Wallet: ")[1]
                        wallet = p2.split(")")[0]
                        self.worker_id_sig.emit(wid)
                        self.wallet_id_sig.emit(wallet)

                        # Auto-save wallet ID
                        save_config("wallet_id", wallet)
                    except: pass

        except Exception as e:
            self.log_sig.emit(f"❌ CRITICAL FAILURE: {e}")

    def stop(self):
        self.running = False
        if self.process: self.process.terminate()

class PollThread(QThread):
    balance_sig = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.wallet_id = None

    def set_wallet_id(self, wid):
        self.wallet_id = wid

    def run(self):
        while True:
            try:
                # Use stored wallet ID if thread state is empty
                wid = self.wallet_id or load_config().get("wallet_id")

                if wid:
                    # In V4.1 Master, balance endpoint uses wallet_id
                    r = requests.get(f"http://127.0.0.1:5000/api/wallet/balance/{wid}", timeout=2)
                    if r.status_code == 200:
                        self.balance_sig.emit(r.json().get('balance', 0.0))
            except: pass
            time.sleep(5)

class HardwareThread(QThread):
    hw_sig = pyqtSignal(dict)

    def run(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            has_gpu = True
        except: has_gpu = False

        while True:
            try:
                # CPU / RAM
                cpu = psutil.cpu_percent()
                ram = psutil.virtual_memory().percent
                gpu_load = 0
                vram_usage = 0
                temp = 0
                fan = 0
                power = 0

                if has_gpu:
                    try:
                        h = pynvml.nvmlDeviceGetHandleByIndex(0)
                        util = pynvml.nvmlDeviceGetUtilizationRates(h)
                        gpu_load = util.gpu
                        mem = pynvml.nvmlDeviceGetMemoryInfo(h)
                        vram_usage = (mem.used / mem.total) * 100
                        temp = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
                        try: fan = pynvml.nvmlDeviceGetFanSpeed(h)
                        except: fan = 0
                        try: power = pynvml.nvmlDeviceGetPowerUsage(h) / 1000.0
                        except: power = 0
                    except: pass

                self.hw_sig.emit({
                    "cpu": cpu, "ram": ram, "gpu": gpu_load, "vram": vram_usage,
                    "temp": temp, "fan": fan, "power": power, "has_gpu": has_gpu
                })
            except: pass
            time.sleep(2)

# --- UI COMPONENTS ---

class ModernCard(QFrame):
    def __init__(self, parent=None, dark=False):
        super().__init__(parent)
        bg = '#0a0a0a' if dark else '#1c1c1e'
        self.setStyleSheet(f"background-color: {bg}; border: 1px solid #2c2c2e; border-radius: 12px;")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20); shadow.setYOffset(4); shadow.setColor(QColor(0,0,0,100))
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
            self.setStyleSheet("background-color: rgba(0,230,118,0.1); color: #00E676; border: none; border-radius: 8px; text-align: left; padding-left: 20px; font-weight: bold;")
        else:
            self.setStyleSheet("background-color: transparent; color: #8e8e93; border: none; border-radius: 8px; text-align: left; padding-left: 20px;")

# --- TABS ---

class DashboardTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(20)

        # Status Card
        s_card = ModernCard()
        s_layout = QVBoxLayout(s_card)
        s_layout.addWidget(QLabel("NODE STATUS"))
        self.lbl_status = QLabel("STANDBY")
        self.lbl_status.setStyleSheet("color: #8e8e93; font-size: 24px; font-weight: bold;")
        s_layout.addWidget(self.lbl_status)
        layout.addWidget(s_card)

        # Start Button
        self.btn_start = QPushButton("INITIALIZE NODE")
        self.btn_start.setFixedSize(300, 80)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setStyleSheet("background-color: #1c1c1e; color: #00E676; border: 2px solid #00E676; border-radius: 40px; font-size: 18px; font-weight: bold;")

        btn_container = QHBoxLayout()
        btn_container.addStretch()
        btn_container.addWidget(self.btn_start)
        btn_container.addStretch()
        layout.addLayout(btn_container)

        layout.addStretch()

class HardwareTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        self.lbl_mode = QLabel("DETECTING HARDWARE...")
        self.lbl_mode.setStyleSheet("font-size: 14px; color: #fff; margin-bottom: 20px;")
        layout.addWidget(self.lbl_mode)

        def make_bar(title, unit):
            w = ModernCard()
            l = QVBoxLayout(w)
            h = QHBoxLayout()
            h.addWidget(QLabel(title)); h.addStretch();
            val = QLabel("0"); h.addWidget(val); h.addWidget(QLabel(unit))
            l.addLayout(h)
            bar = QProgressBar(); bar.setFixedHeight(6); bar.setTextVisible(False)
            bar.setStyleSheet("QProgressBar{background:#2c2c2e;border-radius:3px} QProgressBar::chunk{background:#00E676;border-radius:3px}")
            l.addWidget(bar)
            return w, val, bar

        self.gpu_card, self.gpu_val, self.gpu_bar = make_bar("GPU Utilization", "%")
        self.vram_card, self.vram_val, self.vram_bar = make_bar("VRAM Usage", "%")
        self.temp_card, self.temp_val, self.temp_bar = make_bar("Temperature", "°C")
        self.fan_card, self.fan_val, self.fan_bar = make_bar("Fan Speed", "%")
        self.power_card, self.power_val, self.power_bar = make_bar("Power Usage", "W")

        layout.addWidget(self.gpu_card)
        layout.addWidget(self.vram_card)
        layout.addWidget(self.temp_card)
        layout.addWidget(self.fan_card)
        layout.addWidget(self.power_card)
        layout.addStretch()

class WalletTab(QWidget):
    withdraw_signal = pyqtSignal(str) # address

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Balance Card
        b_card = ModernCard()
        b_layout = QVBoxLayout(b_card)
        b_layout.addWidget(QLabel("UNPAID BALANCE"))
        self.lbl_bal = QLabel("$ 0.0000")
        self.lbl_bal.setStyleSheet("color: #fff; font-size: 48px; font-weight: 600;")
        b_layout.addWidget(self.lbl_bal)
        layout.addWidget(b_card)

        # Address Input
        layout.addWidget(QLabel("PAYOUT ADDRESS (USDT/BTC/PIX)"))
        self.input_addr = QLineEdit()
        self.input_addr.setPlaceholderText("Enter your crypto address...")
        self.input_addr.setStyleSheet("padding: 12px; background: #1c1c1e; color: #fff; border: 1px solid #2c2c2e; border-radius: 8px;")
        self.input_addr.setText(load_config().get("payout_address", ""))
        self.input_addr.textChanged.connect(lambda: save_config("payout_address", self.input_addr.text()))
        layout.addWidget(self.input_addr)

        # Progress
        self.lbl_prog = QLabel("Progress: $0.00 / $10.00")
        layout.addWidget(self.lbl_prog)
        self.prog_bar = QProgressBar()
        self.prog_bar.setFixedHeight(10)
        self.prog_bar.setTextVisible(False)
        self.prog_bar.setStyleSheet("QProgressBar{background:#2c2c2e;border-radius:5px} QProgressBar::chunk{background:#00E676;border-radius:5px}")
        layout.addWidget(self.prog_bar)

        # Withdraw Button
        self.btn_withdraw = QPushButton("Min. $10.00 to Withdraw")
        self.btn_withdraw.setFixedHeight(50)
        self.btn_withdraw.setEnabled(False)
        self.btn_withdraw.setStyleSheet("background: #2c2c2e; color: #8e8e93; border: none; border-radius: 8px; font-weight: bold;")
        self.btn_withdraw.clicked.connect(self.request_payout)
        layout.addWidget(self.btn_withdraw)

        layout.addStretch()
        self.current_balance = 0.0

    def update_balance(self, amount):
        self.current_balance = amount
        self.lbl_bal.setText(f"$ {amount:.4f}")

        # Progress Logic
        p = min(100, int((amount / 10.0) * 100))
        self.prog_bar.setValue(p)
        self.lbl_prog.setText(f"Progress: ${amount:.2f} / $10.00")

        if amount >= 10.0:
            self.btn_withdraw.setEnabled(True)
            self.btn_withdraw.setText("REQUEST PAYOUT")
            self.btn_withdraw.setStyleSheet("background: #00E676; color: #000; border: none; border-radius: 8px; font-weight: bold;")
        else:
            self.btn_withdraw.setEnabled(False)
            self.btn_withdraw.setText(f"Min. $10.00 ({((amount/10)*100):.0f}%)")
            self.btn_withdraw.setStyleSheet("background: #2c2c2e; color: #8e8e93; border: none; border-radius: 8px; font-weight: bold;")

    def request_payout(self):
        addr = self.input_addr.text()
        # Retrieve wallet_id from parent app context or file
        wid = load_config().get("wallet_id")
        if not addr or not wid: return
        try:
            r = requests.post("http://127.0.0.1:5000/api/wallet/withdraw", data={"address": addr, "wallet_id": wid})
            if r.status_code == 200:
                self.btn_withdraw.setText("REQUEST SENT")
                self.btn_withdraw.setEnabled(False)
        except: pass

class TerminalTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        self.term = QTextEdit()
        self.term.setReadOnly(True)
        self.term.setStyleSheet("background-color: #0a0a0a; color: #00E676; font-family: 'Consolas', monospace; border: 1px solid #2c2c2e; border-radius: 12px; padding: 15px;")
        layout.addWidget(self.term)

# --- MAIN WINDOW ---

class ElysiumApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ELYSIUM PROVIDER CLIENT")
        self.resize(1000, 700)
        self.setStyleSheet("QMainWindow { background-color: #0a0a0a; } QLabel { font-family: 'Segoe UI', sans-serif; color: #8e8e93; }")

        central = QWidget(); self.setCentralWidget(central)
        main_layout = QHBoxLayout(central); main_layout.setContentsMargins(0,0,0,0); main_layout.setSpacing(0)

        # Sidebar
        sidebar = QWidget(); sidebar.setFixedWidth(240); sidebar.setStyleSheet("background:#0f0f10; border-right:1px solid #1f1f20;")
        sl = QVBoxLayout(sidebar); sl.setContentsMargins(20,40,20,20); sl.setSpacing(10)
        logo = QLabel("ELYSIUM"); logo.setStyleSheet("color:#fff; font-size:20px; font-weight:900; margin-bottom:20px")
        sl.addWidget(logo)

        self.btn_dash = SidebarButton("Dashboard", "⚡", True)
        self.btn_hw = SidebarButton("Hardware", "🔋")
        self.btn_wall = SidebarButton("Wallet", "💰")
        self.btn_term = SidebarButton("Terminal", "💻")

        for b in [self.btn_dash, self.btn_hw, self.btn_wall, self.btn_term]:
            sl.addWidget(b); b.clicked.connect(self.nav)
        sl.addStretch()
        main_layout.addWidget(sidebar)

        # Stack
        self.stack = QStackedWidget(); self.stack.setContentsMargins(30,30,30,30)
        self.tab_dash = DashboardTab()
        self.tab_hw = HardwareTab()
        self.tab_wall = WalletTab()
        self.tab_term = TerminalTab()

        for t in [self.tab_dash, self.tab_hw, self.tab_wall, self.tab_term]: self.stack.addWidget(t)
        main_layout.addWidget(self.stack)

        # Logic
        self.node_active = False
        self.node_thread = None
        self.tab_dash.btn_start.clicked.connect(self.toggle_node)

        self.poll = PollThread()
        self.poll.balance_sig.connect(self.tab_wall.update_balance)
        self.poll.start()

        self.hw = HardwareThread()
        self.hw.hw_sig.connect(self.on_hw)
        self.hw.start()

    def nav(self):
        s = self.sender()
        for b in [self.btn_dash, self.btn_hw, self.btn_wall, self.btn_term]: b.update_style(False)
        s.update_style(True)
        if s == self.btn_dash: self.stack.setCurrentIndex(0)
        elif s == self.btn_hw: self.stack.setCurrentIndex(1)
        elif s == self.btn_wall: self.stack.setCurrentIndex(2)
        elif s == self.btn_term: self.stack.setCurrentIndex(3)

    def toggle_node(self):
        if not self.node_active:
            self.node_thread = NodeThread()
            self.node_thread.log_sig.connect(self.tab_term.term.append)
            self.node_thread.wallet_id_sig.connect(self.poll.set_wallet_id)
            self.node_thread.start()
            self.node_active = True

            self.tab_dash.lbl_status.setText("ONLINE")
            self.tab_dash.lbl_status.setStyleSheet("color:#00E676; font-size:24px; font-weight:bold")
            self.tab_dash.btn_start.setText("STOP NODE")
            self.tab_dash.btn_start.setStyleSheet("background:#1c1c1e; color:#ff4444; border:2px solid #ff4444; border-radius:40px; font-size:18px; font-weight:bold")
        else:
            if self.node_thread: self.node_thread.stop()
            self.node_active = False
            self.tab_dash.lbl_status.setText("STANDBY")
            self.tab_dash.lbl_status.setStyleSheet("color:#8e8e93; font-size:24px; font-weight:bold")
            self.tab_dash.btn_start.setText("INITIALIZE NODE")
            self.tab_dash.btn_start.setStyleSheet("background:#1c1c1e; color:#00E676; border:2px solid #00E676; border-radius:40px; font-size:18px; font-weight:bold")

    def on_hw(self, d):
        if d['has_gpu']:
            self.tab_hw.lbl_mode.setText("🟢 GPU MODE ACTIVE (High Efficiency)")
            self.tab_hw.lbl_mode.setStyleSheet("color:#00E676; font-weight:bold")
            self.tab_hw.gpu_bar.setValue(int(d['gpu']))
            self.tab_hw.gpu_val.setText(f"{d['gpu']}%")
            self.tab_hw.vram_bar.setValue(int(d['vram']))
            self.tab_hw.vram_val.setText(f"{d['vram']:.1f}%")
            self.tab_hw.temp_bar.setValue(min(100, int(d['temp'])))
            self.tab_hw.temp_val.setText(f"{d['temp']}")
            self.tab_hw.fan_bar.setValue(int(d['fan']))
            self.tab_hw.fan_val.setText(f"{d['fan']}")
            self.tab_hw.power_bar.setValue(min(100, int(d['power']/300*100))) # Approx scale
            self.tab_hw.power_val.setText(f"{d['power']}")
        else:
            self.tab_hw.lbl_mode.setText("⚠️ CPU MODE (Low Efficiency)")
            self.tab_hw.lbl_mode.setStyleSheet("color:#ffb700; font-weight:bold")
            self.tab_hw.gpu_bar.setValue(int(d['cpu'])) # Show CPU on GPU bar as fallback
            self.tab_hw.gpu_val.setText(f"CPU: {d['cpu']}%")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = ElysiumApp()
    win.show()
    sys.exit(app.exec_())
