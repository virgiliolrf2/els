import sys
import os
import subprocess
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QLabel, 
                             QVBoxLayout, QWidget, QTextEdit, QHBoxLayout, QFrame, QGraphicsDropShadowEffect)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PyQt5.QtGui import QColor, QFont, QFontDatabase

# --- CONFIGURAÇÃO DE REDE ---
FORCE_MASTER_IP = "127.0.0.1" 

# --- NODE THREAD ---
class NodeThread(QThread):
    log_sig = pyqtSignal(str)
    earn_sig = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.process = None
        self.running = True
        self.total_money = 0.0

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
        
        cmd = f"source ~/.elysium_venv/bin/activate && python3 $(wslpath -a 'elysium_node.py') --master_url {master_url}"
        
        try:
            self.process = subprocess.Popen(
                ["wsl", "bash", "-c", cmd],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', bufsize=1
            )
            
            for line in self.process.stdout:
                if not self.running: break
                l = line.strip()
                if any(x in l for x in ["[NET]", "[ENV]", "[EXEC]", "[JOB]", "[P2P]", "[NEW JOB]", "[UPLOAD]", "[SUCCESS]", "[ERR]"]):
                    self.log_sig.emit(l)
                if "[BILLING]" in l:
                    self.total_money += 0.0005
                    self.earn_sig.emit(self.total_money)
                    
        except Exception as e:
            self.log_sig.emit(f"❌ CRITICAL FAILURE: {e}")

    def stop(self):
        self.running = False
        if self.process: self.process.terminate()

# --- INSTALL THREAD ---
class InstallThread(QThread):
    msg = pyqtSignal(str)
    done = pyqtSignal()

    def run(self):
        try:
            self.msg.emit("🛠️ INSTALLING CORE DEPENDENCIES (LIBTORRENT)...")
            subprocess.run(["wsl", "bash", "-c", "sudo apt update && sudo apt install -y python3-libtorrent python3-venv"], check=False)
            
            self.msg.emit("🛠️ PROVISIONING HYBRID VIRTUAL ENVIRONMENT...")
            subprocess.run(["wsl", "bash", "-c", "python3 -m venv --system-site-packages ~/.elysium_venv"], check=False)
            
            self.msg.emit("🛠️ DEPLOYING PYTORCH & HIVEMIND STACK...")
            subprocess.run(["wsl", "bash", "-c", "source ~/.elysium_venv/bin/activate && pip install requests hivemind torch --quiet"], check=False)
            
            self.msg.emit("✅ SYSTEM READY.")
            self.done.emit()
        except Exception as e:
            self.msg.emit(f"❌ INSTALL ERROR: {e}")

# --- TESLA-STYLE GUI ---
class ElysiumGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ELYSIUM COMPUTE NODE")
        self.resize(1000, 650)
        
        # Estilo Global (CSS-like QSS)
        # Paleta: Fundo Matte, Texto Branco/Cinza, Acentos Neon Verde
        self.setStyleSheet("""
            QMainWindow { background-color: #121212; }
            QWidget { font-family: 'Segoe UI', 'Roboto', sans-serif; }
            
            QTextEdit { 
                background-color: #0a0a0a; 
                border: 1px solid #333; 
                color: #00ff9d; 
                font-family: 'Consolas', 'Courier New', monospace; 
                font-size: 11px;
                border-radius: 4px;
                padding: 10px;
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
        
        # Coluna da Esquerda (Ganhos)
        earnings_box = QVBoxLayout()
        lbl_earn_title = QLabel("LIFETIME EARNINGS")
        lbl_earn_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold;")
        self.lbl_earn = QLabel("$ 0.0000")
        self.lbl_earn.setStyleSheet("color: #ffffff; font-size: 48px; font-weight: 300;") # Fonte fina estilo iOS/Tesla
        earnings_box.addWidget(lbl_earn_title)
        earnings_box.addWidget(self.lbl_earn)
        
        # Coluna da Direita (Status)
        status_box = QVBoxLayout()
        status_box.setAlignment(Qt.AlignRight)
        lbl_status_title = QLabel("SYSTEM STATUS")
        lbl_status_title.setStyleSheet("color: #666; font-size: 11px; letter-spacing: 2px; font-weight: bold; text-align: right;")
        self.lbl_status = QLabel("STANDBY")
        self.lbl_status.setStyleSheet("color: #666; font-size: 18px; font-weight: bold;")
        
        status_box.addWidget(lbl_status_title)
        status_box.addWidget(self.lbl_status)
        
        header.addLayout(earnings_box)
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
        # Sombra sutil interna no log
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(15)
        self.logs.setGraphicsEffect(shadow)
        layout.addWidget(self.logs)
        
        # --- CONTROL BUTTON ---
        self.btn = QPushButton("INITIALIZE SYSTEM")
        self.btn.setFixedHeight(60)
        self.btn.setCursor(Qt.PointingHandCursor)
        self.update_btn_style(False) # Estado inicial (Desligado)
        self.btn.clicked.connect(self.toggle)
        layout.addWidget(self.btn)
        
        self.worker = None
        self.installed = False

    def update_btn_style(self, active):
        if active:
            # Estilo "Ligado" - Vermelho/Alerta para desligar
            self.btn.setStyleSheet("""
                QPushButton {
                    background-color: #1a1a1a;
                    color: #ff4444;
                    border: 1px solid #ff4444;
                    font-size: 14px; font-weight: bold; letter-spacing: 1px; border-radius: 6px;
                }
                QPushButton:hover { background-color: #2a1a1a; }
            """)
        else:
            # Estilo "Pronto" - Verde Neon Cyber
            self.btn.setStyleSheet("""
                QPushButton {
                    background-color: #00E676; 
                    color: #000000;
                    border: none;
                    font-size: 14px; font-weight: bold; letter-spacing: 1px; border-radius: 6px;
                }
                QPushButton:hover { background-color: #00C853; }
                QPushButton:disabled { background-color: #333; color: #666; }
            """)

    def toggle(self):
        if not self.installed:
            self.btn.setEnabled(False)
            self.btn.setText("PROVISIONING ENVIRONMENT...")
            self.installer = InstallThread()
            self.installer.msg.connect(self.logs.append)
            self.installer.done.connect(self.finish_install)
            self.installer.start()
        else:
            if self.worker and self.worker.isRunning():
                self.worker.stop()
                self.btn.setText("INITIALIZE SYSTEM")
                self.update_btn_style(False)
                self.lbl_status.setText("STANDBY")
                self.lbl_status.setStyleSheet("color: #666; font-size: 18px; font-weight: bold;")
            else:
                self.worker = NodeThread()
                self.worker.log_sig.connect(self.logs.append)
                self.worker.earn_sig.connect(lambda e: self.lbl_earn.setText(f"$ {e:.4f}"))
                self.worker.start()
                self.btn.setText("TERMINATE PROCESS")
                self.update_btn_style(True)
                self.lbl_status.setText("COMPUTING")
                # Brilho verde no texto de status quando ativo
                self.lbl_status.setStyleSheet("color: #00E676; font-size: 18px; font-weight: bold;")

    def finish_install(self):
        self.installed = True
        self.btn.setEnabled(True)
        self.toggle()

if __name__ == "__main__":
    try:
        app = QApplication(sys.argv)
        # Fonte customizada pode ser carregada aqui se desejar, usando padrão do sistema por enquanto
        win = ElysiumGUI()
        win.show()
        sys.exit(app.exec_())
    except Exception as e:
        print("Fatal Error:", e)