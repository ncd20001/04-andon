import sys
import socket
import json
import logging
import subprocess
import os
import requests
from flask import Flask, request, jsonify
from PyQt5.QtWidgets import QApplication, QMainWindow, QLabel, QVBoxLayout, QHBoxLayout, QWidget, QListWidget, QListWidgetItem
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QDateTime

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

# --- Weather Worker Thread ---
class WeatherWorker(QThread):
    weather_updated = pyqtSignal(str, str) # weather, temp

    def run(self):
        try:
            # 気象庁API (大阪府: 270000)
            url = "https://www.jma.go.jp/bosai/forecast/data/forecast/270000.json"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                data = response.json()
                # 今日の天気 (timeSeries[0] -> areas[0] -> weathers[0])
                weather = data[0]['timeSeries'][0]['areas'][0]['weathers'][0]
                weather = weather.replace('\u3000', ' ') # 全角スペースを半角に
                
                # 気温 (timeSeries[2] -> areas[0] -> temps)
                # ※データ構造は時間帯によって変わる可能性があるため簡易取得
                temp_str = "--"
                try:
                    # 多くの場合は timeSeries[2] が気温
                    temps = data[0]['timeSeries'][2]['areas'][0]['temps']
                    if temps:
                        if len(temps) >= 2:
                            # 最低/最高などが混在する場合
                            temp_str = f"{temps[0]}℃ / {temps[1]}℃"
                        else:
                            temp_str = f"{temps[0]}℃"
                except:
                    pass
                
                self.weather_updated.emit(weather, temp_str)
            else:
                self.weather_updated.emit("取得失敗", "--")
        except Exception as e:
            logging.error(f"Weather fetch error: {e}")
            self.weather_updated.emit("通信エラー", "--")

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
        self.idle_slide_index = 0
        self.weather_info = ("取得中...", "--")

        # UI構築
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QHBoxLayout(central_widget)

        # 左半分：呼び出し用
        self.list_calling = QListWidget()
        self.list_calling.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.list_calling)

        # 右半分：終了用
        self.list_finished = QListWidget()
        self.list_finished.setFocusPolicy(Qt.NoFocus)
        layout.addWidget(self.list_finished)

        # GPIO初期化
        self.patlite = None
        self.buzzer = None
        if IS_RASPBERRY_PI:
            try:
                self.patlite = LED(PATLITE_PIN)
                self.buzzer = LED(BUZZER_PIN) # ブザーもLEDクラスで制御（ON/OFFのみならこれで十分）
                print("GPIO Initialized successfully.", flush=True)
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

        # スクリーンセーバー対策：定期的に画面を点灯させる (1分ごと)
        self.keep_awake_timer = QTimer(self)
        self.keep_awake_timer.timeout.connect(self.wake_screen)
        self.keep_awake_timer.start(60000)
        
        # アイドル遷移用タイマー (10秒)
        self.idle_start_timer = QTimer(self)
        self.idle_start_timer.setSingleShot(True)
        self.idle_start_timer.timeout.connect(self.start_idle_mode)

        # スライドショー切り替えタイマー (10秒)
        self.slideshow_timer = QTimer(self)
        self.slideshow_timer.timeout.connect(self.next_slide)

        # 時計更新用タイマー (1秒)
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)

        self.weather_worker = WeatherWorker()
        self.weather_worker.weather_updated.connect(self.update_weather_data)

        # 点滅制御用
        self.blink_timer = QTimer(self)
        self.blink_timer.timeout.connect(self.blink_tick)
        self.blink_counters = {'calling': 0, 'finished': 0}
        # 虹色演出用カラー定義 (マリオの無敵スター風)
        self.rainbow_colors = ["#FF0000", "#FF7F00", "#FFFF00", "#00FF00", "#00FFFF", "#0000FF", "#8B00FF", "#FFFFFF"]
        self.rainbow_index = 0

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
            machine_id = data.get('machine_id', 'Unknown')
            status = data.get('status')

            if status == 'calling':
                self.active_calls[machine_id] = 'calling'
                self.wake_screen()
                # 虹色演出(1s=20tick) + 2回点滅(0.5s=10tick) = 30tick
                self.blink_counters['calling'] = 30
                if not self.blink_timer.isActive():
                    self.blink_timer.start(50) # 50ms間隔で高速切り替え
            elif status == 'finished':
                self.active_calls[machine_id] = 'finished'
                self.wake_screen()
                self.trigger_pulse() # 1秒だけリレーを動かす
                # 虹色演出 + 2回点滅
                self.blink_counters['finished'] = 30
                if not self.blink_timer.isActive():
                    self.blink_timer.start(50)
                # 10秒後に消去する
                QTimer.singleShot(30000, lambda: self.remove_finished_call(machine_id))
            elif status == 'normal':
                if machine_id in self.active_calls:
                    del self.active_calls[machine_id]

            # アイドルモード制御
            if self.active_calls:
                self.stop_idle_mode()
                self.update_display()
            else:
                # 呼び出しがない場合
                if not self.is_idle_mode and not self.idle_start_timer.isActive():
                    self.idle_start_timer.start(10000)
                    self.update_display() # 画面クリア
            
            self.update_gpio()
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
        if IS_RASPBERRY_PI:
            try:
                # 環境変数を準備（自動起動時などにDISPLAYがない場合への対策）
                env = os.environ.copy()
                if 'DISPLAY' not in env:
                    env['DISPLAY'] = ':0'

                # スクリーンセーバー無効化設定
                subprocess.run(['xset', 's', 'off'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['xset', 's', '0', '0'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['xset', '-dpms'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['xset', 's', 'noblank'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # xsetコマンドで画面を強制点灯
                subprocess.run(['xset', 'dpms', 'force', 'on'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['xset', 's', 'reset'], env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as e:
                logging.error(f"Failed to wake screen: {e}")

    # --- 点滅制御 ---
    def blink_tick(self):
        self.rainbow_index = (self.rainbow_index + 1) % len(self.rainbow_colors)
        
        # カウンタ減算
        if self.blink_counters['calling'] > 0:
            self.blink_counters['calling'] -= 1
        if self.blink_counters['finished'] > 0:
            self.blink_counters['finished'] -= 1
            
        self.update_display()
        
        # 両方終わったらタイマー停止
        if self.blink_counters['calling'] <= 0 and self.blink_counters['finished'] <= 0:
            self.blink_timer.stop()
            self.update_display()

    # --- アイドルモード（スライドショー）関連 ---
    def start_idle_mode(self):
        """アイドルモードを開始"""
        self.is_idle_mode = True
        self.idle_slide_index = 0
        self.list_finished.hide() # 右側を隠して左側を全画面化
        self.slideshow_timer.start(10000) # 10秒ごとに切り替え
        self.clock_timer.start(1000)      # 時計は1秒更新
        self.weather_worker.start()       # 天気取得開始
        
        # 点滅リセット
        self.blink_timer.stop()
        self.blink_counters = {'calling': 0, 'finished': 0}
        self.update_idle_content()

    def stop_idle_mode(self):
        """アイドルモードを終了して通常画面へ"""
        self.is_idle_mode = False
        self.list_finished.show() # 右側を再表示
        self.idle_start_timer.stop()
        self.slideshow_timer.stop()
        self.clock_timer.stop()
        # update_displayは呼び出し元で行う

    def next_slide(self):
        """次のスライドへ"""
        self.idle_slide_index = (self.idle_slide_index + 1) % 6
        self.update_idle_content()

    def update_clock(self):
        """時計表示の更新（スライドが時計の時のみ）"""
        if self.is_idle_mode and self.idle_slide_index == 0:
            self.update_idle_content()

    def update_weather_data(self, weather, temp):
        self.weather_info = (weather, temp)
        # 天気スライド表示中なら更新
        if self.is_idle_mode and self.idle_slide_index == 3:
            self.update_idle_content()

    def update_idle_content(self):
        """アイドル画面の描画"""
        self.list_calling.clear()
        self.list_finished.clear()
        self.list_calling.setStyleSheet("background-color: black;")

        idx = self.idle_slide_index
        color = "white" # フォントは太字の白に統一

        if idx == 0: # 時計
            now = QDateTime.currentDateTime()
            date_str = now.toString("yyyy/MM/dd")
            time_str = now.toString("HH:mm:ss")
            # 1つの枠(list_calling)に縦に並べる
            self._add_list_item(self.list_calling, "", date_str, color, 0, 150)
            self._add_list_item(self.list_calling, "", time_str, color, 0, 200)
        elif idx == 1: # 労い
            self._add_list_item(self.list_calling, "", "今日も１日", color, 0, 100)
            self._add_list_item(self.list_calling, "", "ご苦労様です", color, 0, 100)
            self._add_list_item(self.list_calling, "", "Cảm ơn bạn đã", color, 0, 100)
            self._add_list_item(self.list_calling, "", "làm việc vất vả", color, 0, 100)
        elif idx == 2: # 安全
            self._add_list_item(self.list_calling, "", "安全第一で", color, 0, 100)
            self._add_list_item(self.list_calling, "", "頑張りましょう", color, 0, 100)
            self._add_list_item(self.list_calling, "", "An toàn là trên hết", color, 0, 100)
        elif idx == 3: # 天気
            self._add_list_item(self.list_calling, "", "今日の天気", color, 0, 100)
            self._add_list_item(self.list_calling, "", "Thời tiết hôm nay tại", color, 0, 100)
            weather_text = self.weather_info[0]
            temp_text = self.weather_info[1]
            self._add_list_item(self.list_calling, "", weather_text, color, 0, 90)
            self._add_list_item(self.list_calling, "", temp_text, color, 0, 100)
        elif idx == 4: # 5S活動
            self._add_list_item(self.list_calling, "", "5S活動の推進！", color, 0, 100)
            self._add_list_item(self.list_calling, "", "Thúc đẩy 5S!", color, 0, 100)
            self._add_list_item(self.list_calling, "", "整理・整頓・清掃！", color, 0, 100)
            self._add_list_item(self.list_calling, "", "Sàng lọc - Sắp xếp - Sạch sẽ", color, 0, 70)
        elif idx == 5: # 季節のメッセージ
            month = QDateTime.currentDateTime().date().month()
            if 5 <= month <= 9: # 夏場 (5月-9月)
                self._add_list_item(self.list_calling, "", "熱中症に注意", color, 0, 100)
                self._add_list_item(self.list_calling, "", "Chú ý say nắng", color, 0, 100)
                self._add_list_item(self.list_calling, "", "水分補給を忘れずに", color, 0, 100)
                self._add_list_item(self.list_calling, "", "Đừng quên uống nước", color, 0, 80)
            else: # 冬場 (10月-4月)
                self._add_list_item(self.list_calling, "", "手洗い・うがい", color, 0, 100)
                self._add_list_item(self.list_calling, "", "Rửa tay và súc miệng", color, 0, 80)
                self._add_list_item(self.list_calling, "", "インフルエンザに注意", color, 0, 80)
                self._add_list_item(self.list_calling, "", "Chú ý bệnh cúm", color, 0, 100)

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
                color = "white"
                counter = self.blink_counters['calling']
                if counter > 0:
                    if counter > 10:
                        # 虹色演出フェーズ
                        color = self.rainbow_colors[self.rainbow_index]
                    else:
                        # 点滅フェーズ (10tick = 0.5s)
                        if ((counter - 1) // 2) % 2 == 1:
                            color = "black"
                        else:
                            color = "white"
                self._add_list_item(self.list_calling, "呼出：", str(m_id), color, prefix_size, number_size)
            elif status == 'finished':
                color = "yellow"
                counter = self.blink_counters['finished']
                if counter > 0:
                    if counter > 10:
                        # 虹色演出フェーズ
                        color = self.rainbow_colors[self.rainbow_index]
                    else:
                        # 点滅フェーズ
                        if ((counter - 1) // 2) % 2 == 1:
                            color = "black"
                        else:
                            color = "yellow"
                self._add_list_item(self.list_finished, "終了：", str(m_id), color, prefix_size, number_size)

    def _add_list_item(self, list_widget, prefix_text, number_text, color, p_size, n_size):
        base_style = f"font-family: Arial, sans-serif; font-weight: bold; color: {color}; background-color: transparent;"
        
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)

        lbl_prefix = QLabel(prefix_text)
        lbl_prefix.setStyleSheet(f"font-size: {p_size}px; {base_style}")
        
        lbl_num = QLabel(number_text)
        lbl_num.setStyleSheet(f"font-size: {n_size}px; {base_style}")

        layout.addWidget(lbl_prefix)
        layout.addWidget(lbl_num)

        item = QListWidgetItem()
        item.setSizeHint(widget.sizeHint())
        
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
        event.accept()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setOverrideCursor(Qt.BlankCursor)
    window = AndonWindow()
    window.show()
    sys.exit(app.exec())
