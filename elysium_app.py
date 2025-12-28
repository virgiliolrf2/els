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
                             QListWidgetItem, QProgressBar, QLineEdit, QComboBox,
                             QSpacerItem, QSizePolicy)
from PyQt5.QtCore import QThread, pyqtSignal, Qt, QSize, QPoint
from PyQt5.QtGui import QColor, QFont, QIcon, QCursor
import hashlib
import elysium_security

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

def fetch_master_key():
    """Downloads the Master Public Key for encryption."""
    try:
        url = f"http://{FORCE_MASTER_IP}:5000/api/config/public_key"
        print(f"[DEBUG] Fetching Master Key from {url}...")
        r = requests.get(url, timeout=5)
        print(f"[DEBUG] Status Code: {r.status_code}")
        if r.status_code == 200:
            with open("master_public_key.pem", "wb") as f:
                f.write(r.content)
            print("[DEBUG] Master Key saved to master_public_key.pem")
            return True
        else:
            print(f"[DEBUG] Failed to fetch key: {r.text}")
    except Exception as e:
        print(f"[DEBUG] Exception in fetch_master_key: {e}")
    return False

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
    def __init__(self, parent=None, color="#FFFFFF"):
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {color}; border: none; border-radius: 16px;")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25); shadow.setYOffset(5); shadow.setColor(QColor(0,0,0,15))
        self.setGraphicsEffect(shadow)

class TitleBar(QFrame):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.setFixedHeight(45)
        self.setStyleSheet("background-color: #FFFFFF; border-bottom: 1px solid #F3F4F6;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 0, 15, 0)

        # Logo
        self.logo = QLabel("ELYSIUM")
        self.logo.setStyleSheet("color: #10B981; font-weight: 900; font-size: 14px; letter-spacing: 1px;")
        layout.addWidget(self.logo)

        layout.addStretch()

        # Window Controls
        btn_style = """
            QPushButton { background: transparent; border: none; font-size: 14px; font-weight: bold; color: #6B7280; width: 30px; height: 30px; border-radius: 4px; }
            QPushButton:hover { background: #F3F4F6; color: #1F2937; }
        """

        self.btn_min = QPushButton("─")
        self.btn_min.setStyleSheet(btn_style)
        self.btn_min.clicked.connect(self.parent.showMinimized)

        self.btn_close = QPushButton("✕")
        self.btn_close.setStyleSheet(btn_style.replace("#F3F4F6", "#FEE2E2").replace("#1F2937", "#EF4444"))
        self.btn_close.clicked.connect(self.parent.close)

        layout.addWidget(self.btn_min)
        layout.addWidget(self.btn_close)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.parent.oldPos = event.globalPos()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            delta = QPoint(event.globalPos() - self.parent.oldPos)
            self.parent.move(self.parent.x() + delta.x(), self.parent.y() + delta.y())
            self.parent.oldPos = event.globalPos()

class AuthInput(QLineEdit):
    def __init__(self, placeholder, echo=QLineEdit.Normal):
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setEchoMode(echo)
        self.setFixedHeight(50)
        self.setStyleSheet("""
            QLineEdit {
                background-color: #F3F4F6;
                border: none;
                border-radius: 8px;
                color: #1F2937;
                padding: 0 15px;
                font-size: 14px;
            }
            QLineEdit:focus {
                background-color: #FFFFFF;
                border: 2px solid #10B981;
            }
        """)

class AuthButton(QPushButton):
    def __init__(self, text, primary=True):
        super().__init__(text)
        self.setFixedHeight(50)
        self.setCursor(Qt.PointingHandCursor)
        if primary:
            self.setStyleSheet("""
                QPushButton {
                    background-color: #10B981;
                    color: #FFFFFF;
                    border: none;
                    border-radius: 8px;
                    font-weight: bold;
                    font-size: 14px;
                }
                QPushButton:hover { background-color: #059669; }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #6B7280;
                    border: none;
                    font-size: 13px;
                }
                QPushButton:hover { color: #111827; }
            """)

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
                    background-color: #ECFDF5;
                    color: #10B981;
                    border: none;
                    border-radius: 8px;
                    text-align: left;
                    padding-left: 20px;
                    font-weight: bold;
                }
            """)
        else:
            self.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    color: #6B7280;
                    border: none;
                    border-radius: 8px;
                    text-align: left;
                    padding-left: 20px;
                }
                QPushButton:hover { background-color: #F9FAFB; color: #374151; }
            """)

# --- AUTH WIDGETS ---

class LoginWidget(QWidget):
    switch_signal = pyqtSignal()
    success_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        l = QVBoxLayout(self)
        l.setAlignment(Qt.AlignCenter)

        card = ModernCard()
        card.setFixedSize(400, 480)
        cl = QVBoxLayout(card)
        cl.setSpacing(20)
        cl.setContentsMargins(40,40,40,40)

        # Header
        logo = QLabel("Welcome Back")
        logo.setStyleSheet("color: #111827; font-size: 24px; font-weight: 800; margin-bottom: 5px;")
        logo.setAlignment(Qt.AlignCenter)
        cl.addWidget(logo)

        sub = QLabel("Sign in to your compute node")
        sub.setStyleSheet("color: #6B7280; font-size: 14px;")
        sub.setAlignment(Qt.AlignCenter)
        cl.addWidget(sub)

        cl.addSpacing(10)

        # Inputs
        self.email = AuthInput("Email Address")
        self.password = AuthInput("Password", QLineEdit.Password)
        cl.addWidget(self.email)
        cl.addWidget(self.password)

        # Submit
        self.btn = AuthButton("Sign In")
        self.btn.clicked.connect(self.do_login)
        cl.addWidget(self.btn)

        # Switch
        self.switch = AuthButton("Create an account", False)
        self.switch.clicked.connect(self.switch_signal.emit)
        cl.addWidget(self.switch)

        cl.addStretch()
        l.addWidget(card)

    def do_login(self):
        email = self.email.text()
        password = self.password.text()
        if not email or not password: return

        try:
            url = f"http://{FORCE_MASTER_IP}:5000/api/auth/login"
            r = requests.post(url, data={"email": email, "password": password})
            if r.status_code == 200:
                d = r.json()
                if 'wallet_id' in d:
                    save_config("wallet_id", d['wallet_id'])
                self.success_signal.emit()
            else:
                self.btn.setText("Login Failed")
        except:
            self.btn.setText("Connection Error")

class RegisterWidget(QWidget):
    switch_signal = pyqtSignal()
    success_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        l = QVBoxLayout(self)
        l.setAlignment(Qt.AlignCenter)

        card = ModernCard()
        card.setFixedSize(420, 600)
        cl = QVBoxLayout(card)
        cl.setSpacing(15)
        cl.setContentsMargins(40,40,40,40)

        logo = QLabel("Join Elysium")
        logo.setStyleSheet("color: #111827; font-size: 24px; font-weight: 800;")
        logo.setAlignment(Qt.AlignCenter)
        cl.addWidget(logo)

        self.email = AuthInput("Email Address")
        self.password = AuthInput("Password", QLineEdit.Password)

        # Payment Info
        self.pay_method = QComboBox()
        self.pay_method.addItems([
            "PIX",
            "USDT (TRC20)",
            "USDT (ERC20)",
            "USDT (Polygon)",
            "BTC",
            "PayPal",
            "Bank Transfer (SWIFT)"
        ])
        self.pay_method.setFixedHeight(50)
        self.pay_method.setStyleSheet("""
            QComboBox { background: #F3F4F6; color: #1F2937; border: none; border-radius: 8px; padding: 0 15px; font-size: 14px; }
            QComboBox::drop-down { border: none; }
        """)

        self.pay_addr = AuthInput("Payment Address / Key")

        cl.addWidget(self.email)
        cl.addWidget(self.password)
        cl.addWidget(QLabel("Payout Method:"))
        cl.addWidget(self.pay_method)
        cl.addWidget(self.pay_addr)

        self.btn = AuthButton("Encrypt & Register")
        self.btn.clicked.connect(self.do_register)
        cl.addWidget(self.btn)

        self.switch = AuthButton("Back to Login", False)
        self.switch.clicked.connect(self.switch_signal.emit)
        cl.addWidget(self.switch)

        cl.addStretch()
        l.addWidget(card)

    def do_register(self):
        email = self.email.text()
        password = self.password.text()
        method = self.pay_method.currentText()
        addr = self.pay_addr.text()

        if not email or not password or not addr: return

        self.btn.setText("Encrypting...")
        self.btn.setEnabled(False)
        QApplication.processEvents()

        try:
            if not fetch_master_key():
                raise Exception("Master Key Fetch Failed")

            with open("master_public_key.pem", "rb") as f: pub_pem = f.read()
            secure_id = elysium_security.generate_secure_wallet_id(method, addr, pub_pem)

            url = f"http://{FORCE_MASTER_IP}:5000/api/auth/signup"
            r = requests.post(url, data={"email": email, "password": password, "wallet_id": secure_id})

            if r.status_code == 200:
                save_config("wallet_id", secure_id)
                self.success_signal.emit()
            else:
                self.btn.setText("Failed")
                self.btn.setEnabled(True)
        except Exception as e:
            self.btn.setText(f"Error")
            print(f"Reg Error: {e}")
            self.btn.setEnabled(True)

# --- DASHBOARD TABS ---

class DashboardTab(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setSpacing(25)
        layout.setContentsMargins(0,0,0,0)

        # Status Section
        s_card = ModernCard()
        s_layout = QVBoxLayout(s_card)
        s_layout.setContentsMargins(30,30,30,30)

        s_layout.addWidget(QLabel("NODE STATUS"))
        self.lbl_status = QLabel("STANDBY")
        self.lbl_status.setStyleSheet("color: #9CA3AF; font-size: 32px; font-weight: 800;")
        s_layout.addWidget(self.lbl_status)
        layout.addWidget(s_card)

        # Start Button
        self.btn_start = QPushButton("INITIALIZE NODE")
        self.btn_start.setFixedSize(280, 280)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        # Circular Button
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #FFFFFF;
                color: #10B981;
                border: 4px solid #10B981;
                border-radius: 140px;
                font-size: 18px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #ECFDF5;
            }
        """)
        # Shadow for button
        shadow = QGraphicsDropShadowEffect(self.btn_start)
        shadow.setBlurRadius(40); shadow.setYOffset(10); shadow.setColor(QColor(16, 185, 129, 50))
        self.btn_start.setGraphicsEffect(shadow)

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
        self.lbl_mode.setStyleSheet("font-size: 16px; color: #374151; margin-bottom: 20px; font-weight: 600;")
        layout.addWidget(self.lbl_mode)

        def make_bar(title, unit):
            w = ModernCard()
            w.setFixedHeight(100)
            l = QVBoxLayout(w)
            l.setContentsMargins(20,20,20,20)
            h = QHBoxLayout()
            h.addWidget(QLabel(title)); h.addStretch();
            val = QLabel("0"); val.setStyleSheet("font-weight: bold; color: #1F2937;"); h.addWidget(val); h.addWidget(QLabel(unit))
            l.addLayout(h)
            bar = QProgressBar(); bar.setFixedHeight(8); bar.setTextVisible(False)
            bar.setStyleSheet("QProgressBar{background:#F3F4F6;border-radius:4px} QProgressBar::chunk{background:#10B981;border-radius:4px}")
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
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        # Balance Card
        b_card = ModernCard()
        b_layout = QVBoxLayout(b_card)
        b_layout.setContentsMargins(30,30,30,30)
        b_layout.addWidget(QLabel("UNPAID BALANCE"))
        self.lbl_bal = QLabel("$ 0.0000")
        self.lbl_bal.setStyleSheet("color: #111827; font-size: 56px; font-weight: 800;")
        b_layout.addWidget(self.lbl_bal)
        layout.addWidget(b_card)

        # Address Input
        layout.addWidget(QLabel("PAYOUT ADDRESS"))
        self.input_addr = QLineEdit()
        self.input_addr.setPlaceholderText("Enter your crypto address...")
        self.input_addr.setStyleSheet("padding: 15px; background: #FFFFFF; color: #1F2937; border: none; border-radius: 12px; font-size: 14px;")

        # Input Shadow
        ishadow = QGraphicsDropShadowEffect(self.input_addr)
        ishadow.setBlurRadius(15); ishadow.setYOffset(2); ishadow.setColor(QColor(0,0,0,10))
        self.input_addr.setGraphicsEffect(ishadow)

        self.input_addr.setText(load_config().get("payout_address", ""))
        self.input_addr.textChanged.connect(lambda: save_config("payout_address", self.input_addr.text()))
        layout.addWidget(self.input_addr)

        # Progress
        self.lbl_prog = QLabel("Progress: $0.00 / $10.00")
        self.lbl_prog.setStyleSheet("color: #6B7280; font-weight: 600; margin-top: 10px;")
        layout.addWidget(self.lbl_prog)

        self.prog_bar = QProgressBar()
        self.prog_bar.setFixedHeight(12)
        self.prog_bar.setTextVisible(False)
        self.prog_bar.setStyleSheet("QProgressBar{background:#E5E7EB;border-radius:6px} QProgressBar::chunk{background:#10B981;border-radius:6px}")
        layout.addWidget(self.prog_bar)

        # Withdraw Button
        self.btn_withdraw = QPushButton("Min. $10.00 to Withdraw")
        self.btn_withdraw.setFixedHeight(55)
        self.btn_withdraw.setEnabled(False)
        self.btn_withdraw.setStyleSheet("""
            QPushButton { background: #E5E7EB; color: #9CA3AF; border: none; border-radius: 12px; font-weight: bold; font-size: 14px; margin-top: 20px; }
        """)
        self.btn_withdraw.clicked.connect(self.request_payout)
        layout.addWidget(self.btn_withdraw)

        layout.addStretch()

    def update_balance(self, amount):
        self.lbl_bal.setText(f"$ {amount:.4f}")

        # Progress Logic
        p = min(100, int((amount / 10.0) * 100))
        self.prog_bar.setValue(p)
        self.lbl_prog.setText(f"Progress: ${amount:.2f} / $10.00")

        if amount >= 10.0:
            self.btn_withdraw.setEnabled(True)
            self.btn_withdraw.setText("REQUEST PAYOUT")
            self.btn_withdraw.setStyleSheet("""
                QPushButton { background: #10B981; color: #FFFFFF; border: none; border-radius: 12px; font-weight: bold; font-size: 14px; margin-top: 20px; }
                QPushButton:hover { background: #059669; }
            """)
        else:
            self.btn_withdraw.setEnabled(False)
            self.btn_withdraw.setText(f"Min. $10.00 ({((amount/10)*100):.0f}%)")
            self.btn_withdraw.setStyleSheet("""
                QPushButton { background: #E5E7EB; color: #9CA3AF; border: none; border-radius: 12px; font-weight: bold; font-size: 14px; margin-top: 20px; }
            """)

    def request_payout(self):
        addr = self.input_addr.text()
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
        self.term.setStyleSheet("""
            QTextEdit {
                background-color: #111827;
                color: #34D399;
                font-family: 'Consolas', monospace;
                border: none;
                border-radius: 12px;
                padding: 20px;
                font-size: 13px;
            }
        """)

        # Shadow
        shadow = QGraphicsDropShadowEffect(self.term)
        shadow.setBlurRadius(20); shadow.setYOffset(5); shadow.setColor(QColor(0,0,0,40))
        self.term.setGraphicsEffect(shadow)

        layout.addWidget(self.term)

# --- MAIN WINDOW ---

class ElysiumApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ELYSIUM PROVIDER CLIENT")
        self.resize(1100, 750)

        # Frameless Logic
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Main Layout Container (Rounded & Shadowed)
        self.main_container = QFrame()
        self.main_container.setObjectName("MainFrame")
        self.main_container.setStyleSheet("""
            #MainFrame {
                background-color: #F9FAFB;
                border-radius: 16px;
                border: 1px solid #F3F4F6;
            }
        """)

        # Outer Layout for Shadow Margin
        outer_layout = QVBoxLayout()
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.addWidget(self.main_container)

        # Shadow for the Window
        window_shadow = QGraphicsDropShadowEffect(self.main_container)
        window_shadow.setBlurRadius(30); window_shadow.setColor(QColor(0,0,0,30))
        self.main_container.setGraphicsEffect(window_shadow)

        # Central Widget Wrapper
        wrapper = QWidget()
        wrapper.setLayout(outer_layout)
        self.setCentralWidget(wrapper)

        # Internal Layout
        self.layout = QVBoxLayout(self.main_container)
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setSpacing(0)

        # 1. Custom Title Bar
        self.title_bar = TitleBar(self)
        self.layout.addWidget(self.title_bar)

        # 2. Content Stack
        self.root_stack = QStackedWidget()
        self.layout.addWidget(self.root_stack)

        # --- SCREENS ---

        # A. Login
        self.login_ui = LoginWidget()
        self.login_ui.switch_signal.connect(lambda: self.root_stack.setCurrentIndex(1))
        self.login_ui.success_signal.connect(self.start_dashboard)
        self.root_stack.addWidget(self.login_ui)

        # B. Register
        self.register_ui = RegisterWidget()
        self.register_ui.switch_signal.connect(lambda: self.root_stack.setCurrentIndex(0))
        self.register_ui.success_signal.connect(self.start_dashboard)
        self.root_stack.addWidget(self.register_ui)

        # C. Dashboard
        self.dash_container = QWidget()
        dash_layout = QHBoxLayout(self.dash_container)
        dash_layout.setContentsMargins(0,0,0,0)
        dash_layout.setSpacing(0)

        # Sidebar
        sidebar = QWidget(); sidebar.setFixedWidth(250); sidebar.setStyleSheet("background:#FFFFFF; border-right:1px solid #F3F4F6;")
        sl = QVBoxLayout(sidebar); sl.setContentsMargins(20,40,20,20); sl.setSpacing(10)

        self.btn_dash = SidebarButton("Dashboard", "⚡", True)
        self.btn_hw = SidebarButton("Hardware", "🔋")
        self.btn_wall = SidebarButton("Wallet", "💰")
        self.btn_term = SidebarButton("Terminal", "💻")

        for b in [self.btn_dash, self.btn_hw, self.btn_wall, self.btn_term]:
            sl.addWidget(b); b.clicked.connect(self.nav)
        sl.addStretch()

        # User Profile Stub
        prof = QFrame()
        prof.setStyleSheet("background: #F9FAFB; border-radius: 8px; padding: 10px;")
        pl = QHBoxLayout(prof)
        pl.addWidget(QLabel("👤"))
        pl.addWidget(QLabel("Worker Node"))
        sl.addWidget(prof)

        dash_layout.addWidget(sidebar)

        # Content Stack
        self.content_stack = QStackedWidget(); self.content_stack.setContentsMargins(40,40,40,40)
        self.tab_dash = DashboardTab()
        self.tab_hw = HardwareTab()
        self.tab_wall = WalletTab()
        self.tab_term = TerminalTab()

        for t in [self.tab_dash, self.tab_hw, self.tab_wall, self.tab_term]: self.content_stack.addWidget(t)
        dash_layout.addWidget(self.content_stack)

        self.root_stack.addWidget(self.dash_container)

        # Check if already logged in
        if load_config().get("wallet_id"):
            self.root_stack.setCurrentIndex(2) # Go to Dashboard
            self.start_threads()
        else:
            self.root_stack.setCurrentIndex(0) # Go to Login

        self.oldPos = self.pos()

    def start_dashboard(self):
        self.root_stack.setCurrentIndex(2)
        self.start_threads()

    def start_threads(self):
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
        if s == self.btn_dash: self.content_stack.setCurrentIndex(0)
        elif s == self.btn_hw: self.content_stack.setCurrentIndex(1)
        elif s == self.btn_wall: self.content_stack.setCurrentIndex(2)
        elif s == self.btn_term: self.content_stack.setCurrentIndex(3)

    def toggle_node(self):
        if not self.node_active:
            self.node_thread = NodeThread()
            self.node_thread.log_sig.connect(self.tab_term.term.append)
            self.node_thread.wallet_id_sig.connect(self.poll.set_wallet_id)
            self.node_thread.start()
            self.node_active = True

            self.tab_dash.lbl_status.setText("ONLINE")
            self.tab_dash.lbl_status.setStyleSheet("color:#10B981; font-size:32px; font-weight:800")
            self.tab_dash.btn_start.setText("STOP NODE")
            self.tab_dash.btn_start.setStyleSheet("""
                QPushButton {
                    background-color: #FFFFFF;
                    color: #EF4444;
                    border: 4px solid #EF4444;
                    border-radius: 140px;
                    font-size: 18px;
                    font-weight: bold;
                }
                QPushButton:hover { background-color: #FEF2F2; }
            """)
        else:
            if self.node_thread: self.node_thread.stop()
            self.node_active = False
            self.tab_dash.lbl_status.setText("STANDBY")
            self.tab_dash.lbl_status.setStyleSheet("color:#9CA3AF; font-size:32px; font-weight:800")
            self.tab_dash.btn_start.setText("INITIALIZE NODE")
            self.tab_dash.btn_start.setStyleSheet("""
                QPushButton {
                    background-color: #FFFFFF;
                    color: #10B981;
                    border: 4px solid #10B981;
                    border-radius: 140px;
                    font-size: 18px;
                    font-weight: bold;
                }
                QPushButton:hover { background-color: #ECFDF5; }
            """)

    def on_hw(self, d):
        if d['has_gpu']:
            self.tab_hw.lbl_mode.setText("🟢 GPU MODE ACTIVE (High Efficiency)")
            self.tab_hw.lbl_mode.setStyleSheet("color:#10B981; font-weight:bold")
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
            self.tab_hw.lbl_mode.setStyleSheet("color:#F59E0B; font-weight:bold")
            self.tab_hw.gpu_bar.setValue(int(d['cpu'])) # Show CPU on GPU bar as fallback
            self.tab_hw.gpu_val.setText(f"CPU: {d['cpu']}%")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 9))
    win = ElysiumApp()
    win.show()
    sys.exit(app.exec_())
