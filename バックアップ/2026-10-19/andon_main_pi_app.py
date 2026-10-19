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
from flask import Flask, request, jsonify
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QListWidget, QListWidgetItem, QStackedLayout
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

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
    print("Running in simulation mode (No Raspberry Pi or gpiozero not found).")
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

# ネットワーク共有設定
MOUNT_POINT = "/home/pi/winshare"
REMOTE_SHARE = "//192.168.10.253/sv-os-mp4"
MOUNT_USER = "admin"
MOUNT_PASS = "Nochida1"

# 動画フォルダパス
SOURCE_VIDEO_DIR = os.path.join(MOUNT_POINT, "第1製造", "注意喚起")
LOCAL_VIDEO_DIR = "/home/pi/andon_videos"

# --- Logging Setup (SDカード保護: RAMディスク(/dev/shm)優先 & 容量制限) ---
from logging.handlers import RotatingFileHandler
LOG_DIR = "/dev/shm" if os.path.exists("/dev/shm") else os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(LOG_DIR, "andon_main.log")

log_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=512 * 1024,  # 最大512KB
    backupCount=1,        # 1世代のみバックアップ保持
    encoding='utf-8'
)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

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
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"status": "error", "message": "Invalid JSON"}), 400
            self.request_received.emit(data)
            return jsonify({"status": "ok"})

        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        
        try:
            app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, use_reloader=False)
        except Exception as e:
            logging.error(f"Flask Server Error: {e}")

# --- UDP Discovery Responder Thread (親機のみ: PCからの探索に応答) ---
class DiscoveryThread(QThread):
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', UDP_PORT))
            print(f"Listening for discovery on UDP {UDP_PORT}...")
        except Exception as e:
            logging.error(f"UDP Bind Error: {e}")
            return

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = data.decode('utf-8')
                if msg.startswith(DISCOVERY_MSG):
                    sock.sendto(RESPONSE_MSG.encode('utf-8'), addr)
            except Exception as e:
                print(f"UDP Error: {e}")

# --- Sync Sender (親機用: 子機へ送信のみ) ---
class SyncSender:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def send_sync(self, data):
        """子機へ状態を一方的に送信"""
        payload = json.dumps({"type": "sync", **data}).encode('utf-8')
        
        # ブロードキャストで子機へ通知
        targets = ['<broadcast>', '255.255.255.255', '10.255.255.255']
        
        for ip in targets:
            try:
                self.sock.sendto(payload, (ip, SYNC_PORT))
            except Exception:
                pass

    def send_full_sync(self, active_calls):
        """子機へ現在の呼出状態全体を送信（子機の後から起動・取りこぼし対策）"""
        payload = json.dumps({"type": "full_sync", "active_calls": active_calls}).encode('utf-8')
        targets = ['<broadcast>', '255.255.255.255', '10.255.255.255']
        for ip in targets:
            try:
                self.sock.sendto(payload, (ip, SYNC_PORT))
            except Exception:
                pass

# --- Video Sync Thread ---
class VideoSyncThread(QThread):
    def run(self):
        if not os.path.exists(MOUNT_POINT):
            try:
                os.makedirs(MOUNT_POINT, exist_ok=True)
            except:
                pass
        
        if not os.path.ismount(MOUNT_POINT):
            try:
                subprocess.run(
                    ['sudo', 'mount', '-t', 'cifs', REMOTE_SHARE, MOUNT_POINT, 
                     '-o', f'user={MOUNT_USER},password={MOUNT_PASS},vers=3.0,iocharset=utf8'],
                    check=True, timeout=30
                )
            except Exception as e:
                logging.error(f"Mount failed: {e}")

        if not os.path.exists(LOCAL_VIDEO_DIR):
            try:
                os.makedirs(LOCAL_VIDEO_DIR, exist_ok=True)
            except Exception:
                return

        remote_files = []
        if os.path.exists(SOURCE_VIDEO_DIR):
            remote_files = glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.mp4"))
            remote_files.extend(glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.MP4")))
        
        if remote_files:
            remote_filenames = {os.path.basename(f) for f in remote_files}
            local_files = glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*"))
            for f in local_files:
                try:
                    if os.path.isfile(f) and os.path.basename(f) not in remote_filenames:
                        os.remove(f)
                except Exception:
                    pass

            for src in remote_files:
                filename = os.path.basename(src)
                dst = os.path.join(LOCAL_VIDEO_DIR, filename)
                if not os.path.exists(dst):
                    try:
                        shutil.copy2(src, dst)
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
        # オンライン取得を試みる
        try:
            req = urllib.request.Request(SPREADSHEET_CSV_URL, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read()
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
                except Exception:
                    pass
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
                    logging.info(f"Loaded {len(mapping)} machine mappings from Google Spreadsheet.")
                    return mapping
        except Exception as e:
            logging.warning(f"Failed to fetch spreadsheet online: {e}. Falling back to cache.")

        # キャッシュ読み込み
        if os.path.exists(CACHE_FILE_PATH):
            try:
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
                    logging.info(f"Loaded {len(mapping)} machine mappings from local cache.")
                    return mapping
            except Exception as e:
                logging.error(f"Failed to read local cache mapping: {e}")

        return mapping

# --- Main Window ---
class AndonWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Andon Main Unit")
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")

        self.active_calls = {}
        self.finished_timers = {}  # 終了表示消去タイマー管理用
        self.is_pulsing = False
        self.is_idle_mode = False
        self.machine_mapping = {}

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.stack_layout = QStackedLayout(central_widget)

        # Page 1: 通常画面
        self.page_normal = QWidget()
        layout_normal = QHBoxLayout(self.page_normal)

        self.list_calling = QListWidget()
        self.list_calling.setFocusPolicy(Qt.NoFocus)
        layout_normal.addWidget(self.list_calling)

        self.list_finished = QListWidget()
        self.list_finished.setFocusPolicy(Qt.NoFocus)
        layout_normal.addWidget(self.list_finished)
        self.stack_layout.addWidget(self.page_normal)

        # Page 2: 動画画面
        self.page_video = QWidget()
        self.page_video.setStyleSheet("background-color: black;")
        self.stack_layout.addWidget(self.page_video)

        self.patlite = None
        self.buzzer = None
        if IS_RASPBERRY_PI:
            try:
                self.patlite = LED(PATLITE_PIN)
                self.buzzer = LED(BUZZER_PIN)
            except Exception as e:
                print(f"GPIO Init Error: {e}")

        # スプレッドシートマッピングスレッドの起動
        self.mapping_thread = SpreadsheetMappingThread()
        self.mapping_thread.mapping_updated.connect(self.on_mapping_updated)
        self.mapping_thread.start()

        # 親機機能の起動
        self.flask_thread = FlaskServerThread()
        self.flask_thread.request_received.connect(self.handle_flask_request)
        self.flask_thread.start()

        self.discovery_thread = DiscoveryThread()
        self.discovery_thread.start()

        self.sync_sender = SyncSender()

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
        self.last_pulse_time = 0
        
        self.video_process = None
        self.grab_timer = None

        self.video_sync_thread = VideoSyncThread()
        self.video_sync_thread.start()

        # 定期的なメモリ解放タイマー (Zero 2 WのRAM圧迫/スワップ防止)
        self.gc_timer = QTimer(self)
        self.gc_timer.timeout.connect(self.clean_memory)
        self.gc_timer.start(1800000)  # 30分ごと

        # 子機への定期全同期タイマー (5秒ごと: 子機の後から起動・ネットワーク瞬断対策)
        self.full_sync_timer = QTimer(self)
        self.full_sync_timer.timeout.connect(self._sync_all_to_sub)
        self.full_sync_timer.start(5000)

        self.wake_screen()
        self.start_idle_mode()

    def _sync_all_to_sub(self):
        """定期的に子機へ最新の呼び出し状態全体を配信"""
        try:
            self.sync_sender.send_full_sync(self.active_calls)
        except Exception:
            pass

    def close_application(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Q or event.text().lower() == 'q':
            self.close()

    def _process_update(self, data):
        t_start = time.time()
        try:
            machine_id = str(data.get('machine_id', 'Unknown'))
            status = data.get('status')
            prev_status = self.active_calls.get(machine_id)
            status_changed = (prev_status != status)

            if status == 'calling':
                self.active_calls[machine_id] = 'calling'
            elif status == 'finished':
                self.active_calls[machine_id] = 'finished'
            elif status == 'normal':
                if machine_id in self.active_calls:
                    del self.active_calls[machine_id]

            if self.active_calls and self.is_idle_mode:
                self.stop_idle_mode()

            if status == 'calling':
                self.wake_screen()
                self.blink_counters['calling'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
            elif status == 'finished':
                self.wake_screen()
                self.blink_counters['finished'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                
                # 既存の消去タイマーがあれば停止して再設定（競合防止）
                if machine_id in self.finished_timers:
                    self.finished_timers[machine_id].stop()
                timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(lambda m=machine_id: self.remove_finished_call(m))
                self.finished_timers[machine_id] = timer
                timer.start(30000)

            if self.active_calls:
                self.idle_start_timer.stop()
                if status_changed:
                    self.update_display()
            else:
                if not self.is_idle_mode and not self.idle_start_timer.isActive():
                    self.idle_start_timer.start(10000)
                    self.update_display()
            
            # GPIO操作を処理の最後に移動（処理遅延によるリレーON延長を防ぐため）
            if status == 'calling':
                self.update_gpio()
            elif status == 'finished':
                self.trigger_pulse()
            else:
                self.update_gpio()

        except Exception as e:
            logging.error(f"Error in process_update: {e}")
        
        duration = time.time() - t_start
        if duration > 0.5: logging.info(f"SLOW: _process_update took {duration:.3f}s")

    def remove_finished_call(self, machine_id):
        if machine_id in self.finished_timers:
            self.finished_timers[machine_id].stop()
            del self.finished_timers[machine_id]
        if self.active_calls.get(machine_id) == 'finished':
            del self.active_calls[machine_id]
            self.update_display()
            self.update_gpio()
            if not self.active_calls and not self.is_idle_mode:
                self.idle_start_timer.start(10000)

    def handle_flask_request(self, data):
        """PCから指令を受信 -> 自身を更新し、子機へ送信"""
        # 子機へ信号を送信
        self.sync_sender.send_sync(data)
        self._process_update(data)

    def wake_screen(self):
        t_start = time.time()
        # 前回の重いコマンドが終わっているか確認してゾンビプロセス回収
        if self.wake_process:
            if self.wake_process.poll() is not None:
                self.wake_process = None

        current_time = time.time()
        if current_time - self.last_wake_time < 5.0:
            return
        self.last_wake_time = current_time

        if IS_RASPBERRY_PI:
            try:
                # 1. マウスカーソルを動かす（軽量・OSのアイドルタイマーリセット）
                current_pos = QCursor.pos()
                QCursor.setPos(current_pos.x() + 1, current_pos.y())
                QApplication.processEvents()
                QCursor.setPos(current_pos)

                # 2. 重いコマンド(subprocess)は頻度を落とす (60秒に1回)
                if current_time - self.last_heavy_wake_time > 60.0:
                    self.last_heavy_wake_time = current_time
                    env = os.environ.copy()
                    if 'DISPLAY' not in env: env['DISPLAY'] = ':0'
                    cmd = "xdg-screensaver reset; xset s off -dpms s noblank dpms force on; vcgencmd display_power 1"
                    # 修正: run(同期)ではなくPopen(非同期)を使う
                    self.wake_process = subprocess.Popen(cmd, shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
        duration = time.time() - t_start
        if duration > 0.5: logging.info(f"SLOW: wake_screen took {duration:.3f}s")

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
                            fs = lbl.property("original_font_size")
                            if fs:
                                lbl.setStyleSheet(f"font-size: {fs}px; font-family: Arial, sans-serif; font-weight: bold; color: {color}; background-color: transparent;")
        update_list(self.list_calling, 'calling')
        update_list(self.list_finished, 'finished')

    def start_idle_mode(self):
        if self.active_calls: return
        self.is_idle_mode = True
        # 念のため、古いVLCプロセスが残っていたら強制終了させる（二重起動・重くなる対策）
        if IS_RASPBERRY_PI:
            subprocess.run(['pkill', '-f', 'vlc'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if self.video_process:
            try: self.video_process.kill()
            except: pass
            self.video_process = None
        
        playlist = []
        if os.path.exists(LOCAL_VIDEO_DIR):
            playlist = sorted(glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.mp4")) + glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.MP4")))

        if playlist:
            try:
                # 動画画面へ切り替え
                self.stack_layout.setCurrentWidget(self.page_video)
                self.page_video.show()
                QApplication.processEvents()

                env = os.environ.copy()
                if 'DISPLAY' not in env: env['DISPLAY'] = ':0'
                cmd = ['vlc', '-I', 'dummy', '--fullscreen', '--loop', '--no-video-title-show', '--no-osd', '--quiet',
                       '--no-keyboard-events', '--no-mouse-events', '--file-caching=1500', '--drop-late-frames', '--skip-frames', '--disable-screensaver', '--no-stats']
                cmd.extend(playlist)
                
                # VLCをQtのウィジェットに埋め込む (これでフォーカス問題を解決)
                if IS_RASPBERRY_PI:
                    cmd.append(f'--drawable-xid={int(self.page_video.winId())}')
                
                self.video_process = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.blink_timer.stop()
                self.grabKeyboard()
                return
            except Exception:
                pass

        self.stack_layout.setCurrentWidget(self.page_normal)
        self.list_finished.hide()
        self.list_calling.clear()
        self.list_finished.clear()
        self.blink_timer.stop()

    def stop_idle_mode(self):
        self.is_idle_mode = False
        if self.video_process:
            try:
                self.video_process.kill()
                self.video_process.wait(timeout=1.0)
            except Exception:
                pass
            self.video_process = None
        
        self.stack_layout.setCurrentWidget(self.page_normal)
        self.releaseKeyboard()
        self.list_finished.show()
        self.idle_start_timer.stop()
        self.activateWindow()
        self.raise_()
        self.showFullScreen()
        QApplication.processEvents()
        gc.collect()

    def clean_memory(self):
        gc.collect()

    def update_display(self):
        t_start = time.time()
        if self.is_idle_mode: return
        self.list_calling.clear()
        self.list_finished.clear()
        self.list_calling.setStyleSheet("background-color: black;")
        self.list_finished.setStyleSheet("background-color: black;")
        if not self.active_calls: return

        number_size = 200
        prefix_size = int(number_size / 4)
        
        for m_id, status in self.active_calls.items():
            display_text = self._format_machine_id(m_id)
            if status == 'calling':
                color = self._get_blink_color('calling')
                self._add_list_item(self.list_calling, "呼出：", display_text, color, prefix_size, number_size, user_data=m_id)
            elif status == 'finished':
                color = self._get_blink_color('finished')
                self._add_list_item(self.list_finished, "終了：", display_text, color, prefix_size, number_size, user_data=m_id)
        duration = time.time() - t_start
        if duration > 0.5: logging.info(f"SLOW: update_display took {duration:.3f}s")

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
        base_style = f"font-family: Arial, sans-serif; font-weight: bold; color: {color}; background-color: transparent;"
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        lbl_prefix = QLabel(prefix_text)
        lbl_prefix.setStyleSheet(f"font-size: {p_size}px; {base_style}")
        lbl_prefix.setProperty("original_font_size", p_size)
        lbl_num = QLabel(number_text)
        lbl_num.setStyleSheet(f"font-size: {n_size}px; {base_style}")
        lbl_num.setProperty("original_font_size", n_size)
        layout.addWidget(lbl_prefix)
        layout.addWidget(lbl_num)
        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
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
        
        # チャタリング防止：前回のパルス開始から0.5秒以内は再点灯させない
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
        
        # QTimerではなくスレッドタイマーを使用（メインスレッドの処理遅延の影響を受けないようにする）
        threading.Timer(0.2, self.end_pulse_worker).start()

    def end_pulse_worker(self):
        self.is_pulsing = False
        try:
            # スレッドセーフに辞書をコピーして確認
            current_calls = self.active_calls.copy()
            any_calling = any(s == 'calling' for s in current_calls.values())
            
            if any_calling:
                # 呼び出し中ならONを維持
                if self.patlite and not self.patlite.closed: self.patlite.on()
                if self.buzzer and not self.buzzer.closed: self.buzzer.on()
            else:
                # それ以外ならOFF
                if self.patlite and not self.patlite.closed: self.patlite.off()
                if self.buzzer and not self.buzzer.closed: self.buzzer.off()
        except Exception:
            pass

    def closeEvent(self, event):
        if self.patlite:
            self.patlite.close()
            self.patlite = None
        if self.buzzer:
            self.buzzer.close()
            self.buzzer = None
        self.releaseKeyboard()
        if self.video_process:
            try:
                self.video_process.kill()
                self.video_process.wait(timeout=1.0)
            except Exception:
                pass
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    window = AndonWindow()
    window.show()
    sys.exit(app.exec())
