import sys
import socket
import json
import threading
import time
import gc
import shutil
import glob
import logging
import subprocess
import os
import signal

ACTIVE_WINDOW = None

# --- Wayland環境対策: X11(Xwayland)互換モードで動作させる ---
# これにより、VLCのウィジェット埋め込み(--drawable-xid)やキー監視が正常に動作します
os.environ['QT_QPA_PLATFORM'] = 'xcb'

from PyQt5.QtGui import QCursor, QKeySequence, QFont, QPalette, QColor
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QListWidget, QListWidgetItem, QStackedLayout, QShortcut
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

# ネットワーク共有設定
MOUNT_POINT = "/home/pi/winshare"
REMOTE_SHARE = "//192.168.10.253/sv-os-mp4"
MOUNT_USER = "admin"
MOUNT_PASS = "Nochida1"

# 動画フォルダパス
SOURCE_VIDEO_DIR = os.path.join(MOUNT_POINT, "第1製造", "注意喚起")
LOCAL_VIDEO_DIR = "/home/pi/andon_videos"

# --- Logging Setup (再起動後も調査できるよう永続化 + 自動ローテーション + コンソール出力) ---
from logging.handlers import RotatingFileHandler
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(APP_DIR, "andon_sub.log")

# システム統計（CPU、メモリ、温度など）を取得するヘルパー
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
logger.info(f"=== ANDON SUB APPLICATION STARTED (PID: {os.getpid()}) ===")
logger.info(f"Log file: {LOG_FILE_PATH}")
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
                # 親機からのsyncまたはfull_sync信号を処理
                if msg_type in ('sync', 'full_sync'):
                    self.sync_received.emit(msg)
            except Exception as e:
                logging.error(f"[Sync] Error receiving packet: {e}")

# --- Video Sync Thread ---
class VideoSyncThread(QThread):
    def run(self):
        logging.info("[VideoSync] Thread started. Checking CIFS mount and remote share...")
        t_sync_start = time.perf_counter()
        if not os.path.exists(MOUNT_POINT):
            try:
                os.makedirs(MOUNT_POINT, exist_ok=True)
            except Exception as e:
                logging.error(f"[VideoSync] Failed to create mount dir {MOUNT_POINT}: {e}")
        
        if not os.path.ismount(MOUNT_POINT):
            logging.info(f"[VideoSync] CIFS not mounted. Mounting {REMOTE_SHARE} -> {MOUNT_POINT} (timeout 30s)...")
            t_m = time.perf_counter()
            try:
                subprocess.run(
                    ['sudo', 'mount', '-t', 'cifs', REMOTE_SHARE, MOUNT_POINT, 
                     '-o', f'user={MOUNT_USER},password={MOUNT_PASS},vers=3.0,iocharset=utf8'],
                    check=True, timeout=30
                )
                logging.info(f"[VideoSync] CIFS mount successful in {time.perf_counter() - t_m:.2f}s")
            except subprocess.TimeoutExpired:
                logging.error(f"[VideoSync] CIFS mount timed out after 30s! Remote server {REMOTE_SHARE} may be offline.")
            except Exception as e:
                logging.error(f"[VideoSync] Mount failed: {e}")

        if not os.path.exists(LOCAL_VIDEO_DIR):
            try:
                os.makedirs(LOCAL_VIDEO_DIR, exist_ok=True)
            except Exception as e:
                logging.error(f"[VideoSync] Failed to create local video dir {LOCAL_VIDEO_DIR}: {e}")
                return

        remote_files = []
        if os.path.exists(SOURCE_VIDEO_DIR):
            remote_files = glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.mp4"))
            remote_files.extend(glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.MP4")))
            logging.info(f"[VideoSync] Found {len(remote_files)} video(s) on remote server: {[os.path.basename(f) for f in remote_files]}")
        else:
            logging.warning(f"[VideoSync] Source video dir does not exist: {SOURCE_VIDEO_DIR}")
        
        if remote_files:
            remote_filenames = {os.path.basename(f) for f in remote_files}
            local_files = glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*"))
            for f in local_files:
                try:
                    if os.path.isfile(f) and os.path.basename(f) not in remote_filenames:
                        logging.info(f"[VideoSync] Deleting local obsolete video: {f}")
                        os.remove(f)
                except Exception as e:
                    logging.error(f"[VideoSync] Error deleting {f}: {e}")

            copied_count = 0
            for src in remote_files:
                filename = os.path.basename(src)
                dst = os.path.join(LOCAL_VIDEO_DIR, filename)
                if not os.path.exists(dst):
                    try:
                        fsize_mb = os.path.getsize(src) / (1024 * 1024)
                        logging.info(f"[VideoSync] Downloading new video '{filename}' ({fsize_mb:.1f}MB) to local SD card...")
                        t_cp = time.perf_counter()
                        shutil.copy2(src, dst)
                        el_cp = time.perf_counter() - t_cp
                        mb_s = (fsize_mb / el_cp) if el_cp > 0 else 0
                        logging.info(f"[VideoSync] Copied '{filename}' in {el_cp:.2f}s ({mb_s:.1f}MB/s)")
                        copied_count += 1
                    except Exception as e:
                        logging.error(f"[VideoSync] Failed copying '{filename}': {e}")
            logging.info(f"[VideoSync] Video sync completed in {time.perf_counter() - t_sync_start:.2f}s (new files copied: {copied_count})")
        else:
            logging.info(f"[VideoSync] Video sync finished in {time.perf_counter() - t_sync_start:.2f}s (no remote files to copy)")

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
        # オンライン取得を試みる
        try:
            req = urllib.request.Request(SPREADSHEET_CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
                el_fetch = time.perf_counter() - t_start
                logging.info(f"[Spreadsheet] Downloaded CSV in {el_fetch:.2f}s ({len(content)} bytes)")
                # キャッシュへ保存 (変更がある場合のみ書き込んでSDカードの劣化を防ぐ)
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

        # キャッシュ読み込み
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
    def __init__(self):
        super().__init__()
        global ACTIVE_WINDOW
        ACTIVE_WINDOW = self
        self.setWindowTitle("Andon Sub Unit")
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")

        self.active_calls = {}
        self.finished_timers = {}  # 終了表示消去タイマー管理用
        self.is_idle_mode = False
        self.machine_mapping = {}

        # ネイティブフォントオブジェクトをキャッシュ（フォントサイズ80% = 160px）
        self.font_number = QFont("Arial", 160, QFont.Bold)
        self.font_prefix = QFont("Arial", 40, QFont.Bold)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.stack_layout = QStackedLayout(central_widget)
        self.stack_layout.setStackingMode(QStackedLayout.StackAll)

        # Page 1: 通常画面 (アンドン表示レイヤー)
        self.page_normal = QWidget()
        self.page_normal.setStyleSheet("background-color: black;")
        self.page_normal.setAutoFillBackground(True)
        self.page_normal.setAttribute(Qt.WA_OpaquePaintEvent, True)
        layout_normal = QHBoxLayout(self.page_normal)
        layout_normal.setContentsMargins(0, 0, 0, 0)
        layout_normal.setSpacing(0)

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
        layout_normal.addWidget(self.list_calling)

        self.list_finished = QListWidget()
        self.list_finished.setFocusPolicy(Qt.NoFocus)
        self.list_finished.setSpacing(0)
        self.list_finished.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_finished.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_finished.setStyleSheet(list_style)
        layout_normal.addWidget(self.list_finished)
        self.stack_layout.addWidget(self.page_normal)

        # Page 2: 動画画面 (動画常時再生レイヤー)
        self.page_video = QWidget()
        self.page_video.setStyleSheet("background-color: black;")
        self.page_video.setAutoFillBackground(True)
        self.page_video.setFocusPolicy(Qt.StrongFocus)
        self.stack_layout.addWidget(self.page_video)

        # X11ネイティブウィンドウ化（Z-orderの物理スタックを可能にする）
        _ = self.page_normal.winId()
        _ = self.page_video.winId()

        # アプリ全体で確実に終了キーを捕捉するショートカット (どのウィジェットにフォーカスがあっても有効)
        for key_seq in ["q", "Q", "Escape"]:
            sc = QShortcut(QKeySequence(key_seq), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(self.exit_app)

        # 子機機能の起動 (Flask, Discoveryは親機のみ)
        
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
        
        self.idle_start_timer = QTimer(self)
        self.idle_start_timer.setSingleShot(True)
        self.idle_start_timer.timeout.connect(self.start_idle_mode)

        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink_tick)
        self.blink_counters = {'calling': 0, 'finished': 0}
        self.last_wake_time = 0
        self.last_heavy_wake_time = 0
        self.wake_process = None
        
        self.video_process = None
        self.grab_timer = None

        self.video_sync_thread = VideoSyncThread()
        self.video_sync_thread.start()
        logging.info("[Init] VideoSyncThread started.")

        # 定期的なメモリ解放タイマー (Zero 2 WのRAM圧迫/スワップ防止)
        self.gc_timer = QTimer(self)
        self.gc_timer.timeout.connect(self.clean_memory)
        self.gc_timer.start(1800000)  # 30分ごと

        # 定期システムリソース監視ログタイマー (2分ごと)
        self.health_check_timer = QTimer(self)
        self.health_check_timer.timeout.connect(self._log_health_check)
        self.health_check_timer.start(120000)

        # 【初回呼び出し遅延の根本対策】
        # 起動時にアンドン画面（page_normal）を物理画面に露出（Expose）させ、
        # OS起動待機時間中にX11/GPUサーフェス確保とフォントラスタライズを100%完了させる
        logging.info("[Init] Setting up display warmup (GPU & font pre-rendering)...")
        self.active_calls['0'] = 'calling'
        self.update_display()
        self.stack_layout.setCurrentWidget(self.page_normal)
        self.page_normal.raise_()

        # 起動後1.5秒（X11ウィンドウ露出完了後）にダミー表示を解除して動画アイドルモードへ遷移
        QTimer.singleShot(1500, self._initial_warmup_and_start_idle)
        logging.info("[Init] AndonWindow initialized. Warmup timer scheduled for 1500ms.")

    def _initial_warmup_and_start_idle(self):
        """起動直後の1.5秒後に実行: 物理画面でのGPU/フォント初期化を終えて動画アイドルモードへ移行"""
        logging.info("[Init] Warmup complete. Clearing dummy calls and transitioning to idle mode...")
        if '0' in self.active_calls:
            del self.active_calls['0']
        self.update_display()
        self.wake_screen()
        # 起動中に既に呼び出しを受信していなければ動画再生へ移行
        if not self.active_calls:
            self.start_idle_mode()
        logging.info("[Init] Display and idle mode fully ready.")

    def _log_health_check(self):
        vlc_pid = self.video_process.pid if (self.video_process and self.video_process.poll() is None) else "None"
        logging.info(f"[HealthCheck] {get_system_stats_str()} | ActiveCalls={dict(self.active_calls)} | IdleMode={self.is_idle_mode} | VLC_PID={vlc_pid}")

    def fast_kill_video(self):
        """VLCプロセスを一撃で即死させ、動画のフレーム送信を完全に遮断する (待機時間0ms)"""
        proc = self.video_process
        self.video_process = None
        if proc:
            try:
                # setsidによりproc.pidがプロセスグループIDなので、一撃でグループ丸ごとSIGKILL
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def exit_app(self):
        """動画再生中や裏スレッド動作中でも安全かつ即座に終了する"""
        try:
            self.fast_kill_video()
        except Exception:
            pass
        try:
            if hasattr(self, 'grab_timer') and self.grab_timer and self.grab_timer.isActive():
                self.grab_timer.stop()
        except Exception:
            pass
        if IS_RASPBERRY_PI:
            try:
                subprocess.run(['pkill', '-9', '-f', 'vlc'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        try:
            self.releaseKeyboard()
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

            # 【重複パケットガード】既に同じ状態なら描画やモード変更を即座にスキップ（0ms）
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

            # 1. 【超最優先】動画モード解除 または アンドン画面描画
            if self.active_calls:
                self.idle_start_timer.stop()
                if self.is_idle_mode:
                    self.stop_idle_mode()
                else:
                    self.update_display()
            else:
                if not self.is_idle_mode and not self.idle_start_timer.isActive():
                    self.idle_start_timer.start(10000)
                    self.update_display()
            t_mode = time.perf_counter()

            # 2. 直ちに画面を強制再描画 (repaint) して即時反映 (二重描画を防ぐためpage_normalのみ)
            self.page_normal.repaint()
            t_repaint = time.perf_counter()

            # 3. 画面描画完了後にタイマー・スクリーンセーバー解除を実行
            if status == 'calling':
                self.blink_counters['calling'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                self.wake_screen()
            elif status == 'finished':
                self.blink_counters['finished'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                
                # 既存の消去タイマーがあれば停止して再設定
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
            if not self.active_calls and not self.is_idle_mode:
                self.idle_start_timer.start(10000)

    def handle_sync_request(self, data):
        """親機から信号を受信 (個別更新または全状態同期)"""
        msg_type = data.get('type')
        if msg_type == 'full_sync':
            new_calls = data.get('active_calls', {})
            # 親機の状態と差分がある場合のみ画面更新（無駄な再描画防止）
            if self.active_calls != new_calls:
                t0 = time.perf_counter()
                logging.info(f"[Sync] Full sync update: {new_calls} (prev: {self.active_calls})")
                self.active_calls = dict(new_calls)
                if self.active_calls:
                    if self.is_idle_mode:
                        self.stop_idle_mode()
                    self.idle_start_timer.stop()
                else:
                    if not self.is_idle_mode and not self.idle_start_timer.isActive():
                        self.idle_start_timer.start(10000)
                self.update_display()
                self.page_normal.repaint()
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
                # 1. マウスカーソルを動かす（OSのアイドルタイマーリセット）
                current_pos = QCursor.pos()
                QCursor.setPos(current_pos.x() + 1, current_pos.y())
                QCursor.setPos(current_pos)

                # 2. 重いコマンド(subprocess)はバックグラウンドスレッドで非同期実行（UIメインスレッドをブロックしない）
                if current_time - self.last_heavy_wake_time > 60.0:
                    self.last_heavy_wake_time = current_time
                    def _async_wake_worker():
                        try:
                            env = os.environ.copy()
                            if 'DISPLAY' not in env: env['DISPLAY'] = ':0'
                            cmd = "xdg-screensaver reset; xset s off -dpms s noblank dpms force on; vcgencmd display_power 1"
                            subprocess.run(cmd, shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                            logging.info("[WakeScreen] Display wake command completed asynchronously.")
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
        """スタイルシートを使わずQPaletteで文字色のみを直接変更 (フォントサイズ破壊バグを根絶 & 0ms)"""
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
        if self.is_idle_mode: return
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

    def _reclaim_focus(self):
        """動画再生開始時にQtウィンドウのキーボード入力を確実に確保する（1回のみ実行）"""
        if not self.is_idle_mode:
            return
        try:
            self.setFocus()
            self.page_video.setFocus()
            self.grabKeyboard()
        except Exception:
            pass

    def _launch_vlc_process(self):
        """VLCプロセスを常時ループ再生として1度だけ起動する"""
        playlist = []
        if os.path.exists(LOCAL_VIDEO_DIR):
            playlist = sorted(glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.mp4")) + glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.MP4")))

        if not playlist:
            logging.warning(f"[Video] No MP4 files in {LOCAL_VIDEO_DIR}. Waiting for video sync...")
            return False

        logging.info(f"[Video] Starting continuous VLC playback with {len(playlist)} video(s): {[os.path.basename(f) for f in playlist]}")
        t0 = time.perf_counter()
        try:
            env = os.environ.copy()
            if 'DISPLAY' not in env: env['DISPLAY'] = ':0'
            
            # 常時バックグラウンド再生:
            # - nice 19 (最低優先度) で起動し、Python/アンドン描画へのCPU奪取を完全に防ぐ
            # - --file-caching=300 でメモリバッファを最小化
            cmd = ['vlc', '-I', 'dummy', '--loop', '--no-video-title-show', '--no-osd',
                   '--no-keyboard-events', '--no-mouse-events',
                   '--file-caching=300',
                   '--disable-screensaver', '--no-stats']
            cmd.extend(playlist)
            
            if IS_RASPBERRY_PI:
                cmd.append(f'--drawable-xid={int(self.page_video.winId())}')
            else:
                cmd.append('--fullscreen')
            
            def _vlc_preexec():
                if hasattr(os, 'setsid'):
                    os.setsid()
                try:
                    os.nice(19)  # 最低CPU優先度: Python/アンドン描画への割り込みをゼロにする
                except Exception:
                    pass

            self.video_process = subprocess.Popen(
                cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=_vlc_preexec
            )
            logging.info(f"[Video] VLC process spawned (PID={self.video_process.pid}) in {(time.perf_counter() - t0)*1000:.1f}ms")
            return True
        except Exception as e:
            logging.error(f"[Video] Failed to start VLC: {e}")
            return False

    def start_idle_mode(self):
        if self.active_calls: return
        logging.info("[Mode] Transitioning to IDLE mode (video layer)...")
        t0 = time.perf_counter()
        self.is_idle_mode = True
        
        # VLCがまだ起動していない（または落ちた）場合のみ起動
        if self.video_process is None or self.video_process.poll() is not None:
            self._launch_vlc_process()

        # 【レイヤー切り替え】動画レイヤーを最前面に浮上させる（ゼロ秒即時表示）
        self.stack_layout.setCurrentWidget(self.page_video)
        self.page_normal.lower()
        self.page_video.raise_()
        self.page_video.setFocus()
        self.blink_timer.stop()
        
        QTimer.singleShot(300, self._reclaim_focus)
        logging.info(f"[Mode] Entered IDLE mode in {(time.perf_counter() - t0)*1000:.1f}ms")

    def stop_idle_mode(self):
        if not self.is_idle_mode:
            return
        logging.info("[Mode] Transitioning to ACTIVE mode (andon layer)...")
        t0 = time.perf_counter()
        self.is_idle_mode = False

        self.idle_start_timer.stop()

        # 【超最優先・レイヤー切り替え】VLCは停止せず、アンドン画面レイヤーを最前面に浮上させる！（0ミリ秒即時切り替え）
        self.stack_layout.setCurrentWidget(self.page_normal)
        self.page_video.lower()
        self.page_normal.raise_()
        self.list_finished.show()
        
        # アンドン画面を即時更新 (repaintは呼出元で1回だけ実行して二重描画を防止)
        self.update_display()

        # キーボード解放
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        logging.info(f"[Mode] Left IDLE mode in {(time.perf_counter() - t0)*1000:.1f}ms")

    def clean_memory(self):
        gc.collect()

    def update_display(self):
        t_start = time.perf_counter()
        if self.is_idle_mode: return
        self.list_calling.clear()
        self.list_finished.clear()
        if not self.active_calls: return

        # 呼び出し件数に応じた最適なフォントサイズの決定
        calling_count = sum(1 for s in self.active_calls.values() if s == 'calling')
        finished_count = sum(1 for s in self.active_calls.values() if s == 'finished')
        max_rows = max(calling_count, finished_count)

        # 基本フォントサイズ: 200pxの80% = 160px (行間を文字が引っ付くぐらい最小化)
        # 1〜5件までは 160px でピッタリ収まる（行高さ約170px * 5 = 850px < 画面高1080px）
        # 6件以上の場合は画面縦幅（1080px）に収まるよう自動縮小して見切れ・崩れを防止
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
        if self.active_calls and not self.is_idle_mode:
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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignCenter)
        
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

        item = QListWidgetItem()
        # 行間を文字が引っ付くぐらい最小にする（文字フォント高さの約1.05倍）
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
