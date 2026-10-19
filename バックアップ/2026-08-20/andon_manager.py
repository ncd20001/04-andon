import socket
import json
import requests
import time
from PyQt5.QtCore import QObject, QThread, pyqtSignal, QTimer

# 設定定数
UDP_PORT = 50050        # 自動認識用ポート
HTTP_PORT = 5000        # 通信用ポート
DISCOVERY_MSG = b"DISCOVER_ANDON"
RESPONSE_MSG = "ANDON_HERE"

class NetworkWorker(QThread):
    """ネットワーク通信（探索・送信）を非同期で行うスレッド"""
    ip_found = pyqtSignal(str)
    discovery_failed = pyqtSignal()

    def __init__(self, task_type, data=None, target_ip=None):
        super().__init__()
        self.task_type = task_type # 'discover' or 'send'
        self.data = data
        self.target_ip = target_ip

    def run(self):
        if self.task_type == 'discover':
            self._discover()
        elif self.task_type == 'send':
            self._send()

    def _discover(self):
        """UDPブロードキャストでアンドンを探す"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5) # タイムアウトを短く設定
        
        # 全インターフェースで受信できるようにバインド（ポートはOS任せ）
        try:
            sock.bind(('0.0.0.0', 0))
        except Exception as e:
            print(f"Socket bind failed: {e}")

        # 送信先リストを作成（汎用ブロードキャスト + サブネットブロードキャスト）
        target_addrs = ['<broadcast>', '255.255.255.255']
        try:
            # PCのホスト名から、割り当てられている全てのIPアドレスを取得する
            hostname = socket.gethostname()
            local_ips = socket.gethostbyname_ex(hostname)[2]
            
            for ip in local_ips:
                # ループバック(自分自身)は除外
                if ip.startswith("127."):
                    continue
                
                parts = ip.split('.')
                
                # /24 (Class C相当) 例: 192.168.1.255
                bc_24 = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                if bc_24 not in target_addrs:
                    target_addrs.append(bc_24)

                # /16 (Class B相当) 例: 172.16.255.255
                bc_16 = f"{parts[0]}.{parts[1]}.255.255"
                if bc_16 not in target_addrs:
                    target_addrs.append(bc_16)
                
                # /8 (Class A相当) 例: 10.255.255.255 (今回のケースで重要)
                bc_8 = f"{parts[0]}.255.255.255"
                if bc_8 not in target_addrs:
                    target_addrs.append(bc_8)
            
            print(f"Discovery targets: {target_addrs}")
        except Exception as e:
            print(f"Error getting local IPs: {e}")

        found_any = False
        try:
            # 3回繰り返してブロードキャストを送る（取りこぼし防止）
            for i in range(3):
                for addr in target_addrs:
                    try:
                        sock.sendto(DISCOVERY_MSG, (addr, UDP_PORT))
                    except Exception:
                        pass
                
                # 1.5秒間、応答を待つ
                start_time = time.time()
                while time.time() - start_time < 1.5:
                    try:
                        data, addr = sock.recvfrom(1024)
                        msg = data.decode('utf-8')
                        print(f"Received packet from {addr[0]}: {msg}")
                        if msg.startswith(RESPONSE_MSG):
                            server_ip = addr[0]
                            print(f"Andon found at: {server_ip}")
                            self.ip_found.emit(server_ip)
                            found_any = True
                    except socket.timeout:
                        continue
                    except Exception:
                        pass
        except Exception as e:
            print(f"Discovery failed: {e}")
        finally:
            sock.close()

        if not found_any:
            self.discovery_failed.emit()

    def _send(self):
        """HTTP POSTでステータスを送信"""
        if not self.target_ip:
            return
        
        url = f"http://{self.target_ip}:{HTTP_PORT}/api/call"
        try:
            requests.post(url, json=self.data, timeout=2.0)
        except Exception as e:
            print(f"Failed to send status: {e}")

class AndonManager(QObject):
    """
    呼び出し機能（アンドン連携）を管理するクラス
    """
    call_state_changed = pyqtSignal(bool) # 状態変化をUIに通知
    connection_status_changed = pyqtSignal(bool) # 接続状態変化を通知

    def __init__(self, parent_window):
        super().__init__(parent_window)
        self.parent = parent_window
        self.is_calling = False
        self.andon_ips = set()
        self.is_connected = False
        self.worker = None
        self.send_workers = []
        
        # 起動時にアンドンを探す
        self.discover_andon()
        
        # 定期的に再探索を行う（5秒ごと）
        # これにより、後から起動した2台目や、最初に取りこぼした端末を自動で拾います
        self.discovery_timer = QTimer(self)
        self.discovery_timer.timeout.connect(self.discover_andon)
        self.discovery_timer.start(5000)

    def discover_andon(self):
        """アンドン探索を開始"""
        if self.worker and self.worker.isRunning():
            return

        self.worker = NetworkWorker('discover')
        self.worker.ip_found.connect(self._on_andon_found)
        self.worker.discovery_failed.connect(self._on_discovery_failed)
        self.worker.start()

    def _on_andon_found(self, ip):
        if ip not in self.andon_ips:
            print(f"New Andon registered: {ip}")
            self.andon_ips.add(ip)
            self.is_connected = True
            self.connection_status_changed.emit(True)
            # 探索完了後、もし呼び出し中なら状態を再送する（再接続時など）
            if self.is_calling:
                self._send_status_to_ip(ip)

    def _on_discovery_failed(self):
        if not self.andon_ips:
            self.is_connected = False
            self.connection_status_changed.emit(False)

    def toggle_call(self):
        """呼び出し状態をON/OFF切り替える"""
        self.is_calling = not self.is_calling
        self._send_status()
        self.call_state_changed.emit(self.is_calling)

    def set_call(self, state: bool):
        """呼び出し状態を明示的に設定する"""
        if self.is_calling != state:
            self.is_calling = state
            self._send_status()
            self.call_state_changed.emit(self.is_calling)

    def notify_finished(self):
        """作業終了を通知（呼び出し状態は解除し、finishedステータスを送る）"""
        if self.is_calling:
            self.is_calling = False
            self.call_state_changed.emit(False)
        
        self._send_status(status_override="finished")

    def _send_status(self, status_override=None):
        """現在の状態をアンドンへ送信"""
        # IPが未取得なら探索してから送信を試みる
        if not self.andon_ips:
            print("Andon IP not known, discovering...")
            self.discover_andon()
            return

        # 完了したワーカーを掃除
        self.send_workers = [w for w in self.send_workers if w.isRunning()]

        for ip in self.andon_ips:
            self._send_status_to_ip(ip, status_override)

    def _send_status_to_ip(self, ip, status_override=None):
        if status_override:
            status_str = status_override
        else:
            status_str = "calling" if self.is_calling else "normal"
        
        machine_num = self.parent.machine_number if self.parent.machine_number else "Unknown"
        
        payload = {
            "machine_id": machine_num,
            "status": status_str
        }
        
        # 送信ワーカーを起動
        worker = NetworkWorker('send', data=payload, target_ip=ip)
        self.send_workers.append(worker)
        worker.start()

        print(f"Sending to Andon({ip}): {status_str} (Machine: {machine_num})")