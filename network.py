"""Yoriaiのネットワーク層: 発見済みピアの共有レジストリ(PeerRegistry)、
自己紹介カードをHTTPで配信するサーバー、mDNSによる自動発見、
Tailscale経由の発見。

仮の判断(モジュール分割第二弾への対応): これまで`yoriai.py`単一ファイルに
実装されていたネットワーク層のうち、依存が少なく独立性の高い塊をここに
切り出した。関数・クラスのロジックは`yoriai.py`から一切変更していない
(コピー＆import配線の変更のみ)。

ここに定義されたクラス・関数のうち一部(`PeerRegistry`・
`start_card_server`・`get_local_ip`・`get_physical_lan_ips`・
`YoriaiListener`・`discover_via_tailscale`)は、まだ`yoriai.py`側に残って
いるコード(mDNS起動処理を含む`run_agent`)からも呼ばれるため、`yoriai.py`
側で`from network import ...`して使う。逆に`CardRequestHandler`が呼ぶ
`build_profile_card`・`stream_chat_completion`は、モジュール分割第三弾で
`yoriai.py`から`llm_stream.py`へ移動した。`network.py`と`llm_stream.py`の
間に循環依存は無いため(`llm_stream.py`は`network.py`を一切importしない)、
これら2つは`llm_stream`からトップレベルで直接importしている。

`discover_via_tailscale`は既存の`tailscale.py`(Tailscale CLIのラッパー、
`find_cli`/`get_peers`)を呼び出すだけで、ロジックの重複は無い(統合は
このPRのスコープ外)。
"""

import json
import logging
import platform
import re
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import tailscale
from llm_stream import build_profile_card, stream_chat_completion
from tools import PROJECT_TOOLS_CLIENT_NAMES, PROJECT_TOOLS_SCHEMAS
from yoriai_types import (
    CARD_REQUEST_TIMEOUT_SEC,
    ORG_FINGERPRINT_HEADER,
    READ_FILE_TOOL_NAME,
    READ_FILE_TOOL_SCHEMA,
    SEARCH_IN_FILE_TOOL_NAME,
    SEARCH_IN_FILE_TOOL_SCHEMA,
    SERVICE_TYPE,
)

logger = logging.getLogger("yoriai")


# ---------------------------------------------------------------------------
# 発見済みピアの共有レジストリ(--status コマンドの情報源)
# ---------------------------------------------------------------------------

class PeerRegistry:
    """mDNS/Tailscaleで発見したピアの最新カードを保持する、複数スレッドから
    アクセスされる共有ストア。--status コマンドが問い合わせる `/status`
    エンドポイントの情報源になる。

    仮の判断: 同一LAN内かつ同じTailnetにいるデバイスは、mDNSとTailscaleの
    両方の経路で発見されうる。実機で、この場合に同一デバイスが`--status`に
    2件の別メンバーとして表示され、協業モードのメンバー数カウントにも
    影響する不具合が報告された。原因は、以前の実装が発見経路ごとの
    `agent_id`(プロセス起動のたびにランダム生成され、永続化されない)を
    キーにしていたため、本来同一のはずのデバイスでも、常駐エージェントの
    再起動などでこの値がずれると別々のエントリとして扱われてしまうこと
    だった。そのため、自己紹介カードの`device_name`(短いホスト名)を
    デバイスの一意な識別子とみなし、これをキーにして統合する
    (依頼で挙げられた「hostname、または何らかの一意な識別子」の案を採用)。
    同一`device_name`が複数経路で見えている場合、経路ごとの発見情報を
    `via_paths`にまとめて保持し、実際に問い合わせに使うアドレス・ポートは
    `_VIA_PRIORITY`の優先順位(mDNSを優先。同一LAN内での直接発見であり、
    Tailscale経由のポーリングより低遅延・低コストと考えられるため)で選ぶ。

    mDNSは「見えなくなった」イベント(remove_service)があるので即座に
    取り除けるが、Tailscale経由はそのようなイベントが無く定期スキャンの
    結果でしか判断できない。そのため、Tailscale経由の発見情報は
    「直近のスキャンで見つからなかったら消す」という形で間接的に古い情報を
    掃除している(sync_tailscale)。片方の経路の情報だけが消えても、
    もう片方の経路でまだ見えている限りデバイス自体はエントリに残る。
    """

    # 仮の判断: 同一デバイスが複数経路で見えている場合に、実際の接続先として
    # どちらを優先するかの順位。mDNS(同一LAN内)を優先する。
    _VIA_PRIORITY = ("mDNS", "Tailscale")

    def __init__(self):
        self._lock = threading.Lock()
        # device_name -> {via: {"agent_id":, "card":, "address":, "port":, "last_seen":}}
        self._peers = {}

    def upsert(self, agent_id: str, card: dict, via: str, address: str, port: int) -> None:
        # 仮の判断: device_nameが取得できない(壊れたカード等)場合の
        # フォールバックとしてagent_idをそのままキーに使う。
        device_name = card.get("device_name") or agent_id
        with self._lock:
            entry = self._peers.setdefault(device_name, {})
            entry[via] = {
                "agent_id": agent_id,
                "card": card,
                "address": address,
                "port": port,
                "last_seen": time.time(),
            }

    def remove(self, agent_id: str) -> None:
        """mDNSの`remove_service`イベント(=このメソッドの唯一の呼び出し元)に
        対応する。指定した`agent_id`のmDNS側の発見情報だけを取り除く。
        同じデバイスがTailscale経由でまだ見えている場合は、そちらの情報が
        残っている限りデバイス自体のエントリは消えない。

        仮の判断: mDNS・Tailscale両方の経路は、同一デバイス・同一プロセスで
        あれば同じ`agent_id`を報告する。そのため、経路を区別せず
        `agent_id`だけで一致判定すると、mDNS側が見えなくなっただけなのに
        Tailscale側の発見情報まで一緒に消えてしまう(実際にこの実装で
        テストが失敗し発覚した)。呼び出し元がmDNSの`remove_service`に
        限られることを踏まえ、"mDNS"経路だけを対象に一致判定する。
        """
        with self._lock:
            for device_name, via_paths in list(self._peers.items()):
                mdns_info = via_paths.get("mDNS")
                if mdns_info is not None and mdns_info["agent_id"] == agent_id:
                    del via_paths["mDNS"]
                if not via_paths:
                    del self._peers[device_name]

    def sync_tailscale(self, found_agent_ids) -> None:
        found_agent_ids = set(found_agent_ids)
        with self._lock:
            for device_name, via_paths in list(self._peers.items()):
                tailscale_info = via_paths.get("Tailscale")
                if tailscale_info is not None and tailscale_info["agent_id"] not in found_agent_ids:
                    del via_paths["Tailscale"]
                if not via_paths:
                    del self._peers[device_name]

    def snapshot(self) -> list:
        """デバイス(device_name)ごとに1件へ統合したスナップショットを返す。
        `via`には実際に見えている経路をすべて含む(例: `["mDNS", "Tailscale"]`)。
        接続先(`card`/`address`/`port`)は`_VIA_PRIORITY`で最優先の経路の
        ものを使う。`last_seen`は経路間で最新のものを使う。
        """
        with self._lock:
            result = []
            for via_paths in self._peers.values():
                if not via_paths:
                    continue
                present_vias = [v for v in self._VIA_PRIORITY if v in via_paths]
                present_vias += [v for v in via_paths if v not in self._VIA_PRIORITY]
                primary = via_paths[present_vias[0]]
                result.append({
                    "card": primary["card"],
                    "via": present_vias,
                    "address": primary["address"],
                    "port": primary["port"],
                    "last_seen": max(info["last_seen"] for info in via_paths.values()),
                })
            return result


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

        # 仮の判断: 依頼元が明示的にread_fileツール(レビュー専用)または
        # プロジェクトツール一式(修正依頼専用、read_file+ファイル作成・
        # 移動・削除・ディレクトリ作成・一覧表示・テスト実行)の提供を
        # 要求してきた場合のみ、このリクエスト限定でオファーする(通常の
        # チャットにはどちらのフラグも含まれないため、これらのツール自体が
        # 存在しないのと同じになる)。どちらのツールも、このプロセス
        # (レビュー担当・修正担当自身のキッチン)では実行せず、呼び出し元
        # (プロジェクトのファイルへの実際のアクセス権を持つ側)に実行を
        # 委ねる(client_tool_names)。詳細はREAD_FILE_TOOL_NAME定義部の
        # コメントを参照。
        offer_read_file_tool = bool(body.get("offer_read_file_tool"))
        offer_project_tools = bool(body.get("offer_project_tools"))
        # 仮の判断(不具合修正: 対話プロトコル一時停止後、実装フェーズに
        # 繋がらない問題への対応、副作用の是正): 依頼元が明示的に
        # web_search等の既定ツールを不要と申告してきた場合のみ、この
        # リクエスト限定でCHAT_TOOLSをオファーしない。
        disable_default_tools = bool(body.get("disable_web_search"))
        if offer_project_tools:
            extra_tools = PROJECT_TOOLS_SCHEMAS
            client_tool_names = PROJECT_TOOLS_CLIENT_NAMES
        elif offer_read_file_tool:
            extra_tools = [READ_FILE_TOOL_SCHEMA, SEARCH_IN_FILE_TOOL_SCHEMA]
            client_tool_names = {READ_FILE_TOOL_NAME, SEARCH_IN_FILE_TOOL_NAME}
        else:
            extra_tools = None
            client_tool_names = None

        # 仮の判断: 応答は事前にサイズが分からないストリーミングなので
        # Content-Lengthは付けず、NDJSON(1行1イベントのJSON)として都度書き出す。
        # クライアント側が対応できない場合でも、単純に行ごとに読めば動く形式にした。
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            for event in stream_chat_completion(
                model, messages, extra_tools=extra_tools, client_tool_names=client_tool_names,
                disable_default_tools=disable_default_tools,
            ):
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
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    except OSError as exc:
        # 仮の判断: 常駐化(systemd/launchd)している状態で手動で `python3 yoriai.py` を
        # 実行すると、同じポートへのbindが衝突してOSErrorの生トレースバックが出て
        # しまい原因が分かりにくかった(実機での報告により発覚)。よくある原因
        # (既に別プロセスとしてYoriaiが起動中)を案内するメッセージに変えたうえで
        # 終了する。errno 98はLinux、48はmacOSでの「アドレスは既に使用中」を表す。
        if exc.errno in (48, 98):
            logger.error("ポート %d は既に使用中です。", port)
            logger.error(
                "Yoriaiが既に別プロセス(常駐化サービスなど)として起動していないか確認してください。"
                "常駐状態を確認するには systemctl status yoriai.service (Linux) や "
                "launchctl list | grep yoriai (macOS) を、組織の状態だけ見たい場合は "
                "python3 yoriai.py --status を使ってください。"
            )
            sys.exit(1)
        raise
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
