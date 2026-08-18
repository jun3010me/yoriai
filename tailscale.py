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
import shutil
import subprocess

logger = logging.getLogger("yoriai.tailscale")

STATUS_TIMEOUT_SEC = 5


def is_available() -> bool:
    return shutil.which("tailscale") is not None


def get_peers() -> list:
    """Tailscaleに参加中のピア(自分以外)の (hostname, ipv4) タプルのリストを返す。
    tailscaleコマンドが無い場合や実行・解析に失敗した場合は、エラーにはせず
    空リストを返す(呼び出し側はTailscale未導入として扱えばよい)。
    """
    if not is_available():
        return []

    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
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
