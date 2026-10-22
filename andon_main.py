import sys
import socket
import json
import threading
import time
import gc
import logging
import subprocess
import os
import signal
from datetime import datetime

ACTIVE_WINDOW = None

# --- Wayland環境対策: X11(Xwayland)互換モードで動作させる ---
os.environ['QT_QPA_PLATFORM'] = 'xcb'

from flask import Flask, request, jsonify
from PyQt5.QtGui import QCursor, QKeySequence, QFont, QPalette, QColor
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout,
    QWidget, QListWidget, QListWidgetItem, QStackedLayout, QShortcut, QFrame
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QObject, QEvent, QSize

# --- グローバルキー監視フィルター (どこにフォーカスがあってもQ/Escで即座に終了) ---
class GlobalKeyFilter(QObject):
    def __init__(self, exit_callback):
        super().__init__()
        self.exit_callback = exit_callback

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Q, Qt.Key_Escape) or (hasattr(event, 'text') and event.text().lower() == 'q'):
                self.exit_callback()
                return True
        return super().eventFilter(obj, event)

# --- 環境判定 & GPIO設定 ---
IS_RASPBERRY_PI = False
try:
    with open('/sys/firmware/devicetree/base/model', 'r') as f:
        if 'Raspberry Pi' in f.read():
            IS_RASPBERRY_PI = True
except Exception:
    pass

if IS_RASPBERRY_PI:
    try:
        from gpiozero import LED, Buzzer  # type: ignore
    except ImportError:
        IS_RASPBERRY_PI = False

if not IS_RASPBERRY_PI:
    class DummyGPIO:
        def __init__(self, pin): pass
        def on(self): pass
        def off(self): pass
        def close(self): pass
        @property
        def is_lit(self): return False
    LED = DummyGPIO
    Buzzer = DummyGPIO

# ピン番号の設定
PATLITE_PIN = 23
BUZZER_PIN = 24

# ネットワーク設定
UDP_PORT = 50050
HTTP_PORT = 5000
SYNC_PORT = 50055  # 子機への送信ポート
DISCOVERY_MSG = "DISCOVER_ANDON"
RESPONSE_MSG = "ANDON_HERE"

# --- Logging Setup ---
from logging.handlers import RotatingFileHandler
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(APP_DIR, "andon_main.log")

def get_local_ip():
    """自機のIPアドレスを取得"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.10.254', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"

def get_system_stats_str():
    parts = []
    # メモリ情報 (/proc/meminfo)
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem = {}
        for line in lines:
            kv = line.split(':')
            if len(kv) == 2:
                mem[kv[0].strip()] = kv[1].strip()
        total_mb = int(mem.get('MemTotal', '0 kB').split()[0]) // 1024
        avail_mb = int(mem.get('MemAvailable', '0 kB').split()[0]) // 1024
        used_mb = total_mb - avail_mb
        parts.append(f"RAM: {used_mb}/{total_mb}MB (空き:{avail_mb}MB)")
    except Exception:
        pass
    
    # ロードアベレージ
    try:
        load1, load5, load15 = os.getloadavg()
        parts.append(f"Load: {load1:.2f},{load5:.2f},{load15:.2f}")
    except Exception:
        pass

    # CPU温度
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = float(f.read().strip()) / 1000.0
            parts.append(f"Temp: {temp:.1f}°C")
    except Exception:
        pass

    return " | ".join(parts) if parts else "System stats N/A"

log_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=2 * 1024 * 1024,  # 最大2MB
    backupCount=2,             # 2世代バックアップ (計最大6MB)
    encoding='utf-8'
)
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s')
log_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.handlers.clear()
logger.addHandler(log_handler)
logger.addHandler(console_handler)

logger.info("=" * 65)
logger.info(f"=== ANDON MAIN APPLICATION STARTED (PID: {os.getpid()}) ===")
logger.info(f"Log file: {LOG_FILE_PATH}")
logger.info(f"Local IP: {get_local_ip()}")
logger.info(f"Initial System Stats: {get_system_stats_str()}")
logger.info("=" * 65)

def handle_exception(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = handle_exception

# --- Flask Server Thread (親機のみ: PCからの信号受信) ---
class FlaskServerThread(QThread):
    request_received = pyqtSignal(dict)

    def run(self):
        app = Flask(__name__)

        @app.route('/api/call', methods=['POST'])
        def receive_call():
            t_recv = time.perf_counter()
            client_ip = request.remote_addr
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                logging.warning(f"[Flask] Invalid JSON from {client_ip}")
                return jsonify({"status": "error", "message": "Invalid JSON"}), 400
            
            machine_id = data.get('machine_id')
            status = data.get('status')
            logging.info(f"[Flask] <-- Received call from {client_ip}: machine_id='{machine_id}', status='{status}'")

            t_gpio_done = t_recv
            t_sync_done = t_recv

            # 【超先行GPIO＆超先行子機送信】（ミリ秒未満の超即応）
            global ACTIVE_WINDOW
            if ACTIVE_WINDOW:
                # 1. 呼出ならパトライト・ブザーを即時点灯
                if status == 'calling' and hasattr(ACTIVE_WINDOW, 'patlite') and ACTIVE_WINDOW.patlite:
                    try:
                        if not ACTIVE_WINDOW.patlite.is_lit:
                            ACTIVE_WINDOW.patlite.on()
                        if ACTIVE_WINDOW.buzzer and not ACTIVE_WINDOW.buzzer.is_lit:
                            ACTIVE_WINDOW.buzzer.on()
                    except Exception as e:
                        logging.error(f"[Flask] Early GPIO error: {e}")
                t_gpio_done = time.perf_counter()

                # 2. 子機への送信もFlask受信直後に非同期で即座に開始
                if hasattr(ACTIVE_WINDOW, 'sync_sender') and ACTIVE_WINDOW.sync_sender:
                    threading.Thread(target=ACTIVE_WINDOW.sync_sender.send_sync, args=(data,), daemon=True).start()
                t_sync_done = time.perf_counter()

            self.request_received.emit(data)
            t_total_ms = (time.perf_counter() - t_recv) * 1000
            t_gpio_ms = (t_gpio_done - t_recv) * 1000
            t_sync_ms = (t_sync_done - t_gpio_done) * 1000
            logging.info(f"[Flask] --> Responded 200 OK in {t_total_ms:.2f}ms (early_gpio={t_gpio_ms:.2f}ms, sync_thread={t_sync_ms:.2f}ms)")
            return jsonify({"status": "ok"})

        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        
        try:
            logging.info(f"[Flask] Starting HTTP server on port {HTTP_PORT}...")
            app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, use_reloader=False)
        except Exception as e:
            logging.error(f"[Flask] Server Error: {e}")

# --- UDP Discovery Responder Thread (親機のみ: PCからの探索に応答) ---
class DiscoveryThread(QThread):
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', UDP_PORT))
            logging.info(f"[Discovery] Listening for discovery on UDP port {UDP_PORT}...")
        except Exception as e:
            logging.error(f"[Discovery] UDP Bind Error: {e}")
            return

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = data.decode('utf-8', errors='replace')
                if msg.startswith(DISCOVERY_MSG):
                    sock.sendto(RESPONSE_MSG.encode('utf-8'), addr)
                    logging.info(f"[Discovery] Responded '{RESPONSE_MSG}' to client {addr[0]}:{addr[1]}")
            except Exception as e:
                logging.error(f"[Discovery] UDP Error: {e}")

# --- Sync Sender (親機用: 子機へ高信頼・超高速送信) ---
class SyncSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.targets = self._get_broadcast_targets()

    def _get_broadcast_targets(self):
        targets = ['<broadcast>', '255.255.255.255']
        local_ips = []
        try:
            hostname = socket.gethostname()
            local_ips = socket.gethostbyname_ex(hostname)[2]
        except Exception as e:
            logging.warning(f"[SyncSender] Could not gethostbyname_ex: {e}")

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('192.168.10.254', 80))
            active_ip = s.getsockname()[0]
            s.close()
            if active_ip not in local_ips:
                local_ips.append(active_ip)
        except Exception:
            pass

        for ip in local_ips:
            if ip.startswith('127.'):
                continue
            parts = ip.split('.')
            if len(parts) == 4:
                bc24 = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                if bc24 not in targets:
                    targets.append(bc24)
                bc16 = f"{parts[0]}.{parts[1]}.255.255"
                if bc16 not in targets:
                    targets.append(bc16)

        common_bcs = ['192.168.10.255', '192.168.1.255', '192.168.0.255', '192.168.11.255', '10.255.255.255']
        for bc in common_bcs:
            if bc not in targets:
                targets.append(bc)
        
        unique_targets = list(dict.fromkeys(targets))
        logging.info(f"[SyncSender] Resolved broadcast targets ({len(unique_targets)} addrs): {unique_targets}")
        return unique_targets

    def send_sync(self, data):
        """子機へ状態を一方的に送信 (パケットロス防止のため3回連続バースト送信)"""
        t0 = time.perf_counter()
        payload = json.dumps({"type": "sync", **data}).encode('utf-8')
        success_count = 0
        fail_count = 0
        for _ in range(3):
            for ip in self.targets:
                try:
                    self.sock.sendto(payload, (ip, SYNC_PORT))
                    success_count += 1
                except Exception:
                    fail_count += 1
        elapsed_ms = (time.perf_counter() - t0) * 1000
        if elapsed_ms > 50 or fail_count > 0:
            logging.warning(f"[SyncSender] send_sync finished in {elapsed_ms:.1f}ms (sent={success_count}, fail={fail_count})")
        else:
            logging.debug(f"[SyncSender] send_sync finished in {elapsed_ms:.1f}ms")

    def send_full_sync(self, active_calls):
        """子機へ現在の呼出状態全体を送信（子機の後から起動・取りこぼし対策）"""
        payload = json.dumps({"type": "full_sync", "active_calls": active_calls}).encode('utf-8')
        for _ in range(2):
            for ip in self.targets:
                try:
                    self.sock.sendto(payload, (ip, SYNC_PORT))
                except Exception:
                    pass

# --- Spreadsheet Mapping Thread ---
SPREADSHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/1pDYwMWIz-UW-DJ2mXJkmjEjzx6_JwWtGw_WOo7BjU9g/export?format=csv&gid=1446333610"
CACHE_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "machine_mapping.csv")

class SpreadsheetMappingThread(QThread):
    mapping_updated = pyqtSignal(dict)

    def run(self):
        while True:
            mapping = self.fetch_mapping()
            if mapping:
                self.mapping_updated.emit(mapping)
            time.sleep(3600)  # 1時間ごとに再取得

    def fetch_mapping(self):
        import csv
        import urllib.request
        import io

        mapping = {}
        logging.info("[Spreadsheet] Fetching machine mapping from Google Spreadsheet...")
        t_start = time.perf_counter()
        try:
            req = urllib.request.Request(SPREADSHEET_CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
                el_fetch = time.perf_counter() - t_start
                logging.info(f"[Spreadsheet] Downloaded CSV in {el_fetch:.2f}s ({len(content)} bytes)")
                try:
                    need_write = True
                    if os.path.exists(CACHE_FILE_PATH):
                        try:
                            with open(CACHE_FILE_PATH, 'rb') as f:
                                if f.read() == content:
                                    need_write = False
                        except Exception:
                            pass
                    if need_write:
                        with open(CACHE_FILE_PATH, 'wb') as f:
                            f.write(content)
                        logging.info("[Spreadsheet] Updated local cache file.")
                except Exception as e:
                    logging.error(f"[Spreadsheet] Failed to write cache: {e}")
                text = content.decode('utf-8-sig', errors='replace')
                reader = csv.reader(io.StringIO(text))
                for i, row in enumerate(reader):
                    if i == 0: continue
                    if len(row) >= 2:
                        key = row[0].strip()
                        val = row[1].strip()
                        if key:
                            mapping[key] = val
                            try:
                                mapping[str(int(key))] = val
                            except ValueError:
                                pass
                if mapping:
                    logging.info(f"[Spreadsheet] Successfully loaded {len(mapping)} machine mappings from Google Spreadsheet.")
                    return mapping
        except Exception as e:
            el_fetch = time.perf_counter() - t_start
            logging.warning(f"[Spreadsheet] Failed to fetch spreadsheet online ({el_fetch:.2f}s): {e}. Falling back to cache.")

        if os.path.exists(CACHE_FILE_PATH):
            try:
                t_cache = time.perf_counter()
                with open(CACHE_FILE_PATH, 'r', encoding='utf-8-sig', errors='replace') as f:
                    reader = csv.reader(f)
                    for i, row in enumerate(reader):
                        if i == 0: continue
                        if len(row) >= 2:
                            key = row[0].strip()
                            val = row[1].strip()
                            if key:
                                mapping[key] = val
                                try:
                                    mapping[str(int(key))] = val
                                except ValueError:
                                    pass
                if mapping:
                    logging.info(f"[Spreadsheet] Loaded {len(mapping)} machine mappings from local cache in {(time.perf_counter() - t_cache)*1000:.1f}ms.")
                    return mapping
            except Exception as e:
                logging.error(f"[Spreadsheet] Failed to read local cache mapping: {e}")

        return mapping

# --- Main Window ---
class AndonWindow(QMainWindow):
    # 安全標語（月別・日別）
    MONTHLY_SLOGANS = {
        1: "年始め 気持ち引き締め 安全始動",
        2: "寒さに負けず 現場の整理 5S徹底",
        3: "納期追う 心にブレーキ 安全第一",
        4: "慣れた手に 初心を込めて 無災害",
        5: "連休明け 気合を入れ直し 指差呼称",
        6: "雨の日の 滑る足元 即清掃",
        7: "暑さに負けぬ 喉渇く前に水分補給",
        8: "猛暑こそ 小まめな休憩 声かけ合い",
        9: "金型と ボルトの緩み 見逃すな",
        10: "守るルールが 身を守る",
        11: "かじかむ手 しっかりほぐして 確実動作",
        12: "忙しい 年の瀬だからこそ 一呼吸",
    }

    DAILY_SLOGANS = {
        1: "月はじめ 安全装置の 再点検",
        2: "慣れた作業も 省く手順に 潜む罠",
        3: "保護メガネ 耳栓・安全靴 フル装備",
        4: "型替え時 落下の危険を ゼロにする",
        5: "金型面 ゴミと油を 拭き取って",
        6: "危険域 手を入れるなと 心掛け",
        7: "重いコイル 玉掛け合図は 確実に",
        8: "片手押し 無理な体勢 絶対禁止",
        9: "その異音 見逃す前に まず停止",
        10: "10日目の 気の緩みこそ 赤信号",
        11: "いざという時すぐ押す 非常停止",
        12: "素手で触るな 切り傷・擦り傷 未然に防ぐ",
        13: "金型の 締め付けボルト 緩みのないこと",
        14: "スクラップ 片付け徹底 足元クリア",
        15: "中盤戦 深呼吸して リフレッシュ",
        16: "動く機械に 手を出すな",
        17: "ペダル踏み 異物乗せぬよう 足元点検",
        18: "巻き込まれ防ぐ 袖口・裾 確認ヨシ",
        19: "ヒヤリとした その体験を みんなで共有",
        20: "急ぐ時こそ 焦る心に ブレーキを",
        21: "油漏れ 滑る足元 即拭き取り",
        22: "二人作業 声かけ合図で 呼吸を合わす",
        23: "中腰作業 腰を落として 安全作業",
        24: "重い金型 はみ出し・落下を 許さない",
        25: "給油点検 機械の悲鳴を 見逃すな",
        26: "暗い手元 ライトを当てて 視界良好",
        27: "台車押し 曲がり角では 一旦停止",
        28: "ぼんやり作業は 事故の元",
        29: "慌てる手元に 危険あり 落ち着き確認",
        30: "月締め前 使った工具の 定位置復帰",
        31: "今日も一日 全員無事故で 仕事終え",
    }

    def __init__(self):
        super().__init__()
        global ACTIVE_WINDOW
        ACTIVE_WINDOW = self
        self.setWindowTitle("Andon Main Unit")
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")

        self.active_calls = {}
        self.finished_timers = {}  # 終了表示消去タイマー管理用
        self.is_pulsing = False
        self.machine_mapping = {}
        self.slogan_index = 0

        # ネイティブフォント設定
        self.font_number = QFont("Arial", 160, QFont.Bold)
        self.font_prefix = QFont("Arial", 40, QFont.Bold)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.stack_layout = QStackedLayout(central_widget)

        # ==========================================
        # Page 0: アンドン呼出・終了表示画面 (page_andon)
        # ==========================================
        self.page_andon = QWidget()
        self.page_andon.setStyleSheet("background-color: black;")
        layout_andon = QHBoxLayout(self.page_andon)
        layout_andon.setContentsMargins(0, 0, 0, 0)
        layout_andon.setSpacing(0)

        list_style = """
            QListWidget {
                background-color: black;
                border: none;
                outline: none;
                padding: 0px;
                margin: 0px;
            }
            QListWidget::item {
                background-color: black;
                border: none;
                padding: 0px;
                margin: 0px;
            }
            QListWidget::item:selected {
                background-color: black;
            }
        """

        self.list_calling = QListWidget()
        self.list_calling.setFocusPolicy(Qt.NoFocus)
        self.list_calling.setSpacing(0)
        self.list_calling.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_calling.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_calling.setStyleSheet(list_style)
        layout_andon.addWidget(self.list_calling)

        self.list_finished = QListWidget()
        self.list_finished.setFocusPolicy(Qt.NoFocus)
        self.list_finished.setSpacing(0)
        self.list_finished.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_finished.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_finished.setStyleSheet(list_style)
        layout_andon.addWidget(self.list_finished)
        self.stack_layout.addWidget(self.page_andon)

        # ==========================================
        # Page 1: 10番ハイブリッド待機画面 (page_idle)
        # 大型デジタル時計 ＋ 安全標語 ＋ システム稼働ステータス
        # ==========================================
        self.page_idle = QWidget()
        self.page_idle.setStyleSheet("background-color: black;")
        layout_idle = QVBoxLayout(self.page_idle)
        layout_idle.setContentsMargins(40, 30, 40, 20)
        layout_idle.setSpacing(0)

        # --- 1. 上部: 大型デジタル時計 (フォントサイズ1.5倍: 130 -> 195) ---
        self.lbl_clock = QLabel()
        self.lbl_clock.setAlignment(Qt.AlignCenter)
        self.lbl_clock.setFont(QFont("Arial", 195, QFont.Bold))
        self.lbl_clock.setStyleSheet("color: #FFFFFF; font-weight: bold; letter-spacing: 4px;")
        layout_idle.addWidget(self.lbl_clock)

        layout_idle.addSpacing(25)

        # 区切りライン
        line1 = QFrame()
        line1.setFrameShape(QFrame.HLine)
        line1.setFrameShadow(QFrame.Sunken)
        line1.setStyleSheet("border: 1px solid #263238;")
        layout_idle.addWidget(line1)

        layout_idle.addSpacing(25)

        # --- 2. 中央: 安全第一マーク ＆ スローガン ---
        self.lbl_safety_badge = QLabel("✚  安 全 第 一  ✚")
        self.lbl_safety_badge.setAlignment(Qt.AlignCenter)
        self.lbl_safety_badge.setFont(QFont("Arial", 46, QFont.Bold))
        self.lbl_safety_badge.setStyleSheet("color: #00E676; letter-spacing: 6px;")
        layout_idle.addWidget(self.lbl_safety_badge)

        layout_idle.addSpacing(15)

        self.lbl_slogan_month = QLabel()
        self.lbl_slogan_month.setAlignment(Qt.AlignCenter)
        self.lbl_slogan_month.setFont(QFont("Arial", 36, QFont.Bold))
        self.lbl_slogan_month.setStyleSheet("color: #FFD54F; letter-spacing: 2px;")
        layout_idle.addWidget(self.lbl_slogan_month)

        layout_idle.addSpacing(12)

        self.lbl_slogan_day = QLabel()
        self.lbl_slogan_day.setAlignment(Qt.AlignCenter)
        self.lbl_slogan_day.setFont(QFont("Arial", 36, QFont.Bold))
        self.lbl_slogan_day.setStyleSheet("color: #FFD54F; letter-spacing: 2px;")
        layout_idle.addWidget(self.lbl_slogan_day)

        layout_idle.addStretch(1)

        # 区切りライン
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        line2.setStyleSheet("border: 1px solid #263238;")
        layout_idle.addWidget(line2)

        layout_idle.addSpacing(15)

        # --- 3. 下部: 年月日表示 ＆ 通信状態 ---
        status_box = QHBoxLayout()
        status_box.setContentsMargins(10, 0, 10, 0)

        self.lbl_date = QLabel()
        self.lbl_date.setFont(QFont("Arial", 22, QFont.Bold))
        self.lbl_date.setStyleSheet("color: #00E676;")
        status_box.addWidget(self.lbl_date)

        status_box.addStretch(1)

        self.lbl_status_net = QLabel()
        self.lbl_status_net.setFont(QFont("Arial", 18))
        self.lbl_status_net.setStyleSheet("color: #78909C;")
        status_box.addWidget(self.lbl_status_net)

        layout_idle.addLayout(status_box)
        self.stack_layout.addWidget(self.page_idle)

        # アプリ全体で確実に終了キーを捕捉するショートカット
        for key_seq in ["q", "Q", "Escape"]:
            sc = QShortcut(QKeySequence(key_seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(self.exit_app)

        self.patlite = None
        self.buzzer = None
        if IS_RASPBERRY_PI:
            try:
                self.patlite = LED(PATLITE_PIN)
                self.buzzer = LED(BUZZER_PIN)
                logging.info(f"[Init] GPIO initialized: Patlite=pin{PATLITE_PIN}, Buzzer=pin{BUZZER_PIN}")
            except Exception as e:
                logging.error(f"[Init] GPIO Init Error: {e}")
        else:
            logging.info("[Init] Simulation mode: Dummy GPIO initialized")

        # スプレッドシートマッピングスレッドの起動
        self.mapping_thread = SpreadsheetMappingThread()
        self.mapping_thread.mapping_updated.connect(self.on_mapping_updated)
        self.mapping_thread.start()
        logging.info("[Init] SpreadsheetMappingThread started.")

        # 親機機能の起動
        self.flask_thread = FlaskServerThread()
        self.flask_thread.request_received.connect(self.handle_flask_request)
        self.flask_thread.start()
        logging.info("[Init] FlaskServerThread started.")

        self.discovery_thread = DiscoveryThread()
        self.discovery_thread.start()
        logging.info("[Init] DiscoveryThread started.")

        self.sync_sender = SyncSender()

        # 画面保護防止タイマー
        self.keep_awake_timer = QTimer(self)
        self.keep_awake_timer.timeout.connect(self.wake_screen)
        self.keep_awake_timer.start(30000)

        # 呼出点滅タイマー
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink_tick)
        self.blink_counters = {'calling': 0, 'finished': 0}

        self.last_wake_time = 0
        self.last_heavy_wake_time = 0
        self.last_pulse_time = 0

        # 時計更新タイマー (1秒ごと: 負荷0.01%以下)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)

        # 安全標語ローテーションタイマー (30秒ごと)
        self.slogan_timer = QTimer(self)
        self.slogan_timer.timeout.connect(self.rotate_slogan)
        self.slogan_timer.start(30000)

        # メモリ解放タイマー (30分ごと)
        self.gc_timer = QTimer(self)
        self.gc_timer.timeout.connect(self.clean_memory)
        self.gc_timer.start(1800000)

        # 子機への定期全同期タイマー (2秒ごと)
        self.full_sync_timer = QTimer(self)
        self.full_sync_timer.timeout.connect(self._sync_all_to_sub)
        self.full_sync_timer.start(2000)

        # 定期システムリソース監視ログタイマー (2分ごと)
        self.health_check_timer = QTimer(self)
        self.health_check_timer.timeout.connect(self._log_health_check)
        self.health_check_timer.start(120000)

        # 時計・ステータスの初回表示
        self.update_clock()
        self.update_net_status()

        # 初期表示: 待機画面を表示
        self.stack_layout.setCurrentWidget(self.page_idle)
        logging.info("[Init] Andon Main Unit initialized in lightweight mode (No VLC/Video).")

    def update_clock(self):
        """1秒ごとに待機画面の日付と時刻を更新 (ナノ秒レベルの超軽量処理)"""
        now = datetime.now()
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        date_str = f"{now.strftime('%Y年%m月%d日')} ({weekdays[now.weekday()]})"
        time_str = now.strftime("%H:%M:%S")

        self.lbl_date.setText(date_str)
        self.lbl_clock.setText(time_str)

        if not hasattr(self, '_current_day') or self._current_day != now.day:
            self._current_day = now.day
            self.update_slogans(now)

    def update_slogans(self, now=None):
        """月別・日別の安全標語を更新"""
        if now is None:
            now = datetime.now()
        m_slogan = self.MONTHLY_SLOGANS.get(now.month, "")
        d_slogan = self.DAILY_SLOGANS.get(now.day, "")
        m_num = str(now.month).translate(str.maketrans('0123456789', '０１２３４５６７８９'))
        d_num = str(now.day).translate(str.maketrans('0123456789', '０１２３４５６７８９'))
        self.lbl_slogan_month.setText(f"{m_num}月：{m_slogan}")
        self.lbl_slogan_day.setText(f"{d_num}日：{d_slogan}")

    def rotate_slogan(self):
        """安全標語を最新日付に合わせて更新"""
        self.update_slogans()

    def update_net_status(self):
        """下部のネットワーク状態テキストを更新"""
        ip = get_local_ip()
        self.lbl_status_net.setText(f"親機モード [HTTP: {HTTP_PORT} | UDP: {UDP_PORT}]  IP: {ip}")

    def _log_health_check(self):
        logging.info(f"[HealthCheck] {get_system_stats_str()} | ActiveCalls={dict(self.active_calls)} | CurrentPage={'Idle' if self.stack_layout.currentWidget() == self.page_idle else 'Andon'}")

    def _sync_all_to_sub(self):
        """定期的に子機へ最新の呼び出し状態全体を配信"""
        try:
            self.sync_sender.send_full_sync(self.active_calls)
        except Exception:
            pass

    def exit_app(self):
        """安全かつ即座に終了する"""
        try:
            if hasattr(self, 'patlite') and self.patlite:
                self.patlite.close()
                self.patlite = None
        except Exception:
            pass
        try:
            if hasattr(self, 'buzzer') and self.buzzer:
                self.buzzer.close()
                self.buzzer = None
        except Exception:
            pass
        QApplication.quit()
        os._exit(0)

    def close_application(self, event=None):
        self.exit_app()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Q, Qt.Key_Escape) or (hasattr(event, 'text') and event.text().lower() == 'q'):
            self.exit_app()

    def _process_update(self, data):
        t_start = time.perf_counter()
        try:
            machine_id = str(data.get('machine_id', 'Unknown'))
            status = data.get('status')
            prev_status = self.active_calls.get(machine_id)
            
            # 【重複パケットガード】既に同じ状態ならスキップ（0ms）
            if prev_status == status:
                return
            if status == 'normal' and prev_status is None:
                return

            logging.info(f"[_process_update] State change: machine='{machine_id}', status='{status}' (prev='{prev_status}')")

            if status == 'calling':
                self.active_calls[machine_id] = 'calling'
            elif status == 'finished':
                self.active_calls[machine_id] = 'finished'
            elif status == 'normal':
                if machine_id in self.active_calls:
                    del self.active_calls[machine_id]

            t_dict = time.perf_counter()

            # 1. 【超最優先】GPIO (パトライト・ブザー)
            if status == 'calling':
                self.update_gpio()
            elif status == 'finished':
                self.trigger_pulse()
            elif not self.active_calls:
                self.update_gpio()
            t_gpio = time.perf_counter()

            # 2. 画面切り替え (呼出ありならアンドン画面、全解除ならハイブリッド待機画面)
            if self.active_calls:
                # 呼び出し中：即座にアンドン画面へ切り替え
                if self.stack_layout.currentWidget() != self.page_andon:
                    self.stack_layout.setCurrentWidget(self.page_andon)
                self.update_display()
            else:
                # 呼出ゼロ：待機画面へ切り替え
                if self.stack_layout.currentWidget() != self.page_idle:
                    self.update_clock()
                    self.update_net_status()
                    self.stack_layout.setCurrentWidget(self.page_idle)
            t_mode = time.perf_counter()

            # 3. 再描画
            self.centralWidget().repaint()
            t_repaint = time.perf_counter()

            # 4. タイマー・スクリーンセーバー解除
            if status == 'calling':
                self.blink_counters['calling'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                self.wake_screen()
            elif status == 'finished':
                self.blink_counters['finished'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                
                # 既存の消去タイマーがあれば停止して再設定 (30秒後に自動消去)
                if machine_id in self.finished_timers:
                    self.finished_timers[machine_id].stop()
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda m=machine_id: self.remove_finished_call(m))
                self.finished_timers[machine_id] = timer
                timer.start(30000)
                self.wake_screen()
            t_wake = time.perf_counter()

            total_ms = (t_wake - t_start) * 1000
            gpio_ms = (t_gpio - t_dict) * 1000
            mode_ms = (t_mode - t_gpio) * 1000
            paint_ms = (t_repaint - t_mode) * 1000
            wake_ms = (t_wake - t_repaint) * 1000
            log_msg = f"[_process_update] Completed in {total_ms:.1f}ms (gpio={gpio_ms:.1f}ms, mode/disp={mode_ms:.1f}ms, repaint={paint_ms:.1f}ms, wake={wake_ms:.1f}ms)"
            if total_ms > 200:
                logging.warning(f"[SLOW UPDATE WARNING] {log_msg}")
            else:
                logging.info(log_msg)

        except Exception as e:
            logging.error(f"Error in process_update: {e}", exc_info=True)

    def remove_finished_call(self, machine_id):
        if machine_id in self.finished_timers:
            self.finished_timers[machine_id].stop()
            del self.finished_timers[machine_id]
        if self.active_calls.get(machine_id) == 'finished':
            del self.active_calls[machine_id]
            self.update_display()
            self.update_gpio()
            if not self.active_calls:
                self.update_clock()
                self.update_net_status()
                self.stack_layout.setCurrentWidget(self.page_idle)

    def handle_flask_request(self, data):
        """PCから指令を受信 -> 親機の画面を最優先で即時更新"""
        self._process_update(data)

    def wake_screen(self):
        current_time = time.time()
        if current_time - self.last_wake_time < 5.0:
            return
        self.last_wake_time = current_time

        if IS_RASPBERRY_PI:
            try:
                current_pos = QCursor.pos()
                QCursor.setPos(current_pos.x() + 1, current_pos.y())
                QCursor.setPos(current_pos)

                if current_time - self.last_heavy_wake_time > 60.0:
                    self.last_heavy_wake_time = current_time
                    def _async_wake_worker():
                        try:
                            env = os.environ.copy()
                            if 'DISPLAY' not in env: env['DISPLAY'] = ':0'
                            cmd = "xdg-screensaver reset; xset s off -dpms s noblank dpms force on; vcgencmd display_power 1"
                            subprocess.run(cmd, shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                        except Exception as e:
                            logging.error(f"[WakeScreen] Async error: {e}")
                    threading.Thread(target=_async_wake_worker, daemon=True).start()
            except Exception as e:
                logging.error(f"[WakeScreen] Error: {e}")

    def blink_tick(self):
        if self.blink_counters['calling'] > 0:
            self.blink_counters['calling'] -= 1
        if self.blink_counters['finished'] > 0:
            self.blink_counters['finished'] -= 1
        self.update_blink_colors()
        if self.blink_counters['calling'] <= 0 and self.blink_counters['finished'] <= 0:
            self.blink_timer.stop()
            self.update_display()

    def _get_blink_color(self, status):
        color = "white"
        if status == 'calling':
            color = "white"
            if self.blink_counters['calling'] > 0 and self.blink_counters['calling'] % 2 == 1:
                color = "black"
        elif status == 'finished':
            color = "yellow"
            if self.blink_counters['finished'] > 0 and self.blink_counters['finished'] % 2 == 1:
                color = "black"
        return color

    def _set_label_color(self, lbl, color_name):
        pal = lbl.palette()
        if color_name == 'white':
            pal.setColor(QPalette.WindowText, Qt.white)
        elif color_name == 'yellow':
            pal.setColor(QPalette.WindowText, Qt.yellow)
        elif color_name == 'black':
            pal.setColor(QPalette.WindowText, Qt.black)
        else:
            pal.setColor(QPalette.WindowText, QColor(color_name))
        lbl.setPalette(pal)

    def update_blink_colors(self):
        if self.stack_layout.currentWidget() == self.page_idle: return
        def update_list(list_widget, status_type):
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                m_id = item.data(Qt.UserRole)
                if m_id and self.active_calls.get(m_id) == status_type:
                    color = self._get_blink_color(status_type)
                    widget = list_widget.itemWidget(item)
                    if widget:
                        for lbl in widget.findChildren(QLabel):
                            self._set_label_color(lbl, color)
        update_list(self.list_calling, 'calling')
        update_list(self.list_finished, 'finished')

    def clean_memory(self):
        gc.collect()

    def update_display(self):
        t_start = time.perf_counter()
        if self.stack_layout.currentWidget() == self.page_idle: return
        self.list_calling.clear()
        self.list_finished.clear()
        if not self.active_calls: return

        calling_count = sum(1 for s in self.active_calls.values() if s == 'calling')
        finished_count = sum(1 for s in self.active_calls.values() if s == 'finished')
        max_rows = max(calling_count, finished_count)

        if max_rows <= 5:
            number_size = 160
        elif max_rows == 6:
            number_size = 135
        elif max_rows == 7:
            number_size = 115
        else:
            number_size = max(60, int(900 / max_rows))
            
        prefix_size = int(number_size / 4)
        
        for m_id, status in self.active_calls.items():
            display_text = self._format_machine_id(m_id)
            if status == 'calling':
                color = self._get_blink_color('calling')
                self._add_list_item(self.list_calling, "呼出：", display_text, color, prefix_size, number_size, user_data=m_id)
            elif status == 'finished':
                color = self._get_blink_color('finished')
                self._add_list_item(self.list_finished, "終了：", display_text, color, prefix_size, number_size, user_data=m_id)
        
        duration_ms = (time.perf_counter() - t_start) * 1000
        if duration_ms > 50:
            logging.info(f"[Display] update_display took {duration_ms:.1f}ms for {len(self.active_calls)} calls (number_size={number_size})")

    def on_mapping_updated(self, mapping):
        self.machine_mapping = mapping
        if self.active_calls and self.stack_layout.currentWidget() == self.page_andon:
            self.update_display()

    def _format_machine_id(self, m_id):
        m_str = str(m_id).strip()
        if hasattr(self, 'machine_mapping') and self.machine_mapping:
            if m_str in self.machine_mapping:
                return self.machine_mapping[m_str]
            try:
                id_int_str = str(int(m_str))
                if id_int_str in self.machine_mapping:
                    return self.machine_mapping[id_int_str]
            except ValueError:
                pass
        try:
            id_int = int(m_str)
            if id_int == 28:
                return "17"
            if 16 <= id_int <= 27:
                return f"P-{id_int - 15}"
        except (ValueError, TypeError):
            pass
        return m_str

    def _add_list_item(self, list_widget, prefix_text, number_text, color, p_size, n_size, user_data=None):
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(40, 0, 20, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        
        lbl_prefix = QLabel(prefix_text)
        font_p = QFont("Arial", p_size, QFont.Bold)
        lbl_prefix.setFont(font_p)
        self._set_label_color(lbl_prefix, color)
        lbl_prefix.setAttribute(Qt.WA_TranslucentBackground, True)
        lbl_prefix.setContentsMargins(0, 0, 0, 0)
        
        lbl_num = QLabel(number_text)
        font_n = QFont("Arial", n_size, QFont.Bold)
        lbl_num.setFont(font_n)
        self._set_label_color(lbl_num, color)
        lbl_num.setAttribute(Qt.WA_TranslucentBackground, True)
        lbl_num.setContentsMargins(0, 0, 0, 0)
        
        layout.addWidget(lbl_prefix)
        layout.addWidget(lbl_num)
        layout.addStretch(1)

        item = QListWidgetItem()
        item_height = int(n_size * 1.05)
        item.setSizeHint(QSize(widget.sizeHint().width(), item_height))
        item.setData(Qt.UserRole, user_data)
        list_widget.addItem(item)
        list_widget.setItemWidget(item, widget)

    def update_gpio(self):
        if not IS_RASPBERRY_PI: return
        try:
            any_calling = any(s == 'calling' for s in self.active_calls.values())
            if any_calling:
                if self.patlite and not self.patlite.closed and not self.patlite.is_lit: self.patlite.on()
                if self.buzzer and not self.buzzer.closed and not self.buzzer.is_lit: self.buzzer.on()
            else:
                if not self.is_pulsing:
                    if self.patlite and not self.patlite.closed: self.patlite.off()
                    if self.buzzer and not self.buzzer.closed: self.buzzer.off()
        except Exception: pass

    def trigger_pulse(self):
        if not IS_RASPBERRY_PI: return
        if any(s == 'calling' for s in self.active_calls.values()): return
        if self.is_pulsing: return
        
        if time.time() - self.last_pulse_time < 0.5:
            logging.info("Chattering detected: Pulse request ignored.")
            return
        self.last_pulse_time = time.time()

        self.is_pulsing = True
        try:
            if self.patlite and not self.patlite.closed: self.patlite.on()
            if self.buzzer and not self.buzzer.closed: self.buzzer.on()
        except Exception:
            pass
        
        threading.Timer(0.2, self.end_pulse_worker).start()

    def end_pulse_worker(self):
        self.is_pulsing = False
        try:
            current_calls = self.active_calls.copy()
            any_calling = any(s == 'calling' for s in current_calls.values())
            
            if any_calling:
                if self.patlite and not self.patlite.closed: self.patlite.on()
                if self.buzzer and not self.buzzer.closed: self.buzzer.on()
            else:
                if self.patlite and not self.patlite.closed: self.patlite.off()
                if self.buzzer and not self.buzzer.closed: self.buzzer.off()
        except Exception:
            pass

    def closeEvent(self, event):
        self.exit_app()
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    window = AndonWindow()
    key_filter = GlobalKeyFilter(window.exit_app)
    app.installEventFilter(key_filter)
    window.show()
    sys.exit(app.exec())
