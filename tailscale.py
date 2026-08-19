"""Tailscale経由でのYoriaiエージェント発見。

mDNSはLANローカルのマルチキャストを前提としており、Tailscale越しのリモート
デバイスには原理的に届かない。そのため `tailscale status --json` で取得した
Tailscale上のピア一覧に対して、Yoriaiの自己紹介カードのHTTPエンドポイントへ
直接ポーリングを行うことで代替する。

仮の判断: mDNSと違い相手のポート番号を事前に知る手段がないため、
Yoriai側は既定のポート(yoriai.DEFAULT_CARD_PORT)で待ち受けている前提で
問い合わせる。相手が `--port` で別のポートを指定している場合はこの方式では
発見できない(次フェーズでの見直し候補)。
"""

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger("yoriai.tailscale")

STATUS_TIMEOUT_SEC = 5

# 仮の判断: GUI版Tailscale(App Store/dmg配布)は /Applications 以下のアプリバンドルに
# 実行ファイルが入っており、PATHには通っていないことが多い。また、このバイナリは
# アプリバンドルとしての起動を前提にしているらしく、シンボリックリンク経由で呼び出すと
# "Fatal error: The current bundleIdentifier is unknown to the registry"
# で失敗することを確認した。GUI版アプリの「Install command-line tool」を有効にすると
# /usr/local/bin/tailscale にこのバイナリへのシンボリックリンクが自動生成されるため、
# PATH経由(`shutil.which`)で見つかるパスがシンボリックリンクであるケースが実際にあった。
# そのため、見つかったパスがシンボリックリンクかどうかによらず、常に `os.path.realpath()`
# で実体パスに解決してから呼び出すことで、シンボリックリンク経由の起動を避ける。
CLI_CANDIDATES = [
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
]


def find_cli():
    """呼び出すtailscale CLIの実行ファイルの実体パスを探す。見つからなければNoneを返す。

    探索順序:
    1. PATHが通っている `tailscale` (CLI版、GUI版が作るsymlinkなど)
    2. GUI版のアプリバンドル内バイナリ
    3. Homebrewでの典型的な設置場所(Intel/Apple Silicon)
    いずれもシンボリックリンクである場合は `os.path.realpath()` で実体パスに解決して返す。
    """
    path_cli = shutil.which("tailscale")
    if path_cli:
        return os.path.realpath(path_cli)

    for candidate in CLI_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.realpath(candidate)

    return None


def is_available() -> bool:
    return find_cli() is not None


def get_peers(cli_path: str) -> list:
    """Tailscaleに参加中のピア(自分以外)の (hostname, ipv4) タプルのリストを返す。
    実行・解析に失敗した場合は、エラーにはせず空リストを返す
    (呼び出し側はTailscale未導入として扱えばよい)。
    """
    try:
        result = subprocess.run(
            [cli_path, "status", "--json"],
            capture_output=True, text=True, timeout=STATUS_TIMEOUT_SEC,
        )
        if result.returncode != 0:
            logger.warning("tailscale status の実行に失敗しました: %s", result.stderr.strip())
            return []
        data = json.loads(result.stdout)
    except Exception as exc:
        logger.warning("tailscale status の実行/解析に失敗しました: %s", exc)
        return []

    peers = []
    for peer in (data.get("Peer") or {}).values():
        hostname = peer.get("HostName") or peer.get("DNSName") or "unknown"
        # 仮の判断: 既存のカード配信サーバーがIPv4前提のため、IPv4アドレスのみを対象とする
        ipv4_addresses = [ip for ip in (peer.get("TailscaleIPs") or []) if "." in ip]
        if not ipv4_addresses:
            continue
        peers.append((hostname, ipv4_addresses[0]))
    return peers
