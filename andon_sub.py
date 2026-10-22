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

# --- 環境判定 ---
IS_RASPBERRY_PI = False
try:
    with open('/sys/firmware/devicetree/base/model', 'r') as f:
        if 'Raspberry Pi' in f.read():
            IS_RASPBERRY_PI = True
except Exception:
    pass

# ネットワーク設定
SYNC_PORT = 50055  # 親機からの受信ポート

# --- Logging Setup ---
from logging.handlers import RotatingFileHandler
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(APP_DIR, "andon_sub.log")

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
    
    try:
        load1, load5, load15 = os.getloadavg()
        parts.append(f"Load: {load1:.2f},{load5:.2f},{load15:.2f}")
    except Exception:
        pass

    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = float(f.read().strip()) / 1000.0
            parts.append(f"Temp: {temp:.1f}°C")
    except Exception:
        pass

    return " | ".join(parts) if parts else "System stats N/A"

log_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=2 * 1024 * 1024,
    backupCount=2,
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
logger.info(f"=== ANDON SUB APPLICATION STARTED (PID: {os.getpid()}) ===")
logger.info(f"Log file: {LOG_FILE_PATH}")
logger.info(f"Local IP: {get_local_ip()}")
logger.info(f"Initial System Stats: {get_system_stats_str()}")
logger.info("=" * 65)

def handle_exception(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = handle_exception

# --- Sync Thread (子機用: 親機からの受信のみ) ---
class SyncThread(QThread):
    sync_received = pyqtSignal(dict)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(('0.0.0.0', SYNC_PORT))
            logging.info(f"[Sync] Listening for Main Unit on UDP {SYNC_PORT}...")
        except Exception as e:
            logging.error(f"[Sync] Sync Bind Error: {e}")
            return

        while True:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode('utf-8'))
                msg_type = msg.get('type')
                if msg_type in ('sync', 'full_sync'):
                    self.sync_received.emit(msg)
            except Exception as e:
                logging.error(f"[Sync] Error receiving packet: {e}")

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
            time.sleep(3600)

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
        self.setWindowTitle("Andon Sub Unit")
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")

        self.active_calls = {}
        self.finished_timers = {}
        self.call_history = []     # 呼出履歴（最新3件）
        self.finish_history = []   # 作業終了履歴（最新3件）
        self.machine_mapping = {}
        self.slogan_index = 0
        self.last_sync_timestamp = None

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
        # ==========================================
        self.page_idle = QWidget()
        self.page_idle.setStyleSheet("background-color: black;")
        layout_idle = QVBoxLayout(self.page_idle)
        layout_idle.setContentsMargins(40, 15, 40, 12)
        layout_idle.setSpacing(0)

        # --- 1. 上部: 大型デジタル時計 ---
        self.lbl_clock = QLabel()
        self.lbl_clock.setAlignment(Qt.AlignCenter)
        self.lbl_clock.setFont(QFont("Arial", 125, QFont.Bold))
        self.lbl_clock.setStyleSheet("color: #FFFFFF; font-weight: bold; letter-spacing: 4px;")
        layout_idle.addWidget(self.lbl_clock)

        layout_idle.addSpacing(10)

        line1 = QFrame()
        line1.setFrameShape(QFrame.HLine)
        line1.setFrameShadow(QFrame.Sunken)
        line1.setStyleSheet("border: 1px solid #263238;")
        layout_idle.addWidget(line1)

        layout_idle.addSpacing(10)

        # --- 2. 中央: 安全第一マーク ＆ スローガン ---
        self.lbl_safety_badge = QLabel("✚  安 全 第 一  ✚")
        self.lbl_safety_badge.setAlignment(Qt.AlignCenter)
        self.lbl_safety_badge.setFont(QFont("Arial", 32, QFont.Bold))
        self.lbl_safety_badge.setStyleSheet("color: #00E676; letter-spacing: 6px;")
        layout_idle.addWidget(self.lbl_safety_badge)

        layout_idle.addSpacing(8)

        self.lbl_slogan_month = QLabel()
        self.lbl_slogan_month.setAlignment(Qt.AlignCenter)
        self.lbl_slogan_month.setFont(QFont("Arial", 26, QFont.Bold))
        self.lbl_slogan_month.setStyleSheet("color: #FFD54F; letter-spacing: 2px;")
        layout_idle.addWidget(self.lbl_slogan_month)

        layout_idle.addSpacing(6)

        self.lbl_slogan_day = QLabel()
        self.lbl_slogan_day.setAlignment(Qt.AlignCenter)
        self.lbl_slogan_day.setFont(QFont("Arial", 26, QFont.Bold))
        self.lbl_slogan_day.setStyleSheet("color: #FFD54F; letter-spacing: 2px;")
        layout_idle.addWidget(self.lbl_slogan_day)

        # --- 2.5 履歴表示エリア（安全標語の下に左右2列で最新3件を表示：1.5倍フォント28px） ---
        layout_idle.addSpacing(10)

        line_hist = QFrame()
        line_hist.setFrameShape(QFrame.HLine)
        line_hist.setFrameShadow(QFrame.Sunken)
        line_hist.setStyleSheet("border: 1px solid #1c252a;")
        layout_idle.addWidget(line_hist)

        layout_idle.addSpacing(8)

        hist_box = QHBoxLayout()
        hist_box.setContentsMargins(50, 0, 50, 0)
        hist_box.setSpacing(40)

        # 左側: 呼出履歴 (フォントサイズ1.5倍: 28px)
        box_call_hist = QVBoxLayout()
        box_call_hist.setSpacing(6)
        lbl_call_header = QLabel("【 呼出履歴 (最新3件) 】")
        lbl_call_header.setFont(QFont("Arial", 28, QFont.Bold))
        lbl_call_header.setStyleSheet("color: #FFFFFF;")
        box_call_hist.addWidget(lbl_call_header)

        self.lbl_call_history = []
        for _ in range(3):
            lbl = QLabel("•  --")
            lbl.setFont(QFont("Arial", 28, QFont.Bold))
            lbl.setStyleSheet("color: #616161;")
            box_call_hist.addWidget(lbl)
            self.lbl_call_history.append(lbl)
        hist_box.addLayout(box_call_hist, 1)

        # 縦の区切り線
        line_v = QFrame()
        line_v.setFrameShape(QFrame.VLine)
        line_v.setFrameShadow(QFrame.Sunken)
        line_v.setStyleSheet("border: 1px solid #263238;")
        hist_box.addWidget(line_v)

        # 右側: 作業終了履歴 (フォントサイズ1.5倍: 28px)
        box_finish_hist = QVBoxLayout()
        box_finish_hist.setSpacing(6)
        lbl_finish_header = QLabel("【 作業終了履歴 (最新3件) 】")
        lbl_finish_header.setFont(QFont("Arial", 28, QFont.Bold))
        lbl_finish_header.setStyleSheet("color: #FFEA00;")
        box_finish_hist.addWidget(lbl_finish_header)

        self.lbl_finish_history = []
        for _ in range(3):
            lbl = QLabel("•  --")
            lbl.setFont(QFont("Arial", 28, QFont.Bold))
            lbl.setStyleSheet("color: #616161;")
            box_finish_hist.addWidget(lbl)
            self.lbl_finish_history.append(lbl)
        hist_box.addLayout(box_finish_hist, 1)

        layout_idle.addLayout(hist_box)
        layout_idle.addStretch(1)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        line2.setStyleSheet("border: 1px solid #263238;")
        layout_idle.addWidget(line2)

        layout_idle.addSpacing(10)

        # --- 3. 下部: 年月日表示 ＆ 親機同期状態 ---
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

        for key_seq in ["q", "Q", "Escape"]:
            sc = QShortcut(QKeySequence(key_seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(self.exit_app)

        # スプレッドシートマッピングスレッドの起動
        self.mapping_thread = SpreadsheetMappingThread()
        self.mapping_thread.mapping_updated.connect(self.on_mapping_updated)
        self.mapping_thread.start()
        logging.info("[Init] SpreadsheetMappingThread started.")

        # 親機からの信号受信スレッド
        self.sync_thread = SyncThread()
        self.sync_thread.sync_received.connect(self.handle_sync_request)
        self.sync_thread.start()
        logging.info("[Init] SyncThread started.")

        self.keep_awake_timer = QTimer(self)
        self.keep_awake_timer.timeout.connect(self.wake_screen)
        self.keep_awake_timer.start(30000)

        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink_tick)
        self.blink_counters = {'calling': 0, 'finished': 0}
        self.last_wake_time = 0
        self.last_heavy_wake_time = 0

        # 時計更新タイマー (1秒ごと)
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

        # 定期システムリソース監視ログタイマー (2分ごと)
        self.health_check_timer = QTimer(self)
        self.health_check_timer.timeout.connect(self._log_health_check)
        self.health_check_timer.start(120000)

        self.update_clock()
        self.update_net_status()

        # 初期表示: 待機画面を表示
        self.stack_layout.setCurrentWidget(self.page_idle)
        logging.info("[Init] Andon Sub Unit initialized in lightweight mode (No VLC/Video).")

    def update_clock(self):
        """1秒ごとに待機画面の日付と時刻を更新"""
        now = datetime.now()
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        date_str = f"{now.strftime('%Y年%m月%d日')} ({weekdays[now.weekday()]})"
        time_str = now.strftime("%H:%M:%S")

        self.lbl_date.setText(date_str)
        self.lbl_clock.setText(time_str)
        self.update_net_status()

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
        """親機との同期状態を反映"""
        ip = get_local_ip()
        if self.last_sync_timestamp:
            elapsed = time.time() - self.last_sync_timestamp
            last_time_str = time.strftime("%H:%M:%S", time.localtime(self.last_sync_timestamp))
            if elapsed < 10:
                sync_status = f"親機同期: 正常 ({last_time_str})"
            else:
                sync_status = f"親機同期: 待機中 (最終 {last_time_str})"
        else:
            sync_status = "親機同期: 接続待機中"
        self.lbl_status_net.setText(f"子機モード [{sync_status}]  IP: {ip}")

    def _log_health_check(self):
        logging.info(f"[HealthCheck] {get_system_stats_str()} | ActiveCalls={dict(self.active_calls)} | CurrentPage={'Idle' if self.stack_layout.currentWidget() == self.page_idle else 'Andon'}")

    def exit_app(self):
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

            if prev_status == status:
                return
            if status == 'normal' and prev_status is None:
                return

            logging.info(f"[_process_update] State change: machine='{machine_id}', status='{status}' (prev='{prev_status}')")

            if status == 'calling':
                self.active_calls[machine_id] = 'calling'
                now_time = datetime.now().strftime("%H:%M")
                display_text = self._format_machine_id(machine_id)
                item_str = f"{now_time}  呼出：{display_text}"
                if not self.call_history or self.call_history[0] != item_str:
                    self.call_history.insert(0, item_str)
                    self.call_history = self.call_history[:3]
                    self.update_history_display()
            elif status == 'finished':
                self.active_calls[machine_id] = 'finished'
                now_time = datetime.now().strftime("%H:%M")
                display_text = self._format_machine_id(machine_id)
                item_str = f"{now_time}  終了：{display_text}"
                if not self.finish_history or self.finish_history[0] != item_str:
                    self.finish_history.insert(0, item_str)
                    self.finish_history = self.finish_history[:3]
                    self.update_history_display()
            elif status == 'normal':
                if machine_id in self.active_calls:
                    del self.active_calls[machine_id]

            t_dict = time.perf_counter()

            # 画面切り替え
            if self.active_calls:
                if self.stack_layout.currentWidget() != self.page_andon:
                    self.stack_layout.setCurrentWidget(self.page_andon)
                self.update_display()
            else:
                if self.stack_layout.currentWidget() != self.page_idle:
                    self.update_clock()
                    self.stack_layout.setCurrentWidget(self.page_idle)
            t_mode = time.perf_counter()

            self.centralWidget().repaint()
            t_repaint = time.perf_counter()

            if status == 'calling':
                self.blink_counters['calling'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                self.wake_screen()
            elif status == 'finished':
                self.blink_counters['finished'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                
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
            mode_ms = (t_mode - t_dict) * 1000
            paint_ms = (t_repaint - t_mode) * 1000
            wake_ms = (t_wake - t_repaint) * 1000
            log_msg = f"[_process_update] Completed in {total_ms:.1f}ms (mode/disp={mode_ms:.1f}ms, repaint={paint_ms:.1f}ms, wake={wake_ms:.1f}ms)"
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
            if not self.active_calls:
                self.update_clock()
                self.update_history_display()
                self.stack_layout.setCurrentWidget(self.page_idle)

    def update_history_display(self):
        """待機画面の呼出履歴・作業終了履歴（最新3件）を更新"""
        if not hasattr(self, 'lbl_call_history'): return
        for i, lbl in enumerate(self.lbl_call_history):
            if i < len(self.call_history):
                lbl.setText(f"•  {self.call_history[i]}")
                lbl.setStyleSheet("color: #E0E0E0;")
            else:
                lbl.setText("•  --")
                lbl.setStyleSheet("color: #616161;")

        for i, lbl in enumerate(self.lbl_finish_history):
            if i < len(self.finish_history):
                lbl.setText(f"•  {self.finish_history[i]}")
                lbl.setStyleSheet("color: #FFEE58;")
            else:
                lbl.setText("•  --")
                lbl.setStyleSheet("color: #616161;")

    def handle_sync_request(self, data):
        """親機から信号を受信 (個別更新または全状態同期)"""
        self.last_sync_timestamp = time.time()
        msg_type = data.get('type')
        if msg_type == 'full_sync':
            new_calls = data.get('active_calls', {})
            if 'call_history' in data:
                self.call_history = data['call_history']
            if 'finish_history' in data:
                self.finish_history = data['finish_history']
            self.update_history_display()
            if self.active_calls != new_calls:
                t0 = time.perf_counter()
                logging.info(f"[Sync] Full sync update: {new_calls} (prev: {self.active_calls})")
                self.active_calls = dict(new_calls)
                if self.active_calls:
                    if self.stack_layout.currentWidget() != self.page_andon:
                        self.stack_layout.setCurrentWidget(self.page_andon)
                    self.update_display()
                else:
                    if self.stack_layout.currentWidget() != self.page_idle:
                        self.update_clock()
                        self.stack_layout.setCurrentWidget(self.page_idle)
                self.centralWidget().repaint()
                logging.info(f"[Sync] Full sync screen updated in {(time.perf_counter() - t0)*1000:.1f}ms")
        else:
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
