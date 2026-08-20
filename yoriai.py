#!/usr/bin/env python3
"""Yoriai: 同一ネットワーク上のローカルLLMエージェントを mDNS で自動発見し、
自己紹介カード(JSON)を交換する最小構成プロトタイプ。

対象OS: macOS (Apple Silicon) / Linux (Raspberry Pi 4を含むDebian系など)。
"""

import argparse
import json
import logging
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, IPVersion, InterfaceChoice

import config
import tailscale

SERVICE_TYPE = "_yoriai._tcp.local."
OLLAMA_BASE_URL = "http://localhost:11434"
LMSTUDIO_BASE_URL = "http://localhost:1234"
CARD_REQUEST_TIMEOUT_SEC = 5
ORG_FINGERPRINT_HEADER = "X-Yoriai-Org-Fingerprint"
HEARTBEAT_INTERVAL_SEC = 10

# 仮の判断: チャットの接続確立自体はカード取得と同程度の速さで判定してよいが、
# LLMの生成そのものは(モデルサイズや質問内容によっては)数十秒かかることが
# あるため、読み取りタイムアウトは長めに取る。
CHAT_CONNECT_TIMEOUT_SEC = 5
CHAT_READ_TIMEOUT_SEC = 120

# 仮の判断: 起動時1回だけのスキャンだと、たまたま相手のYoriaiがまだ起動しきっていない
# タイミングで実行してしまった場合に「Connection refused」で失敗し、その後相手が
# 起動してもずっと0件のまま固定されてしまう問題が実機検証で見つかった。
# そのため定期的に再スキャンするようにした。ハートビートと同じ10秒間隔にすると、
# ピア数が多いTailnetでは問い合わせのログが頻繁に出てノイズになりうるため、
# 少し長めの間隔にしている。
TAILSCALE_RESCAN_INTERVAL_SEC = 30

# 仮の判断: これまでは既定でOSにポートを自動選択させていたが(0=自動)、
# Tailscale経由の発見(mDNSが使えない相手に直接ポーリングする方式)では
# 事前に相手のポート番号を知る手段がないため、固定の既定ポートを設ける。
# 同一マシン上で複数エージェントをテストしたい場合は `--port 0` で
# 従来通りOS自動選択に戻せる。
DEFAULT_CARD_PORT = 47120

NO_TOKEN_GUIDANCE = (
    "トークンがありません。--init で新規作成するか、"
    "--join=<トークン> で既存の組織に参加してください"
)

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
    system = platform.system()
    if system == "Darwin":
        info = _get_chip_info_macos()
    elif system == "Linux":
        info = _get_chip_info_linux()
    else:
        info = None
    if info:
        return info
    # 仮の判断: OS別の取得方法が使えない環境ではplatformの値で代用する
    return platform.processor() or platform.machine()


def _get_chip_info_macos():
    try:
        result = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _get_chip_info_linux():
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return None

    # 仮の判断: Raspberry PiなどのARMボードでは/proc/cpuinfoの末尾に
    # ボード名を表す "Model" 行が付くことが多いため、まずそちらを優先する。
    # x86系のように"Model"行が無い場合は、コアごとに繰り返し出てくる
    # "model name" 行の先頭のものを使う。
    model_match = re.search(r"^Model\s*:\s*(.+)$", content, re.MULTILINE)
    if model_match:
        return model_match.group(1).strip()

    name_match = re.search(r"^model name\s*:\s*(.+)$", content, re.MULTILINE)
    if name_match:
        return name_match.group(1).strip()

    return None


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


def _empty_memory_info() -> dict:
    return {"total_bytes": None, "free_bytes": None, "total_gb": None, "free_gb": None}


def _memory_info_from_bytes(total_bytes, free_bytes) -> dict:
    return {
        "total_bytes": total_bytes,
        "free_bytes": free_bytes,
        "total_gb": round(total_bytes / 1e9, 1) if total_bytes else None,
        "free_gb": round(free_bytes / 1e9, 1) if free_bytes else None,
    }


def get_memory_info() -> dict:
    system = platform.system()
    if system == "Darwin":
        return _get_memory_info_macos()
    elif system == "Linux":
        return _get_memory_info_linux()
    return _empty_memory_info()


def _get_memory_info_macos() -> dict:
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

    return _memory_info_from_bytes(total_bytes, free_bytes)


def _get_memory_info_linux() -> dict:
    try:
        with open("/proc/meminfo", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        logger.warning("/proc/meminfoの読み込みに失敗しました: %s", exc)
        return _empty_memory_info()

    values = {}
    for line in content.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)\s*kB", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024  # kB -> bytes

    total_bytes = values.get("MemTotal")
    # 仮の判断: 「空きメモリ」はMemFreeではなくMemAvailable(すぐに確保可能な
    # メモリの推定値)を使う。macOS版のPages free+inactiveと同様、キャッシュ分を
    # 含めないと実態よりかなり少なく見えてしまうため。
    free_bytes = values.get("MemAvailable")

    return _memory_info_from_bytes(total_bytes, free_bytes)


def get_ollama_installed_models() -> list:
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        resp.raise_for_status()
        return [m.get("name") for m in resp.json().get("models", [])]
    except Exception as exc:
        logger.warning("Ollamaのインストール済みモデル一覧の取得に失敗しました: %s", exc)
        return []


def get_ollama_loaded_models() -> list:
    try:
        resp = requests.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=3)
        resp.raise_for_status()
        return [m.get("name") for m in resp.json().get("models", [])]
    except Exception as exc:
        logger.warning("Ollamaのロード済みモデル一覧の取得に失敗しました: %s", exc)
        return []


def get_lmstudio_models() -> list:
    """LM Studioのローカルサーバー(OpenAI互換API)からモデル一覧を取得する。

    仮の判断: `/v1/models` はロード状態(インストール済みかロード済みか)を
    区別して返してくれないため、ここで取得できたモデルは「インストール済み」
    「ロード済み」の両方として扱う。LM Studio側で「Local Server」を
    起動していない場合は空リストを返す(エラーにはしない)。
    """
    try:
        resp = requests.get(f"{LMSTUDIO_BASE_URL}/v1/models", timeout=3)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]
    except Exception as exc:
        logger.warning("LM Studioのモデル一覧の取得に失敗しました: %s", exc)
        return []


def _merge_model_lists(*model_lists: list) -> list:
    merged = []
    for models in model_lists:
        for name in models:
            if name not in merged:
                merged.append(name)
    return merged


def get_installed_models() -> list:
    # 仮の判断: OllamaとLM Studioのどちらが動いていても自己紹介カードに反映されるよう、
    # 両方に問い合わせて結果を合算する(両方動いている場合は両方のモデルが載る)。
    return _merge_model_lists(get_ollama_installed_models(), get_lmstudio_models())


def get_loaded_models() -> list:
    return _merge_model_lists(get_ollama_loaded_models(), get_lmstudio_models())


# ---------------------------------------------------------------------------
# チャットのプロキシ(Ollama/LM Studioへのストリーミング問い合わせ)
# ---------------------------------------------------------------------------

def _stream_ollama_chat(model: str, messages: list):
    """OllamaのネイティブAPI(/api/chat、NDJSONストリーミング)に問い合わせ、
    正規化したイベント({"content": ...} / {"done": True} / {"error": ...})を
    順にyieldする。
    """
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={"model": model, "messages": messages, "stream": True},
            stream=True,
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, CHAT_READ_TIMEOUT_SEC),
        )
        resp.raise_for_status()
    except Exception as exc:
        yield {"error": str(exc)}
        return

    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = obj.get("message", {}).get("content")
            if content:
                yield {"content": content}
            if obj.get("done"):
                break
    except Exception as exc:
        yield {"error": str(exc)}
        return
    yield {"done": True}


def _stream_lmstudio_chat(model: str, messages: list):
    """LM StudioのOpenAI互換API(/v1/chat/completions、SSEストリーミング)に
    問い合わせ、Ollama版と同じ正規化イベントを順にyieldする。
    """
    try:
        resp = requests.post(
            f"{LMSTUDIO_BASE_URL}/v1/chat/completions",
            json={"model": model, "messages": messages, "stream": True},
            stream=True,
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, CHAT_READ_TIMEOUT_SEC),
        )
        resp.raise_for_status()
    except Exception as exc:
        yield {"error": str(exc)}
        return

    try:
        for raw_line in resp.iter_lines():
            if not raw_line:
                continue
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices", [])
            if choices:
                content = choices[0].get("delta", {}).get("content")
                if content:
                    yield {"content": content}
    except Exception as exc:
        yield {"error": str(exc)}
        return
    yield {"done": True}


def stream_chat_completion(model: str, messages: list):
    """モデル名からOllama/LM Studioどちらにチャットを振るかを決め、
    正規化されたストリーミングイベントを順にyieldする。

    仮の判断: フェーズ5では「タスクの難易度に応じた賢い振り分け」はスコープ外の
    ため、モデルの選定自体は呼び出し元(候補選定ロジック)に任せ、ここでは
    単純に「Ollamaのロード済みモデル一覧に名前があればOllama、なければ
    LM Studio」というバックエンドの振り分けだけを行う。
    """
    if model in get_ollama_loaded_models():
        yield from _stream_ollama_chat(model, messages)
    else:
        yield from _stream_lmstudio_chat(model, messages)


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
# 発見済みピアの共有レジストリ(--status コマンドの情報源)
# ---------------------------------------------------------------------------

class PeerRegistry:
    """mDNS/Tailscaleで発見したピアの最新カードを保持する、複数スレッドから
    アクセスされる共有ストア。--status コマンドが問い合わせる `/status`
    エンドポイントの情報源になる。

    仮の判断: mDNSは「見えなくなった」イベント(remove_service)があるので
    即座に取り除けるが、Tailscale経由はそのようなイベントが無く定期スキャンの
    結果でしか判断できない。そのため、Tailscale経由で見つけたピアは
    「直近のスキャンで見つからなかったら消す」という形で間接的に古い情報を
    掃除している(sync_tailscale)。同じピアがmDNSでも見えている場合は
    Tailscale側のスキャン結果に関わらず残す。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._peers = {}  # agent_id -> {"card", "via", "address", "port", "last_seen"}

    def upsert(self, agent_id: str, card: dict, via: str, address: str, port: int) -> None:
        with self._lock:
            self._peers[agent_id] = {
                "card": card,
                "via": via,
                "address": address,
                "port": port,
                "last_seen": time.time(),
            }

    def remove(self, agent_id: str) -> None:
        with self._lock:
            self._peers.pop(agent_id, None)

    def sync_tailscale(self, found_agent_ids) -> None:
        found_agent_ids = set(found_agent_ids)
        with self._lock:
            stale = [
                agent_id for agent_id, info in self._peers.items()
                if info["via"] == "Tailscale" and agent_id not in found_agent_ids
            ]
            for agent_id in stale:
                del self._peers[agent_id]

    def snapshot(self) -> list:
        with self._lock:
            return list(self._peers.values())


# ---------------------------------------------------------------------------
# 自己紹介カードをHTTPで配信するサーバー
# ---------------------------------------------------------------------------

class CardRequestHandler(BaseHTTPRequestHandler):
    agent_id = None  # start_card_serverでサブクラス化して差し込む
    org_fingerprint = None  # 同上
    registry = None  # 同上(PeerRegistry、/status で使う)

    def do_GET(self):
        if self.path == "/card":
            self._handle_card()
        elif self.path == "/status":
            self._handle_status()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/chat":
            self._handle_chat()
        else:
            self.send_response(404)
            self.end_headers()

    def _check_org_fingerprint(self) -> bool:
        # 仮の判断: mDNS側でトークン不一致の相手はそもそも問い合わせに来ない想定だが、
        # エンドポイントに直接アクセスされた場合の備えとして、サーバー側でも
        # 組織フィンガープリント(トークンのSHA-256)の一致をここで再検証する。
        requester_fingerprint = self.headers.get(ORG_FINGERPRINT_HEADER)
        if requester_fingerprint != self.org_fingerprint:
            self.send_response(403)
            self.end_headers()
            return False
        return True

    def _handle_card(self):
        if not self._check_org_fingerprint():
            return
        self._send_json(build_profile_card(self.agent_id))

    def _handle_status(self):
        # 仮の判断: --status はこのエンドポイントに問い合わせるだけの軽量な
        # コマンドにしたいので、自分自身のカードと、mDNS/Tailscaleでこれまでに
        # 発見済みのピア一覧(PeerRegistryのスナップショット)をまとめて返す。
        # 新たにネットワークを再スキャンしたりはしない。
        if not self._check_org_fingerprint():
            return
        peers = self.registry.snapshot() if self.registry else []
        self._send_json({
            "self": build_profile_card(self.agent_id),
            "peers": peers,
        })

    def _handle_chat(self):
        # 仮の判断: /chat も /card と同じくトークンのフィンガープリントで認証する。
        if not self._check_org_fingerprint():
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        model = body.get("model")
        messages = body.get("messages", [])

        # 仮の判断: 応答は事前にサイズが分からないストリーミングなので
        # Content-Lengthは付けず、NDJSON(1行1イベントのJSON)として都度書き出す。
        # クライアント側が対応できない場合でも、単純に行ごとに読めば動く形式にした。
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            for event in stream_chat_completion(model, messages):
                self.wfile.write(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # 仮の判断: 相手が受信を打ち切った場合、ここでは何もせず単に配信を止める

    def _send_json(self, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # 標準の毎リクエストアクセスログは出さず、アプリ側のログのみ表示する
        pass


def start_card_server(agent_id: str, org_fingerprint: str, port: int, registry: "PeerRegistry") -> ThreadingHTTPServer:
    handler_cls = type(
        "BoundCardRequestHandler",
        (CardRequestHandler,),
        {"agent_id": agent_id, "org_fingerprint": org_fingerprint, "registry": registry},
    )
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


def get_physical_lan_ips() -> list:
    """物理LANインターフェースのIPv4アドレス一覧を返す(Tailscale等の仮想
    インターフェースをmDNSのバインド対象から除外し、"自宅LAN内向け"の発見に
    限定するため)。
    """
    system = platform.system()
    if system == "Darwin":
        return _get_physical_lan_ips_macos()
    elif system == "Linux":
        return _get_physical_lan_ips_linux()
    return []


def _get_physical_lan_ips_macos() -> list:
    """macOSで物理イーサネット/Wi-Fiに使われる `en<数字>` という命名規則の
    インターフェースだけを対象にする。`ifconfig`の出力を素朴にパースしている
    だけなので、環境によっては拾えない/拾いすぎる可能性がある(仮の判断)。
    """
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
    except Exception as exc:
        logger.warning("ifconfigの実行に失敗しました: %s", exc)
        return []
    if result.returncode != 0:
        return []

    ips = []
    current_iface = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace():
            current_iface = line.split(":", 1)[0]
            continue
        if current_iface and re.match(r"^en\d+$", current_iface):
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
            if match:
                ips.append(match.group(1))
    return ips


# 仮の判断: Linuxはインターフェースの命名規則がディストリビューション/機種に
# よってまちまち(eth0, wlan0, enp3s0, wlp2s0 など)で、macOSの `en<数字>` の
# ようなシンプルな許可リストでは網羅できない。そのため逆に、既知の仮想
# インターフェースの名前だけを除外するブロックリスト方式にしている。
LINUX_VIRTUAL_IFACE_PREFIXES = ("lo", "tailscale", "docker", "veth", "br-", "virbr", "tun", "tap", "wg")

_SIOCGIFADDR = 0x8915  # Linux固有のioctlリクエスト番号(インターフェースのIPv4アドレス取得用)


def _get_physical_lan_ips_linux() -> list:
    """`ip`/`ifconfig`コマンドに依存せず、標準ライブラリの`socket`/`fcntl`だけで
    インターフェース一覧とIPv4アドレスを取得する(最小構成のRaspberry Pi OSなど、
    iproute2/net-toolsが入っていない環境でも動かすための仮の判断)。
    """
    import fcntl
    import struct

    try:
        iface_names = [name for _, name in socket.if_nameindex()]
    except OSError as exc:
        logger.warning("ネットワークインターフェース一覧の取得に失敗しました: %s", exc)
        return []

    ips = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for iface in iface_names:
            if iface.startswith(LINUX_VIRTUAL_IFACE_PREFIXES):
                continue
            try:
                result = fcntl.ioctl(
                    sock.fileno(),
                    _SIOCGIFADDR,
                    struct.pack("256s", iface[:15].encode("utf-8")),
                )
            except OSError:
                continue  # IPv4アドレスが割り当てられていないインターフェース
            ips.append(socket.inet_ntoa(result[20:24]))
    return ips


# ---------------------------------------------------------------------------
# mDNSによる自動発見と自己紹介カードの交換
# ---------------------------------------------------------------------------

class YoriaiListener:
    def __init__(self, self_agent_id: str, self_org_fingerprint: str, registry: "PeerRegistry" = None):
        self.self_agent_id = self_agent_id
        self.self_org_fingerprint = self_org_fingerprint
        self.registry = registry
        self.known_peers = {}

    def add_service(self, zc, service_type, name):
        self._handle_peer(zc, name)

    def update_service(self, zc, service_type, name):
        self._handle_peer(zc, name)

    def remove_service(self, zc, service_type, name):
        peer = self.known_peers.pop(name, None)
        if peer:
            logger.info("エージェントが見えなくなりました: %s", peer.get("device_name", name))
            if self.registry:
                self.registry.remove(peer.get("agent_id"))

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

        device_name = properties.get("device_name", name)
        peer_org_fingerprint = properties.get("org_fingerprint")
        if peer_org_fingerprint != self.self_org_fingerprint:
            # トークンが異なる相手は「別の組織のエージェント」としてログにだけ残し、
            # カードの取得は行わない(エラーにはしない)。
            logger.info("\U0001f512 %s は別の組織のエージェントのようです(トークン不一致のため無視します)", device_name)
            return

        address = socket.inet_ntoa(info.addresses[0])
        port = info.port
        self.known_peers[name] = {"agent_id": peer_agent_id, "device_name": device_name}

        threading.Thread(
            target=self._fetch_and_log_card, args=(name, peer_agent_id, address, port), daemon=True,
        ).start()

    def _fetch_and_log_card(self, name, peer_agent_id, address, port):
        try:
            resp = requests.get(
                f"http://{address}:{port}/card",
                headers={ORG_FINGERPRINT_HEADER: self.self_org_fingerprint},
                timeout=CARD_REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            card = resp.json()
        except Exception as exc:
            logger.warning("%s からの自己紹介カード取得に失敗しました: %s", name, exc)
            return
        if self.registry:
            self.registry.upsert(peer_agent_id, card, "mDNS", address, port)
        log_peer_card(card, address, port)


def log_peer_card(card: dict, address: str, port: int, via: str = "mDNS") -> None:
    device_name = card.get("device_name", "unknown")
    chip = card.get("os", {}).get("chip", "unknown")
    memory = card.get("memory", {})
    free_gb = memory.get("free_gb")
    total_gb = memory.get("total_gb")
    installed = card.get("models", {}).get("installed", [])
    loaded = card.get("models", {}).get("loaded", [])
    loaded_str = ", ".join(loaded) if loaded else "なし"

    # 仮の判断: mDNSでの発見(既存の表示)とTailscale経由の発見を、
    # 絵文字とラベルで見分けやすくしている。
    emoji = "\U0001f91d" if via == "mDNS" else "\U0001f310"
    label = "" if via == "mDNS" else f"[{via}] "

    logger.info(
        "%s %s%s を発見しました (%s:%s)\n"
        "    チップ: %s\n"
        "    空きメモリ: %s / 総メモリ: %s\n"
        "    ロード済みモデル: %s\n"
        "    インストール済みモデル(%d件): %s",
        emoji, label, device_name, address, port,
        chip,
        f"{free_gb}GB" if free_gb is not None else "不明",
        f"{total_gb}GB" if total_gb is not None else "不明",
        loaded_str,
        len(installed), ", ".join(installed) if installed else "なし",
    )


def discover_via_tailscale(agent_id: str, org_fingerprint: str, port: int, registry: "PeerRegistry" = None) -> int:
    """Tailscale経由でエージェント候補をポーリングし、見つかった台数を返す。

    呼び出し元(run_agent)が起動時とTAILSCALE_RESCAN_INTERVAL_SEC間隔で
    繰り返し呼び出す想定。1回の呼び出しは毎回この関数の中で完結するスキャンで、
    前回までの結果は保持しない(呼び出しごとに毎回ゼロから数え直す)。
    """
    cli_path = tailscale.find_cli()
    if not cli_path:
        logger.info("tailscaleコマンドが見つからないため、Tailscale経由の発見はスキップします。")
        if registry:
            registry.sync_tailscale([])  # tailscaleが使えなくなった場合、以前の発見結果を掃除する
        return 0
    logger.info("Tailscale CLIを検出しました: %s", cli_path)

    peers = tailscale.get_peers(cli_path)
    if not peers:
        logger.info("Tailscale経由で0台のエージェント候補を確認しました(Tailscaleのピアが見つかりませんでした)")
        if registry:
            registry.sync_tailscale([])
        return 0

    logger.info(
        "Tailscaleのピアを%d件確認しました。自己紹介カードへの問い合わせを試みます: %s",
        len(peers), ", ".join(f"{hostname}({ip})" for hostname, ip in peers),
    )

    def _probe(peer):
        hostname, ip = peer
        try:
            resp = requests.get(
                f"http://{ip}:{port}/card",
                headers={ORG_FINGERPRINT_HEADER: org_fingerprint},
                timeout=CARD_REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            card = resp.json()
        except Exception as exc:
            # 仮の判断: 当初は1台ごとの失敗を無言でスキップしていたが、「トークン不一致で403」
            # と「そもそも繋がらない(タイムアウト/接続拒否)」の区別がつかず実機での原因切り分けが
            # 困難だったため、原因が分かるようINFOログに残すようにした。
            logger.info("Tailscale経由の問い合わせに失敗しました: %s (%s) - %s", hostname, ip, exc)
            return None
        if card.get("agent_id") == agent_id:
            return None  # 自分自身
        return ip, card

    found_count = 0
    found_agent_ids = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for result in executor.map(_probe, peers):
            if result is None:
                continue
            ip, card = result
            found_count += 1
            found_agent_ids.append(card.get("agent_id"))
            if registry:
                registry.upsert(card.get("agent_id"), card, "Tailscale", ip, port)
            log_peer_card(card, ip, port, via="Tailscale")

    if registry:
        registry.sync_tailscale(found_agent_ids)

    logger.info("Tailscale経由で%d台のエージェント候補を確認しました", found_count)
    return found_count


# ---------------------------------------------------------------------------
# 対話モード(REPL): 組織内メンバーへの問い合わせ
# ---------------------------------------------------------------------------

def _build_chat_candidate(card: dict, is_self: bool, address: str = None, port: int = None):
    """自己紹介カードから、チャットの問い合わせ先候補(ロード済みモデルを
    持つメンバー)を作る。ロード済みモデルが無いメンバーはNoneを返す。
    """
    loaded = card.get("models", {}).get("loaded", [])
    if not loaded:
        return None
    label = card.get("device_name", "unknown")
    if is_self:
        label += "(自分)"
    return {
        "label": label,
        # 仮の判断: ロード済みモデルが複数ある場合はどれを使うべきかの判断基準が
        # まだ無いため、単純に先頭のものを使う。
        "model": loaded[0],
        "free_gb": card.get("memory", {}).get("free_gb"),
        "is_self": is_self,
        "address": address,
        "port": port,
    }


def _select_chat_candidates(self_card: dict, peers: list) -> list:
    """組織内から問い合わせ候補を集め、空きメモリの多い順に並べて返す。

    仮の判断: フェーズ5では「タスクの難易度に応じた賢い振り分け」はスコープ外
    のため、単純に「ロード済みモデルがあり、空きメモリが最も多いメンバー」を
    選ぶだけにする。空きメモリが不明なメンバーは最下位として扱う。
    """
    candidates = []
    self_candidate = _build_chat_candidate(self_card, is_self=True)
    if self_candidate:
        candidates.append(self_candidate)
    for peer in peers:
        candidate = _build_chat_candidate(
            peer.get("card", {}), is_self=False, address=peer.get("address"), port=peer.get("port"),
        )
        if candidate:
            candidates.append(candidate)

    candidates.sort(key=lambda c: c["free_gb"] if c["free_gb"] is not None else -1, reverse=True)
    return candidates


def _stream_chat_from_candidate(candidate: dict, org_fingerprint: str, messages: list):
    """候補が自分自身ならプロセス内で直接、他のメンバーならHTTP経由(/chat)で
    問い合わせ、正規化されたストリーミングイベントを順にyieldする。
    """
    if candidate["is_self"]:
        yield from stream_chat_completion(candidate["model"], messages)
        return

    try:
        resp = requests.post(
            f"http://{candidate['address']}:{candidate['port']}/chat",
            json={"model": candidate["model"], "messages": messages},
            headers={ORG_FINGERPRINT_HEADER: org_fingerprint},
            stream=True,
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, CHAT_READ_TIMEOUT_SEC),
        )
        resp.raise_for_status()
    except Exception as exc:
        yield {"error": str(exc)}
        return

    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
    except Exception as exc:
        yield {"error": str(exc)}


def _ask_organization(ctx: dict, messages: list) -> None:
    """組織内のメンバーに順番に問い合わせ、失敗したら次に空きメモリが多い
    候補へ自動でフォールバックしながら回答をストリーミング表示する。

    成功した場合はassistantの回答を`messages`に追記する(会話履歴の継続)。
    """
    self_card = build_profile_card(ctx["agent_id"])
    peers = ctx["registry"].snapshot()
    candidates = _select_chat_candidates(self_card, peers)

    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    for i, candidate in enumerate(candidates):
        if i == 0:
            print(f"[{candidate['label']} に問い合わせています... (モデル: {candidate['model']})]")
        else:
            print(f"[フォールバック: {candidate['label']} に問い合わせています... (モデル: {candidate['model']})]")

        answer_parts = []
        failed = False
        for event in _stream_chat_from_candidate(candidate, ctx["org_fingerprint"], messages):
            if "error" in event:
                logger.warning("%s への問い合わせに失敗しました: %s", candidate["label"], event["error"])
                failed = True
                break
            content = event.get("content")
            if content:
                print(content, end="", flush=True)
                answer_parts.append(content)
            if event.get("done"):
                break

        if answer_parts:
            print()
            messages.append({"role": "assistant", "content": "".join(answer_parts)})
            return
        if not failed:
            logger.warning("%s から有効な応答が得られませんでした。", candidate["label"])

    print("すべての候補への問い合わせに失敗しました。しばらくしてから再度お試しください。")


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def _setup_agent(token: str, port: int) -> dict:
    """エージェントの起動処理(自己紹介カードサーバー、mDNS登録、Tailscaleの
    初回発見)をまとめて行い、以降の探索ループ(_discovery_loop)や対話モード
    (REPL)が使う情報をひとつの辞書(ctx)にまとめて返す。

    仮の判断: 従来1つの関数(run_agent)にまとまっていたセットアップ処理と
    「探索し続けるループ」を分離した。対話モード(REPL)では、探索ループを
    バックグラウンドスレッドで動かしつつメインスレッドで入力を受け付ける
    必要があり、常駐(headless)実行ではこれまで通りメインスレッドで
    ループさせたいため、両者で共有できる形にしている。
    """
    if platform.system() not in ("Darwin", "Linux"):
        logger.warning("このプロトタイプはmacOS/Linuxを想定しています。他OSでは一部情報が取得できません。")

    agent_id = str(uuid.uuid4())
    org_fingerprint = config.token_fingerprint(token)
    short_hostname = get_short_hostname()

    physical_ips = get_physical_lan_ips()
    if physical_ips:
        # 仮の判断: 物理LANインターフェースが複数見つかった場合は先頭(通常はen0=Wi-Fi)を
        # 自己紹介カードの広告先IPとして使う。
        local_ip = physical_ips[0]
        zc_interfaces = physical_ips
        logger.info("mDNSは物理LANインターフェースのみを使用します: %s", ", ".join(physical_ips))
    else:
        local_ip = get_local_ip()
        zc_interfaces = InterfaceChoice.All
        logger.warning(
            "物理LANインターフェース(en*)を特定できなかったため、mDNSは全インターフェースを使用します。"
            "Tailscale等の仮想インターフェースがある環境では発見に失敗することがあります。"
        )

    port = port if port else pick_free_port()

    registry = PeerRegistry()
    server = start_card_server(agent_id, org_fingerprint, port, registry)
    logger.info("自己紹介カードサーバーを起動しました: http://%s:%s/card", local_ip, port)

    zeroconf = Zeroconf(interfaces=zc_interfaces, ip_version=IPVersion.V4Only)
    # 仮の判断: サービスインスタンス名の衝突を避けるため agent_id の先頭8文字を付与する
    service_name = f"{short_hostname}-{agent_id[:8]}.{SERVICE_TYPE}"
    service_info = ServiceInfo(
        SERVICE_TYPE,
        service_name,
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        # 仮の判断: mDNSのTXTレコードには生トークンではなくSHA-256のフィンガープリントだけを載せる。
        # TXTレコードは同一ネットワーク上の誰からでも読めるため、生トークンを流すと
        # 「トークンによる参加制限」の意味が薄れてしまうため。
        properties={"agent_id": agent_id, "device_name": short_hostname, "org_fingerprint": org_fingerprint},
        server=f"{short_hostname}.local.",
    )

    logger.info("mDNSにサービスを登録します: %s", service_name)
    zeroconf.register_service(service_info)

    listener = YoriaiListener(agent_id, org_fingerprint, registry)
    ServiceBrowser(zeroconf, SERVICE_TYPE, listener)

    # mDNSはLANローカルのマルチキャストが前提で、Tailscale越しのリモートデバイスには
    # 原理的に届かない。そのため、Tailscaleのピア一覧に対して自己紹介カードの
    # エンドポイントを直接ポーリングする(mDNSとは別枠の仕組み)。起動直後にまず
    # 1回実行し、以降はTAILSCALE_RESCAN_INTERVAL_SEC間隔で再スキャンする
    # (相手がまだ起動しきっていないタイミングで一度失敗しても、後で拾えるようにするため)。
    tailscale_found_count = discover_via_tailscale(agent_id, org_fingerprint, port, registry)

    return {
        "agent_id": agent_id,
        "org_fingerprint": org_fingerprint,
        "port": port,
        "registry": registry,
        "server": server,
        "zeroconf": zeroconf,
        "service_info": service_info,
        "listener": listener,
        "tailscale_found_count": tailscale_found_count,
    }


def _discovery_loop(ctx: dict, stop_event: "threading.Event" = None) -> None:
    """探索ループ本体(ハートビートログ・Tailscaleの定期再スキャン)。

    常駐(headless)実行時はメインスレッドでKeyboardInterruptが起きるまで、
    対話モード(REPL)実行時はバックグラウンドスレッドでstop_eventが
    セットされるまで回り続ける。
    """
    logger.info("同じネットワーク上のYoriaiエージェントを探索しています... (Ctrl+Cで終了)")
    last_heartbeat = time.monotonic()
    last_tailscale_scan = time.monotonic()
    while stop_event is None or not stop_event.is_set():
        time.sleep(1)
        # 仮の判断: 発見がない間も動作中であることが外から分かるよう、
        # 一定間隔でハートビートログを出す。mDNSが機能しないネットワーク
        # (例: クライアント間マルチキャストが遮断された環境)でも、
        # Tailscale経由の発見数を合わせて見せることで「本当に0台なのか」を
        # 判断しやすくする。
        if time.monotonic() - last_tailscale_scan >= TAILSCALE_RESCAN_INTERVAL_SEC:
            ctx["tailscale_found_count"] = discover_via_tailscale(
                ctx["agent_id"], ctx["org_fingerprint"], ctx["port"], ctx["registry"],
            )
            last_tailscale_scan = time.monotonic()
        if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
            logger.info(
                "探索中... (現在発見数: mDNS %d件 / Tailscale %d件)",
                len(ctx["listener"].known_peers), ctx["tailscale_found_count"],
            )
            last_heartbeat = time.monotonic()


def _shutdown_agent(ctx: dict) -> None:
    ctx["zeroconf"].unregister_service(ctx["service_info"])
    ctx["zeroconf"].close()
    ctx["server"].shutdown()


def run_agent(token: str, port: int) -> None:
    """常駐(headless)実行のエントリーポイント。systemd/launchdなど、対話的な
    入出力ができない環境から起動される想定で、フェーズ5導入前と挙動を変えない。
    """
    ctx = _setup_agent(token, port)
    try:
        _discovery_loop(ctx)
    except KeyboardInterrupt:
        logger.info("終了します...")
    finally:
        _shutdown_agent(ctx)


def _run_repl(ctx: dict) -> None:
    print()
    print("=== Yoriai 対話モード ===")
    print("組織のメンバーに質問できます。終了するには exit または quit と入力するか、Ctrl+Cを押してください。")
    print()

    # 仮の判断: 会話履歴(messages)はこのセッション内でのみ保持し、終了したら破棄する。
    # 次回起動時に前回の会話を引き継ぐ機能は今回のスコープ外。
    messages = []
    while True:
        try:
            text = input("Yoriai> ").strip()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            break

        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break

        messages.append({"role": "user", "content": text})
        try:
            _ask_organization(ctx, messages)
        except KeyboardInterrupt:
            print()
            continue

    print("対話モードを終了します。")


def run_agent_with_repl(token: str, port: int) -> None:
    """対話モード(REPL)付きの起動エントリーポイント。ターミナルから直接
    `python3 yoriai.py` を実行した場合に使う。mDNS/Tailscaleの探索・
    ハートビートはバックグラウンドスレッドで継続しつつ、メインスレッドで
    対話入力を受け付ける。
    """
    ctx = _setup_agent(token, port)
    stop_event = threading.Event()
    discovery_thread = threading.Thread(target=_discovery_loop, args=(ctx, stop_event), daemon=True)
    discovery_thread.start()

    try:
        _run_repl(ctx)
    finally:
        stop_event.set()
        _shutdown_agent(ctx)


def _prompt_yes_no(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        # 仮の判断: 確認入力ができない状況(非対話実行など)では、
        # 安全側に倒して「上書きしない」ものとして扱う。
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


def _resolve_new_token(custom_passphrase) -> str:
    if custom_passphrase:
        return custom_passphrase.strip()

    # 仮の判断: --init=<合言葉> の指定が無い場合は対話的に合言葉の入力を促す。
    # 空欄のまま(または非対話実行などで入力できない)場合のみ、
    # 従来通りランダムなトークンを生成する。
    try:
        entered = input("合言葉(トークン)を入力してください(空欄でランダム生成): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        entered = ""

    return entered if entered else config.generate_token()


def _device_label() -> str:
    # 仮の判断: 案内メッセージの「このMac」「他のMac」という表現は、
    # macOS専用プロトタイプだった頃の名残でLinux(Raspberry Piなど)でも
    # 誤ってそのまま表示されていた。実行環境に応じて表現を出し分ける。
    return "Mac" if platform.system() == "Darwin" else "端末"


def handle_init(force: bool, custom_passphrase=None) -> None:
    existing_token = config.load_token()
    device = _device_label()

    if existing_token and not force:
        # 既にトークンがある場合は無条件に上書きせず、既存のトークンを案内するだけにする。
        # うっかり --init を叩き直しただけで組織が分裂してしまう事故を避けるため。
        print(f"既にこの組織のトークンが存在します: {existing_token}")
        print(f"この{device}でYoriaiを開始するには: python yoriai.py")
        print("トークンを再発行したい場合は: python yoriai.py --init --force")
        return

    if existing_token and force:
        print("既存のトークンを新しいトークンで上書きしようとしています。")
        print(f"現在のトークン: {existing_token}")
        if not _prompt_yes_no("本当に上書きしますか?(既存の組織との接続が切れます)"):
            print("キャンセルしました。既存のトークンをそのまま使用します。")
            return

    token = _resolve_new_token(custom_passphrase)
    config.save_token(token)
    # 仮の判断: --init はトークンの発行・保存のみを行い、そのまま起動はしない。
    # 発行したトークンを他の端末に共有してから `python yoriai.py` (この端末自身も含む)
    # で明示的に起動する、という2段階の操作の方が事故が少ないと判断した。
    print("新しいトークンを発行しました。このトークンを組織のメンバーに共有してください。")
    print(f"トークン: {token}")
    print(f"保存先: {config.CONFIG_FILE}")
    print(f"この{device}でYoriaiを開始するには: python yoriai.py")
    print(f"他の{device}をこの組織に参加させるには: python yoriai.py --join=<上記のトークン>")


def _format_seconds_ago(timestamp: float) -> str:
    delta = max(0, time.time() - timestamp)
    if delta < 60:
        return f"{int(delta)}秒前"
    if delta < 3600:
        return f"{int(delta // 60)}分前"
    return f"{int(delta // 3600)}時間前"


def _format_status_member(card: dict, index: int, label: str, last_seen: float = None) -> str:
    device_name = card.get("device_name", "unknown")
    chip = card.get("os", {}).get("chip", "unknown")
    memory = card.get("memory", {})
    free_gb = memory.get("free_gb")
    total_gb = memory.get("total_gb")
    installed = card.get("models", {}).get("installed", [])
    loaded = card.get("models", {}).get("loaded", [])

    if free_gb is not None and total_gb is not None:
        memory_line = f"    メモリ: 空き {free_gb}GB / 総 {total_gb}GB"
    else:
        memory_line = "    メモリ: 不明"

    lines = [
        f"[{index}] {device_name} {label}",
        f"    チップ: {chip}",
        memory_line,
        f"    ロード済みモデル: {', '.join(loaded) if loaded else 'なし'}",
        f"    インストール済みモデル({len(installed)}件): {', '.join(installed) if installed else 'なし'}",
    ]
    if last_seen is not None:
        lines.append(f"    最終確認: {_format_seconds_ago(last_seen)}")
    return "\n".join(lines)


def handle_status(port: int) -> None:
    token = config.load_token()
    if not token:
        print(NO_TOKEN_GUIDANCE)
        sys.exit(1)
    org_fingerprint = config.token_fingerprint(token)

    try:
        resp = requests.get(
            f"http://localhost:{port}/status",
            headers={ORG_FINGERPRINT_HEADER: org_fingerprint},
            timeout=CARD_REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        # 仮の判断: --status は「今動いている常駐プロセスに問い合わせるだけ」の
        # 軽量なコマンドという要件のため、エージェントが起動していない場合は
        # 新たに起動したりはせず、案内を出して終了する。
        print("実行中のYoriaiエージェントに接続できませんでした。")
        print(f"詳細: {exc}")
        print("先に python3 yoriai.py でエージェントを起動してください")
        print("(常駐化している場合は、想定しているポートで動作しているか確認してください)。")
        sys.exit(1)

    self_card = data.get("self", {})
    peers = data.get("peers", [])

    print("=== Yoriai 組織の状態 ===")
    print()
    print(_format_status_member(self_card, 1, "[自分]"))
    print()

    if not peers:
        print("現在、組織にはあなた1人だけです。")
        return

    for i, peer in enumerate(peers, start=2):
        label = f"({peer.get('via', 'unknown')}経由)"
        print(_format_status_member(peer.get("card", {}), i, label, peer.get("last_seen")))
        print()

    print(f"合計: {len(peers) + 1}台のエージェントが組織に参加中")


# --init が値なしで指定された(合言葉を対話入力する)ことを表す印。
# 「--initそのものが指定されなかった」(default=None)と区別するために使う。
_INIT_INTERACTIVE = object()


def main():
    parser = argparse.ArgumentParser(description="Yoriai: ローカルLLMエージェントの自動発見・自己紹介カード交換プロトタイプ")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--init", nargs="?", const=_INIT_INTERACTIVE, default=None, metavar="合言葉",
        help="組織を作成する(既にトークンがあれば表示のみ。--forceで再発行)。"
             "--init=<合言葉> で好きな文字列を指定でき、省略時は対話入力(空欄ならランダム生成)",
    )
    group.add_argument("--join", metavar="TOKEN", help="既存の組織のトークンを指定して参加し、そのまま起動する")
    group.add_argument(
        "--status", action="store_true",
        help="常駐中のYoriaiエージェントに問い合わせ、組織に参加しているメンバー一覧を表示して終了する",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_CARD_PORT,
        help=f"自己紹介カードを配信するHTTPポート番号(既定値: {DEFAULT_CARD_PORT}。0を指定するとOSに自動選択させる)",
    )
    parser.add_argument("--force", action="store_true", help="--init と併用し、既存トークンを確認の上で強制的に再発行する")
    args = parser.parse_args()

    if args.init is not None:
        custom_passphrase = None if args.init is _INIT_INTERACTIVE else args.init
        handle_init(args.force, custom_passphrase)
        return

    if args.status:
        handle_status(args.port)
        return

    if args.join is not None:
        token = args.join.strip()
        if not token:
            logger.error("--join には空でないトークンを指定してください")
            sys.exit(1)
        config.save_token(token)
        logger.info("トークンを設定ファイル(%s)に保存しました。この組織のエージェントとして起動します。", config.CONFIG_FILE)
    else:
        token = config.load_token()
        if not token:
            print(NO_TOKEN_GUIDANCE)
            sys.exit(1)

    # 仮の判断: 標準入力がターミナル(TTY)に繋がっているかどうかで、対話モード
    # (REPL)を出すか常駐(headless)動作にするかを自動判定する。systemd/launchd
    # から起動された場合や `curl | bash` のようにパイプ経由で動く場合は標準入力が
    # TTYではないため、従来通りの常駐動作(挙動を変えない)になる。
    if sys.stdin.isatty():
        run_agent_with_repl(token, args.port)
    else:
        run_agent(token, args.port)


if __name__ == "__main__":
    main()
