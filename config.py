"""Yoriaiの参加トークン(組織の合言葉)をローカルに保存・読み込みするモジュール。

保存先は `~/.yoriai/config.json` の平文ファイルに統一している。
仮の判断: 個人のローカル/Tailscaleネットワーク内での利用が前提であり、
セキュリティの厳重さよりも運用のしやすさを優先して、macOS Keychainなどの
セキュアストレージへの保存は行わない方針にした(以前はKeychainを優先していたが、
シンプルな平文ファイル保存に統一した)。せめてファイルパーミッションは
所有者のみ読み書き可能に絞っている。

トークン自体をネットワークに流さないよう、mDNSでの広告やHTTPでの検証には
生トークンではなく `token_fingerprint()` で得られるSHA-256ハッシュ値のみを使う。
"""

import hashlib
import json
import logging
import secrets
import stat
from pathlib import Path

logger = logging.getLogger("yoriai.config")

CONFIG_DIR = Path.home() / ".yoriai"
CONFIG_FILE = CONFIG_DIR / "config.json"

TOKEN_BYTES = 32  # ランダム生成する場合の長さ(32バイト=64文字の16進文字列)


def generate_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """トークンそのものをmDNS/HTTPに流さないための一方向ハッシュ値を返す。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def save_token(token: str) -> None:
    """トークンを ~/.yoriai/config.json に平文で保存する。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"token": token}, ensure_ascii=False, indent=2))
    CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_token():
    """保存済みのトークンを返す。見つからなければNoneを返す。"""
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data.get("token")
    except Exception as exc:
        logger.warning("設定ファイル(%s)の読み込みに失敗しました: %s", CONFIG_FILE, exc)
        return None
