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
import os
import secrets
import stat
from pathlib import Path

logger = logging.getLogger("yoriai.config")

CONFIG_DIR = Path.home() / ".yoriai"
CONFIG_FILE = CONFIG_DIR / "config.json"

TOKEN_BYTES = 32  # ランダム生成する場合の長さ(32バイト=64文字の16進文字列)

# 仮の判断(実運用ログでの退行ループ調査への対応): llm_stream.pyのOllama向け
# /api/chat・LM Studio/MLX-LM向け/v1/chat/completionsのいずれも、temperature・
# repeat_penaltyを明示的に指定しておらず各バックエンドのその場のデフォルト値に
# 生成挙動を委ねていた。これによりノードやタイミングによって生成の一貫性が
# 変動し、堂々巡り検出で1度促されても同じツール呼び出しを繰り返す退行ループを
# 誘発しやすい状態になっていたことが実機調査で判明した。値そのものは暫定
# (temperature=0.4は決定性寄りだが完全な貪欲法ではない程度、repeat_penalty=1.15は
# Ollama/LM Studio双方のデフォルト(概ね1.0前後)より繰り返しを避ける方向に
# 少し強めた値)であり、根拠は経験則にとどまるため、実機の様子を見ながら
# 環境変数(YORIAI_TEMPERATURE・YORIAI_REPEAT_PENALTY)で調整できるようにする。
DEFAULT_TEMPERATURE = float(os.environ.get("YORIAI_TEMPERATURE", "0.4"))
DEFAULT_REPEAT_PENALTY = float(os.environ.get("YORIAI_REPEAT_PENALTY", "1.15"))

# 仮の判断: モデルによって最適な値が異なりうるため(例: 特定モデルが
# 環境変数側のデフォルトでも堂々巡りしやすいと分かった場合など)、モデル名
# ごとの個別上書きをここに追記できるようにする。キーはモデル名、値は
# "temperature"・"repeat_penalty"の一部または両方を含む辞書(省略した
# パラメータはDEFAULT_TEMPERATURE・DEFAULT_REPEAT_PENALTYが使われる)。
MODEL_SAMPLING_OVERRIDES: dict = {}


def get_sampling_params(model: str) -> dict:
    """`model`に適用するサンプリングパラメータ(temperature・repeat_penalty)を返す。

    MODEL_SAMPLING_OVERRIDESにそのモデル名の個別設定があればそれを優先し、
    無い(またはキーの一部のみ指定されている)場合はDEFAULT_TEMPERATURE・
    DEFAULT_REPEAT_PENALTYで補う。
    """
    overrides = MODEL_SAMPLING_OVERRIDES.get(model, {})
    return {
        "temperature": overrides.get("temperature", DEFAULT_TEMPERATURE),
        "repeat_penalty": overrides.get("repeat_penalty", DEFAULT_REPEAT_PENALTY),
    }


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
