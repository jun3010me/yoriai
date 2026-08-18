#!/usr/bin/env python3
"""Yoriai: 同一ネットワーク上のローカルLLMエージェントを mDNS で自動発見し、
自己紹介カード(JSON)を交換する最小構成プロトタイプ。

対象OS: macOS (Apple Silicon) のみ動作確認。
"""

import argparse
import json
import logging
import platform
import re
import socket
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, IPVersion

SERVICE_TYPE = "_yoriai._tcp.local."
OLLAMA_BASE_URL = "http://localhost:11434"
CARD_REQUEST_TIMEOUT_SEC = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("yoriai")


# ---------------------------------------------------------------------------
# 自己紹介カードの生成
# ---------------------------------------------------------------------------

def get_short_hostname() -> str:
    name = socket.gethostname()
    for suffix in (".local.", ".local"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def get_chip_info() -> str:
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    # 仮の判断: sysctlが使えない環境(macOS以外)ではplatformの値で代用する
    return platform.processor() or platform.machine()


def _parse_vm_stat_free_bytes(vm_stat_output: str):
    lines = vm_stat_output.splitlines()
    if not lines:
        return None
    page_size = 4096
    match = re.search(r"page size of (\d+) bytes", lines[0])
    if match:
        page_size = int(match.group(1))

    stats = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            stats[key.strip()] = int(value)

    # 仮の判断: 「空きメモリ」は Pages free + Pages inactive
    # (すぐに再利用可能なページ) の合計とみなす。macOSのメモリ管理上、
    # 「本当の空き」だけを見ると実態より小さく出て誤解を招くため。
    free_pages = stats.get("Pages free", 0) + stats.get("Pages inactive", 0)
    return free_pages * page_size


def get_memory_info() -> dict:
    total_bytes = None
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            total_bytes = int(result.stdout.strip())
    except Exception:
        pass

    free_bytes = None
    try:
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            free_bytes = _parse_vm_stat_free_bytes(result.stdout)
    except Exception:
        pass

    return {
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "total_gb": round(total_bytes / 1e9, 1) if total_bytes else None,
        "free_gb": round(free_bytes / 1e9, 1) if free_bytes else None,
    }


def get_installed_models() -> list:
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        resp.raise_for_status()
        return [m.get("name") for m in resp.json().get("models", [])]
    except Exception as exc:
        logger.warning("Ollamaのインストール済みモデル一覧の取得に失敗しました: %s", exc)
        return []


def get_loaded_models() -> list:
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=3)
        resp.raise_for_status()
        return [m.get("name") for m in resp.json().get("models", [])]
    except Exception as exc:
        logger.warning("Ollamaのロード済みモデル一覧の取得に失敗しました: %s", exc)
        return []


def build_profile_card(agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "device_name": get_short_hostname(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "chip": get_chip_info(),
        },
        "memory": get_memory_info(),
        "models": {
            "installed": get_installed_models(),
            "loaded": get_loaded_models(),
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


# ---------------------------------------------------------------------------
# 自己紹介カードをHTTPで配信するサーバー
# ---------------------------------------------------------------------------

class CardRequestHandler(BaseHTTPRequestHandler):
    agent_id = None  # start_card_serverでサブクラス化して差し込む

    def do_GET(self):
        if self.path != "/card":
            self.send_response(404)
            self.end_headers()
            return
        card = build_profile_card(self.agent_id)
        body = json.dumps(card, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # 標準の毎リクエストアクセスログは出さず、アプリ側のログのみ表示する
        pass


def start_card_server(agent_id: str, port: int) -> ThreadingHTTPServer:
    handler_cls = type("BoundCardRequestHandler", (CardRequestHandler,), {"agent_id": agent_id})
    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def get_local_ip() -> str:
    # 仮の判断: 実際に通信は行わず、OSに経路選択させてローカルIPを取得する定番の方法
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# mDNSによる自動発見と自己紹介カードの交換
# ---------------------------------------------------------------------------

class YoriaiListener:
    def __init__(self, self_agent_id: str):
        self.self_agent_id = self_agent_id
        self.known_peers = {}

    def add_service(self, zc, service_type, name):
        self._handle_peer(zc, name)

    def update_service(self, zc, service_type, name):
        self._handle_peer(zc, name)

    def remove_service(self, zc, service_type, name):
        peer = self.known_peers.pop(name, None)
        if peer:
            logger.info("エージェントが見えなくなりました: %s", peer.get("device_name", name))

    def _handle_peer(self, zc, name):
        info = zc.get_service_info(SERVICE_TYPE, name)
        if info is None or not info.addresses:
            return

        properties = {
            key.decode(): value.decode()
            for key, value in info.properties.items()
            if value is not None
        }
        peer_agent_id = properties.get("agent_id")
        if peer_agent_id == self.self_agent_id:
            return  # 自分自身の広告は無視する

        address = socket.inet_ntoa(info.addresses[0])
        port = info.port
        self.known_peers[name] = {"agent_id": peer_agent_id, "device_name": properties.get("device_name", name)}

        threading.Thread(target=self._fetch_and_log_card, args=(name, address, port), daemon=True).start()

    def _fetch_and_log_card(self, name, address, port):
        try:
            resp = requests.get(f"http://{address}:{port}/card", timeout=CARD_REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            card = resp.json()
        except Exception as exc:
            logger.warning("%s からの自己紹介カード取得に失敗しました: %s", name, exc)
            return
        log_peer_card(card, address, port)


def log_peer_card(card: dict, address: str, port: int) -> None:
    device_name = card.get("device_name", "unknown")
    chip = card.get("os", {}).get("chip", "unknown")
    memory = card.get("memory", {})
    free_gb = memory.get("free_gb")
    total_gb = memory.get("total_gb")
    installed = card.get("models", {}).get("installed", [])
    loaded = card.get("models", {}).get("loaded", [])
    loaded_str = ", ".join(loaded) if loaded else "なし"

    logger.info(
        "\U0001f91d %s を発見しました (%s:%s)\n"
        "    チップ: %s\n"
        "    空きメモリ: %s / 総メモリ: %s\n"
        "    ロード済みモデル: %s\n"
        "    インストール済みモデル(%d件): %s",
        device_name, address, port,
        chip,
        f"{free_gb}GB" if free_gb is not None else "不明",
        f"{total_gb}GB" if total_gb is not None else "不明",
        loaded_str,
        len(installed), ", ".join(installed) if installed else "なし",
    )


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description="Yoriai: ローカルLLMエージェントの自動発見・自己紹介カード交換プロトタイプ")
    parser.add_argument("--port", type=int, default=0, help="自己紹介カードを配信するHTTPポート番号(0=自動選択、既定値)")
    args = parser.parse_args()

    if platform.system() != "Darwin":
        logger.warning("このプロトタイプはmacOS(Apple Silicon)を想定しています。他OSでは一部情報が取得できません。")

    agent_id = str(uuid.uuid4())
    short_hostname = get_short_hostname()
    local_ip = get_local_ip()
    port = args.port if args.port else pick_free_port()

    server = start_card_server(agent_id, port)
    logger.info("自己紹介カードサーバーを起動しました: http://%s:%s/card", local_ip, port)

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    # 仮の判断: サービスインスタンス名の衝突を避けるため agent_id の先頭8文字を付与する
    service_name = f"{short_hostname}-{agent_id[:8]}.{SERVICE_TYPE}"
    service_info = ServiceInfo(
        SERVICE_TYPE,
        service_name,
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        properties={"agent_id": agent_id, "device_name": short_hostname},
        server=f"{short_hostname}.local.",
    )

    logger.info("mDNSにサービスを登録します: %s", service_name)
    zeroconf.register_service(service_info)

    listener = YoriaiListener(agent_id)
    ServiceBrowser(zeroconf, SERVICE_TYPE, listener)

    logger.info("同じネットワーク上のYoriaiエージェントを探索しています... (Ctrl+Cで終了)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("終了します...")
    finally:
        zeroconf.unregister_service(service_info)
        zeroconf.close()
        server.shutdown()


if __name__ == "__main__":
    main()
