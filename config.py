"""Yoriaiの参加トークン(組織の合言葉)をローカルに保存・読み込みするモジュール。

保存先は次の優先順で選択する。
1. macOS Keychain (`security`コマンド経由) — 本来はこちらが望ましい
2. 平文ファイル `~/.yoriai/config.json` — Keychainが使えない環境向けのフォールバック

トークン自体をネットワークに流さないよう、mDNSでの広告やHTTPでの検証には
生トークンではなく `token_fingerprint()` で得られるSHA-256ハッシュ値のみを使う。
"""

import hashlib
import json
import logging
import platform
import secrets
import stat
import subprocess
from pathlib import Path

logger = logging.getLogger("yoriai.config")

CONFIG_DIR = Path.home() / ".yoriai"
CONFIG_FILE = CONFIG_DIR / "config.json"

# 仮の判断: Keychainの検索キーは固定のサービス名/アカウント名にしている。
# 将来、複数の組織のトークンを同時に持ちたくなった場合はここを見直す必要がある。
KEYCHAIN_SERVICE = "yoriai"
KEYCHAIN_ACCOUNT = "yoriai-token"

TOKEN_BYTES = 32  # 32バイト(=64文字の16進文字列)


def generate_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """トークンそのものをmDNS/HTTPに流さないための一方向ハッシュ値を返す。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _keychain_available() -> bool:
    return platform.system() == "Darwin"


def _save_to_keychain(token: str) -> bool:
    if not _keychain_available():
        return False
    try:
        # 既存エントリがあると add-generic-password が失敗することがあるため、
        # 先に削除してから追加する(存在しない場合の削除失敗は無視して良い)。
        subprocess.run(
            ["security", "delete-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE],
            capture_output=True, text=True, timeout=5,
        )
        result = subprocess.run(
            ["security", "add-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w", token, "-U"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.warning("Keychainへの保存に失敗しました: %s", exc)
        return False


def _load_from_keychain():
    if not _keychain_available():
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", KEYCHAIN_ACCOUNT, "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            return token or None
    except Exception as exc:
        logger.warning("Keychainからの読み込みに失敗しました: %s", exc)
    return None


def _save_to_file(token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"token": token}, ensure_ascii=False, indent=2))
    # 仮の判断: 本来はKeychain等のセキュアストレージで管理すべきだが、
    # フォールバック時はせめてファイルパーミッションを所有者のみ読み書き可能に絞る。
    CONFIG_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _load_from_file():
    if not CONFIG_FILE.exists():
        return None
    try:
        data = json.loads(CONFIG_FILE.read_text())
        return data.get("token")
    except Exception as exc:
        logger.warning("設定ファイル(%s)の読み込みに失敗しました: %s", CONFIG_FILE, exc)
        return None


def save_token(token: str) -> str:
    """トークンを保存する。保存先("keychain" または "file")を返す。"""
    if _save_to_keychain(token):
        return "keychain"
    logger.warning(
        "macOS Keychainへの保存ができなかったため、平文ファイル(%s)に保存します。"
        "本来はKeychain等のセキュアストレージで管理することが望ましい。",
        CONFIG_FILE,
    )
    _save_to_file(token)
    return "file"


def load_token():
    """保存済みのトークンを返す。見つからなければNoneを返す。"""
    token = _load_from_keychain()
    if token:
        return token
    return _load_from_file()
