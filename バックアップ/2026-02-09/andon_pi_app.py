import sys
import socket
import json
import time
import gc
import shutil
import glob
import re
import signal
import logging
import subprocess
import os
from flask import Flask, request, jsonify
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QListWidget, QListWidgetItem, QStackedLayout
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer

# --- GPIO設定 (gpiozeroを使用) ---
try:
    from gpiozero import LED, Buzzer
    IS_RASPBERRY_PI = True
except ImportError:
    IS_RASPBERRY_PI = False
    print("gpiozero not found. Running in simulation mode.")

# ピン番号の設定 (環境に合わせて変更してください)
PATLITE_PIN = 23  # パトライト用GPIOピン (GPIO14は起動時にHIGHになるため変更)
BUZZER_PIN = 24   # ブザー用GPIOピン

# ネットワーク設定
UDP_PORT = 50050
HTTP_PORT = 5000
SYNC_PORT = 50055  # アンドン同士の同期用ポート
DISCOVERY_MSG = "DISCOVER_ANDON"
RESPONSE_MSG = "ANDON_HERE"

# ネットワーク共有設定
MOUNT_POINT = "/home/pi/winshare"
REMOTE_SHARE = "//192.168.10.253/sv-os-mp4"
MOUNT_USER = "admin"
MOUNT_PASS = "Nochida1"

# ネットワーク上の動画フォルダパス
SOURCE_VIDEO_DIR = os.path.join(MOUNT_POINT, "第1製造", "注意喚起")

# ローカル保存用ディレクトリ
LOCAL_VIDEO_DIR = "/home/pi/andon_videos"

# --- Logging Setup ---
logging.basicConfig(
    filename='andon_system.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def handle_exception(exc_type, exc_value, exc_traceback):
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)

sys.excepthook = handle_exception

# --- Flask Server Thread ---
class FlaskServerThread(QThread):
    request_received = pyqtSignal(dict)

    def run(self):
        app = Flask(__name__)

        @app.route('/api/call', methods=['POST'])
        def receive_call():
            data = request.json
            self.request_received.emit(data)
            return jsonify({"status": "ok"})

        # ログ出力を抑制して実行
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.ERROR)
        
        try:
            app.run(host='0.0.0.0', port=HTTP_PORT, debug=False, use_reloader=False)
        except Exception as e:
            logging.error(f"Flask Server Error: {e}")

# --- UDP Discovery Responder Thread ---
class DiscoveryThread(QThread):
    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # ポート再利用設定（再起動時のエラー防止）
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('0.0.0.0', UDP_PORT))
            print(f"Listening for discovery on UDP {UDP_PORT}...")
            
            # 自分のIPアドレスを表示（デバッグ用）
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 1))
                my_ip = s.getsockname()[0]
                s.close()
                print(f"My IP Address: {my_ip}", flush=True)
            except:
                print("Could not determine IP address", flush=True)
        except Exception as e:
            logging.error(f"UDP Bind Error: {e}")
            return

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = data.decode('utf-8')
                if msg.startswith(DISCOVERY_MSG):
                    print(f"Discovery request from {addr}", flush=True)
                    sock.sendto(RESPONSE_MSG.encode('utf-8'), addr)
            except Exception as e:
                print(f"UDP Error: {e}")

# --- Sync Thread (アンドン間同期) ---
class SyncThread(QThread):
    sync_received = pyqtSignal(dict)

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(('0.0.0.0', SYNC_PORT))
        except Exception as e:
            logging.error(f"Sync Bind Error: {e}")
            return

        while True:
            try:
                data, addr = sock.recvfrom(1024)
                msg = json.loads(data.decode('utf-8'))
                # 自分自身からのパケットでなければ処理する
                if msg.get('type') == 'sync':
                    self.sync_received.emit(msg)
            except Exception:
                pass

    def send_sync(self, data):
        """他のアンドンへ状態をブロードキャスト"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        payload = json.dumps({"type": "sync", **data}).encode('utf-8')
        
        # 届きそうな範囲に拡散
        targets = ['<broadcast>', '255.255.255.255', '10.255.255.255']
        
        for ip in targets:
            try:
                sock.sendto(payload, (ip, SYNC_PORT))
            except:
                pass

# --- Video Sync Thread ---
class VideoSyncThread(QThread):
    def run(self):
        # 1. マウント処理
        if not os.path.exists(MOUNT_POINT):
            try:
                os.makedirs(MOUNT_POINT, exist_ok=True)
            except:
                pass
        
        # マウントされていない場合のみマウント実行
        if not os.path.ismount(MOUNT_POINT):
            try:
                subprocess.run(
                    ['sudo', 'mount', '-t', 'cifs', REMOTE_SHARE, MOUNT_POINT, 
                     '-o', f'user={MOUNT_USER},password={MOUNT_PASS},vers=3.0,iocharset=utf8'],
                    check=True, timeout=30
                )
                print("Mount successful", flush=True)
            except Exception as e:
                logging.error(f"Mount failed: {e}")

        if not os.path.exists(LOCAL_VIDEO_DIR):
            try:
                os.makedirs(LOCAL_VIDEO_DIR, exist_ok=True)
            except Exception as e:
                logging.error(f"Failed to create local video dir: {e}")
                return

        # 2. ネットワーク上のMP4ファイルを取得
        remote_files = []
        if os.path.exists(SOURCE_VIDEO_DIR):
            remote_files = glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.mp4"))
            remote_files.extend(glob.glob(os.path.join(SOURCE_VIDEO_DIR, "*.MP4")))
        
        if remote_files:
            print(f"Found {len(remote_files)} videos on network. Refreshing local cache...", flush=True)
            
            # ローカルの動画ファイルを一度全て削除
            local_files = glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*"))
            for f in local_files:
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except Exception as e:
                    logging.error(f"Failed to delete local file {f}: {e}")

            # リモートからコピー
            for src in remote_files:
                filename = os.path.basename(src)
                dst = os.path.join(LOCAL_VIDEO_DIR, filename)
                
                try:
                    print(f"Copying video: {filename}", flush=True)
                    shutil.copy2(src, dst)
                except Exception as e:
                    logging.error(f"Video sync error for {filename}: {e}")
        else:
            print("No remote videos found. Keeping existing local videos.", flush=True)
            logging.info("No remote mp4 files found or directory inaccessible. Using local cache.")

# --- Main Window ---
class AndonWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Andon Display")
        self.showFullScreen()
        self.setStyleSheet("background-color: black; color: white;")

        # データ管理
        self.active_calls = {} # 機械IDとステータスの辞書 {id: status}
        self.is_pulsing = False # リレーの一時動作フラグ
        
        # アイドルモード（スライドショー）管理
        self.is_idle_mode = False

        # UI構築
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        self.stack_layout = QStackedLayout(central_widget)

        # Page 1: 通常画面
        self.page_normal = QWidget()
        layout_normal = QHBoxLayout(self.page_normal)

        # 左半分：呼び出し用
        self.list_calling = QListWidget()
        self.list_calling.setFocusPolicy(Qt.NoFocus)
        layout_normal.addWidget(self.list_calling)

        # 右半分：終了用
        self.list_finished = QListWidget()
        self.list_finished.setFocusPolicy(Qt.NoFocus)
        layout_normal.addWidget(self.list_finished)
        self.stack_layout.addWidget(self.page_normal)

        # Page 2: 動画画面
        self.page_video = QWidget()
        self.page_video.setStyleSheet("background-color: black;")
        self.stack_layout.addWidget(self.page_video)

        # GPIO初期化
        self.patlite = None
        self.buzzer = None
        if IS_RASPBERRY_PI:
            try:
                self.patlite = LED(PATLITE_PIN)
                self.buzzer = LED(BUZZER_PIN) # ブザーもLEDクラスで制御（ON/OFFのみならこれで十分）
                print("GPIO Initialized successfully.", flush=True)
                # Wi-Fi省電力モードをOFFにする（通信安定化・フリーズ対策）
                try:
                    subprocess.run(['sudo', 'iw', 'dev', 'wlan0', 'set', 'power_save', 'off'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except: pass
            except Exception as e:
                print(f"GPIO Init Error: {e}. Running without GPIO.", flush=True)

        # サーバー起動
        self.flask_thread = FlaskServerThread()
        self.flask_thread.request_received.connect(self.handle_flask_request)
        self.flask_thread.start()

        # 自動認識起動
        self.discovery_thread = DiscoveryThread()
        self.discovery_thread.start()

        # 同期スレッド起動
        self.sync_thread = SyncThread()
        self.sync_thread.sync_received.connect(self.handle_sync_request)
        self.sync_thread.start()

        # スクリーンセーバー対策：定期的に画面を点灯させる (30秒ごとに強化)
        self.keep_awake_timer = QTimer(self)
        self.keep_awake_timer.timeout.connect(self.wake_screen)
        self.keep_awake_timer.start(30000)
        
        # アイドル遷移用タイマー (10秒)
        self.idle_start_timer = QTimer(self)
        self.idle_start_timer.setSingleShot(True)
        self.idle_start_timer.timeout.connect(self.start_idle_mode)

        # 点滅制御用
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink_tick)
        self.blink_counters = {'calling': 0, 'finished': 0}
        self.last_wake_time = 0 # 画面点灯コマンドの間引き用
        
        # 動画再生プロセス管理
        self.video_process = None

        # 動画同期スレッド起動
        self.video_sync_thread = VideoSyncThread()
        self.video_sync_thread.start()

        # 起動時に画面点灯設定を適用
        self.wake_screen()

        # アプリ起動時はアイドルモードにする
        self.start_idle_mode()

    def close_application(self, event):
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Q or event.text().lower() == 'q':
            self.close()

    def _process_update(self, data):
        """画面とGPIOの更新処理（共通部分）"""
        try:
            machine_id = str(data.get('machine_id', 'Unknown'))
            status = data.get('status')

            # 状態変更検知（前回と同じなら重い描画処理をスキップするため）
            prev_status = self.active_calls.get(machine_id)
            status_changed = (prev_status != status)

            # 1. 内部状態の更新
            if status == 'calling':
                self.active_calls[machine_id] = 'calling'
            elif status == 'finished':
                self.active_calls[machine_id] = 'finished'
            elif status == 'normal':
                if machine_id in self.active_calls:
                    del self.active_calls[machine_id]

            # 2. リレー操作 (最優先で実行)
            if status == 'calling':
                self.update_gpio() # 常時ON
            elif status == 'finished':
                self.trigger_pulse() # 1秒ON
            else:
                self.update_gpio() # normal時など

            # 3. 動画を止める
            if self.active_calls and self.is_idle_mode:
                self.stop_idle_mode()

            # 4. 画面点灯・点滅設定などの付随処理
            if status == 'calling':
                self.wake_screen()
                # 点滅のみ (15tick * 100ms = 1.5秒)
                self.blink_counters['calling'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100) # 100ms間隔に緩和して負荷低減
            elif status == 'finished':
                self.wake_screen()
                # 点滅のみ
                self.blink_counters['finished'] = 15
                if not self.blink_timer.isActive():
                    self.blink_timer.start(100)
                # 10秒後に消去する
                QTimer.singleShot(30000, lambda: self.remove_finished_call(machine_id))

            # 5. テキストを表示する (画面更新)
            if self.active_calls:
                # 状態が変わった時だけ再描画して負荷を下げる
                if status_changed:
                    self.update_display()
            else:
                # 呼び出しがない場合
                if not self.is_idle_mode and not self.idle_start_timer.isActive():
                    self.idle_start_timer.start(10000)
                    self.update_display() # 画面クリア
            
        except Exception as e:
            logging.error(f"Error in handle_request: {e}")

    def remove_finished_call(self, machine_id):
        if self.active_calls.get(machine_id) == 'finished':
            del self.active_calls[machine_id]
            self.update_display()
            self.update_gpio()
            
            # 全て消えたらアイドルタイマー開始
            if not self.active_calls and not self.is_idle_mode:
                self.idle_start_timer.start(10000)

    def handle_flask_request(self, data):
        """PCから直接指令が来た場合 -> 更新して、他のアンドンへ転送"""
        self._process_update(data)
        # 他のアンドンへ同期信号を送る
        self.sync_thread.send_sync(data)

    def handle_sync_request(self, data):
        """他のアンドンから転送されてきた場合 -> 更新のみ（転送しない）"""
        self._process_update(data)

    def wake_screen(self):
        """画面のスリープを解除する"""
        # 【軽量化】頻繁なコマンド実行を防ぐ（5秒に1回まで）
        current_time = time.time()
        if current_time - self.last_wake_time < 5.0:
            return
        self.last_wake_time = current_time

        if IS_RASPBERRY_PI:
            try:
                # 環境変数を準備（自動起動時などにDISPLAYがない場合への対策）
                env = os.environ.copy()
                if 'DISPLAY' not in env:
                    env['DISPLAY'] = ':0'

                # 1. マウスカーソルをわずかに動かしてOSに「操作中」と思わせる（最強の対策）
                # カーソルは非表示設定(BlankCursor)なので画面上は見えない
                current_pos = QCursor.pos()
                QCursor.setPos(current_pos.x() + 1, current_pos.y())
                QApplication.processEvents()
                QCursor.setPos(current_pos)

                # 2. 標準的なスクリーンセーバーリセットコマンド
                subprocess.run("xdg-screensaver reset", shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # 複数のxsetコマンドを1回のシェル実行にまとめることで、
                # プロセス生成コスト(fork/exec)を削減し、レスポンスを向上させる
                # && ではなく ; で繋ぐことで、一部が失敗しても他を実行させる
                cmd = "xset s off; xset s 0 0; xset -dpms; xset s noblank; xset dpms force on"
                # 失敗してもログに出さない（Trixie/Wayland環境などでxsetがない場合のため）
                subprocess.run(cmd, shell=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                # ラズパイのハードウェア側でディスプレイ出力を強制ONにする
                # VNCが真っ黒になる現象や、省電力機能による信号断を防ぐ
                subprocess.run(['vcgencmd', 'display_power', '1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # カーネルレベルのコンソールブランクを無効化（念のため）
                # 権限エラーで失敗してもアプリは止まらないようにtry内にある
                subprocess.run("setterm -blank 0 -powerdown 0 -powersave off > /dev/tty0", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logging.error(f"Failed to wake screen: {e}")

    # --- 点滅制御 ---
    def blink_tick(self):
        # カウンタ減算
        if self.blink_counters['calling'] > 0:
            self.blink_counters['calling'] -= 1
        if self.blink_counters['finished'] > 0:
            self.blink_counters['finished'] -= 1
            
        # self.update_display() # 重いので呼ばない
        self.update_blink_colors() # 軽量版を呼ぶ
        
        # 両方終わったらタイマー停止
        if self.blink_counters['calling'] <= 0 and self.blink_counters['finished'] <= 0:
            self.blink_timer.stop()
            self.update_display()

    def _get_blink_color(self, status):
        """現在の点滅状態に応じた色を返す"""
        color = "white"
        if status == 'calling':
            color = "white"
            counter = self.blink_counters['calling']
            if counter > 0:
                # 100ms 間隔で点滅
                if counter % 2 == 1:
                    color = "black"
                else:
                    color = "white"
        elif status == 'finished':
            color = "yellow"
            counter = self.blink_counters['finished']
            if counter > 0:
                if counter % 2 == 1:
                    color = "black"
                else:
                    color = "yellow"
        return color

    def update_blink_colors(self):
        """既存のリストアイテムの色だけを更新（軽量版）"""
        if self.is_idle_mode: return

        def update_list_widget(list_widget, status_type):
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                m_id = item.data(Qt.UserRole)
                if m_id:
                    current_status = self.active_calls.get(m_id)
                    if current_status == status_type:
                        color = self._get_blink_color(current_status)
                        widget = list_widget.itemWidget(item)
                        if widget:
                            labels = widget.findChildren(QLabel)
                            for lbl in labels:
                                font_size = lbl.property("original_font_size")
                                if font_size:
                                    new_style = f"font-size: {font_size}px; font-family: Arial, sans-serif; font-weight: bold; color: {color}; background-color: transparent;"
                                    lbl.setStyleSheet(new_style)

        update_list_widget(self.list_calling, 'calling')
        update_list_widget(self.list_finished, 'finished')

    # --- アイドルモード（スライドショー）関連 ---
    def start_idle_mode(self):
        """アイドルモードを開始"""
        self.is_idle_mode = True

        # 動画プロセスが残っていたらkill
        if self.video_process:
            try:
                self.video_process.kill()
            except:
                pass
            self.video_process = None
        
        # 再生リストの構築
        playlist = []
        
        # ローカルフォルダ内の全MP4を取得して再生リストにする
        if os.path.exists(LOCAL_VIDEO_DIR):
            files = glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.mp4"))
            files.extend(glob.glob(os.path.join(LOCAL_VIDEO_DIR, "*.MP4")))
            files.sort() # 名前順に再生
            playlist = files

        # 再生可能なファイルがあれば新規再生
        if playlist:
            try:
                # 動画画面へ切り替え
                self.stack_layout.setCurrentWidget(self.page_video)
                self.page_video.show()
                QApplication.processEvents()

                # 環境変数を準備（DISPLAY=:0 がないとGUIが表示されない場合がある）
                env = os.environ.copy()
                if 'DISPLAY' not in env:
                    env['DISPLAY'] = ':0'

                # vlcコマンドを使用
                # --fullscreen: 全画面, --loop: ループ, --no-osd: 文字表示なし
                # --no-keyboard-events --no-mouse-events: VLC側での入力を無効化
                # --file-caching=3000: 3秒分バッファしてSDカード遅延対策
                # --drop-late-frames --skip-frames: 遅れたら描画を飛ばして負荷を下げる
                # --disable-screensaver: VLC側でもスクリーンセーバー抑制
                cmd = ['vlc', '-I', 'dummy', '--fullscreen', '--loop', '--no-video-title-show', '--no-osd', '--quiet',
                       '--no-keyboard-events', '--no-mouse-events', '--file-caching=3000', '--drop-late-frames', '--skip-frames', '--disable-screensaver']
                cmd.extend(playlist)
                
                # VLCをQtのウィジェットに埋め込む
                if IS_RASPBERRY_PI:
                    cmd.append(f'--drawable-xid={int(self.page_video.winId())}')

                print(f"Playing videos: {playlist}", flush=True)
                self.video_process = subprocess.Popen(cmd, env=env)
                
                
                # 動画再生中はスライドショーのタイマーなどは動かさない
                self.blink_timer.stop()
                self.blink_counters = {'calling': 0, 'finished': 0}
                self.grabKeyboard()
                return
            except Exception as e:
                logging.error(f"Video playback failed: {e}")
                # 失敗時は通常のスライドショーへフォールバック

        self.stack_layout.setCurrentWidget(self.page_normal)
        self.list_finished.hide() # 右側を隠して左側を全画面化
        self.list_calling.clear()
        self.list_finished.clear()
        
        # 点滅リセット
        self.blink_timer.stop()
        self.blink_counters = {'calling': 0, 'finished': 0}

    def stop_idle_mode(self):
        """アイドルモードを終了して通常画面へ"""
        self.is_idle_mode = False
        
        # 【改善】レスポンス最優先：何よりも先に動画プロセスをkillして画面を切り替える
        if self.video_process:
            try:
                self.video_process.kill()
                self.video_process.wait() # ゾンビプロセス化を防ぐために終了を確認する（kill直後なので一瞬）
            except:
                pass
            self.video_process = None

        self.stack_layout.setCurrentWidget(self.page_normal)
        # その後で裏側の後始末を行う
        # キーボードキャプチャを解除
        self.releaseKeyboard()

        self.list_finished.show() # 右側を再表示
        self.idle_start_timer.stop()
        
        # アプリを最前面へ
        self.activateWindow()
        self.raise_()
        self.showFullScreen()
        
        # メモリ掃除（動画再生で使ったメモリを解放）
        gc.collect()
        QApplication.processEvents()

    def update_display(self):
        """画面リストを更新"""
        if self.is_idle_mode:
            return # アイドルモード中は update_idle_content で制御

        self.list_calling.clear()
        self.list_finished.clear()
        
        self.list_calling.setStyleSheet("background-color: black;")
        self.list_finished.setStyleSheet("background-color: black;")

        if not self.active_calls:
            # 呼び出しがない時は黒画面（10秒後にアイドルモードへ）
            return

        number_size = 200
        prefix_size = int(number_size / 4)
        
        for m_id, status in self.active_calls.items():
            if status == 'calling':
                color = self._get_blink_color('calling')
                self._add_list_item(self.list_calling, "呼出：", str(m_id), color, prefix_size, number_size, user_data=m_id)
            elif status == 'finished':
                color = self._get_blink_color('finished')
                self._add_list_item(self.list_finished, "終了：", str(m_id), color, prefix_size, number_size, user_data=m_id)

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
        """呼び出しリストの状態に応じてGPIOを制御"""
        if not IS_RASPBERRY_PI: return

        try:
            # "calling" 状態のものが1つでもあるか確認
            any_calling = any(s == 'calling' for s in self.active_calls.values())

            if any_calling:
                # 呼び出し中は常時ON
                if self.patlite and not self.patlite.is_lit:
                    self.patlite.on()
                if self.buzzer and not self.buzzer.is_lit:
                    self.buzzer.on()
            else:
                # 呼び出しがなく、パルス動作中でなければOFF
                if not self.is_pulsing:
                    if self.patlite: self.patlite.off()
                    if self.buzzer: self.buzzer.off()
        except Exception as e:
            logging.error(f"GPIO Error: {e}")

    def trigger_pulse(self):
        """リレーを1秒間だけONにする"""
        if not IS_RASPBERRY_PI: return
        
        # 既に呼び出し中(常時ON)ならパルス動作は不要
        if any(s == 'calling' for s in self.active_calls.values()):
            return

        self.is_pulsing = True
        if self.patlite: self.patlite.on()
        if self.buzzer: self.buzzer.on()
        
        # 1秒後にOFFにするタイマー
        QTimer.singleShot(1000, self.end_pulse)

    def end_pulse(self):
        self.is_pulsing = False
        self.update_gpio() # 状態を再評価してOFFにする

    def closeEvent(self, event):
        if self.patlite: self.patlite.close()
        if self.buzzer: self.buzzer.close()
        self.releaseKeyboard()
        if self.video_process:
            try:
                self.video_process.kill()
            except:
                pass
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    window = AndonWindow()
    window.show()
    sys.exit(app.exec())
