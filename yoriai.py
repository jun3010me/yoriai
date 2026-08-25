#!/usr/bin/env python3
"""Yoriai: 同一ネットワーク上のローカルLLMエージェントを mDNS で自動発見し、
自己紹介カード(JSON)を交換する最小構成プロトタイプ。

対象OS: macOS (Apple Silicon) / Linux (Raspberry Pi 4を含むDebian系など)。
"""

import argparse
import ast
import datetime
import json
import locale
import logging
import os
import platform
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser as _HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 仮の判断: `readline`モジュールをimportするだけで、以降の`input()`呼び出しが
# GNU readline(macOSではlibedit)による行編集(矢印キーでのカーソル移動、
# Backspace、Ctrl+A/E、上下矢印での入力履歴)を使うようになる(CPython標準の
# 既知の挙動で、`readline`の関数を明示的に呼ぶ必要はない)。実機で「対話モード
# で矢印キーを押すと`^[[D`のようなエスケープシーケンスがそのまま入力されて
# しまう」という報告があり、原因は`readline`が一度もimportされていなかった
# ことだった。Windowsや一部の最小構成Pythonには`readline`が無いことがあるが、
# その場合でも`input()`自体は行編集機能なしで動作し続けるため、try/exceptで
# 握りつぶして対話モード全体には影響しないようにしている。
try:
    import readline  # noqa: F401
except ImportError:
    pass

import requests
from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, IPVersion, InterfaceChoice

import config
import tailscale

SERVICE_TYPE = "_yoriai._tcp.local."
OLLAMA_BASE_URL = "http://localhost:11434"
LMSTUDIO_BASE_URL = "http://localhost:1234"
# 仮の判断: mlx_lm.serverの既定ポート(8080)を前提とする。
MLX_LM_BASE_URL = "http://localhost:8080"
CARD_REQUEST_TIMEOUT_SEC = 5
ORG_FINGERPRINT_HEADER = "X-Yoriai-Org-Fingerprint"
HEARTBEAT_INTERVAL_SEC = 10

# 仮の判断: チャットの接続確立自体はカード取得と同程度の速さで判定してよいが、
# LLMの生成そのものは(モデルサイズや質問内容によっては)数十秒かかることが
# あるため、読み取りタイムアウトは長めに取る。
CHAT_CONNECT_TIMEOUT_SEC = 5
CHAT_READ_TIMEOUT_SEC = 120

# 仮の判断: バックエンド(Ollama/LM Studio/MLX-LM)への問い合わせに応答の
# 最大トークン数を指定していなかったため、複雑な修正依頼(複数の指摘を
# 同時に反映しようとする等)を受けたモデルが、生成をやめる判断ができずに
# 応答を延々と続けてしまう不具合が実機で報告された(index.htmlの修正で
# 13,819トークンから20,000超まで際限なく増え続けた事例。手動でのCtrl+C×2
# による強制終了が必要だった)。読み取りタイムアウト(CHAT_READ_TIMEOUT_SEC)は
# トークンが実際に流れ続けている間は働かない(データが届く限り「タイムアウト」
# にはならない)ため、この問題への歯止めにはならない。そのため、生成される
# トークン数そのものに上限を設ける。値は、HTML/CSS/JS学習サイトの1ファイル
# 丸ごとの書き直しのような大きめの生成でも収まる余裕を持たせつつ、暴走時の
# 被害(待ち時間・トークン消費)を現実的な範囲に抑えられる値として選んだ。
CHAT_MAX_OUTPUT_TOKENS = 8192

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


def get_mlx_lm_models() -> list:
    """MLX-LM(`python -m mlx_lm.server`)のOpenAI互換APIからモデル一覧を取得する。

    仮の判断: MLX-LMのサーバーは起動時に指定した1つのモデルだけを保持する
    アーキテクチャで、Ollamaのように複数モデルをインストールしておいて
    要求ごとに切り替える、という概念が無い。そのためLM Studioと同様、
    ここで取得できたモデルは「インストール済み」「ロード済み」の両方として
    扱う。既定ポートは8080(mlx_lm.serverの既定値)を前提としており、
    別ポートで起動している場合は検出できない(設定可能にするのは今回の
    スコープ外)。MLX-LMのサーバーが起動していない場合は空リストを返す
    (エラーにはしない)。
    """
    try:
        resp = requests.get(f"{MLX_LM_BASE_URL}/v1/models", timeout=3)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]
    except Exception as exc:
        logger.warning("MLX-LMのモデル一覧の取得に失敗しました: %s", exc)
        return []


def _merge_model_lists(*model_lists: list) -> list:
    merged = []
    for models in model_lists:
        for name in models:
            if name not in merged:
                merged.append(name)
    return merged


# ---------------------------------------------------------------------------
# ウェブ検索ツール(SearXNG)
# ---------------------------------------------------------------------------

WEB_SEARCH_TOOL_NAME = "web_search"

# 仮の判断: ツールはOllama/LM StudioどちらもOpenAI互換のtools形式
# (type: function)を受け付けるため、共通のスキーマを1つだけ用意する。
WEB_SEARCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": WEB_SEARCH_TOOL_NAME,
        "description": "インターネットを検索して、最新の情報や自分の知識だけでは分からない情報を調べる。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "検索したいキーワードや質問文"},
            },
            "required": ["query"],
        },
    },
}

CHAT_TOOLS = [WEB_SEARCH_TOOL_SCHEMA]

# ---------------------------------------------------------------------------
# ファイル読み取りツール(協業モードのレビュー専用)
# ---------------------------------------------------------------------------
#
# 仮の判断(実機で発見された不具合を受けての設計変更): 当初は、レビュー
# 依頼のリクエストボディに実装依頼元が「その時点でプロジェクトに実際に
# 保存されているファイルの中身」をavailable_filesとして直接埋め込み、
# read_fileの実行(_execute_tool_call)はレビュー担当自身のプロセス内で
# その辞書を参照するだけ、という設計にしていた。しかしこの方式では、
# available_filesを埋め込む(=リクエストを送信する)タイミングのスナップ
# ショットが固定されてしまい、LLMの応答生成中(read_fileが実際に呼ばれる
# までの間)に他のワーカースレッドが新しいファイルを完成させても、その
# 更新がレビュー担当には一切反映されない不具合が実機で見つかった
# (2回目のレビュー時点で実際には完成していたconfig.pyが「存在しない」と
# 誤判定され、無駄にレビュー往復の上限を使い切ってタスクが未完了のまま
# 打ち切られた)。
#
# 修正後は、read_fileを「レビュー担当のプロセス内では実行しない、
# 呼び出し元(実装依頼元)が引き取って実行するツール」として扱う。
# stream_chat_completionは、モデルがread_fileを呼び出そうとした時点で
# 実行はせず{"pending_tool_calls": [...]}をyieldして処理を打ち切り、
# それを受け取った実装依頼元(_collect_review_answer_with_read_file)が
# その場でout_dirを読み直して結果を作り、会話を継続する新しい/chat
# リクエストとして送り返す。/chatはそもそも1回ごとに会話履歴
# (messages)を丸ごと送るステートレスな設計になっているため、この
# 「続きから再開する」やり取りは自然に実現できる。この方式により、
# read_fileが実際に呼ばれた瞬間の最新のディスクの状態を都度反映できる。
READ_FILE_TOOL_NAME = "read_file"
READ_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": READ_FILE_TOOL_NAME,
        "description": (
            "レビュー中のプロジェクトに含まれる、他のファイルの実際の中身を読み取る。"
            "自分が担当していないファイルとの連携(関数名・引数・データ形式が"
            "合っているか等)を、推測ではなく実際のコードを確認してから判断したい"
            "場合に使う。まだ実装されていないファイルを指定した場合は、その旨が返される。"
            "引数を省略するとファイル全体を返すが、大きなファイルを確認する際は、まず"
            "search_in_fileで関連箇所を探し、start_line/end_line(またはlineと"
            "context_lines)で必要な範囲だけを指定して読むことを検討すること。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "読みたいファイル名(例: storage.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "start_line": {
                    "type": "integer",
                    "description": "読み始めたい行番号(1始まり)。省略時はファイル全体を返す。",
                },
                "end_line": {
                    "type": "integer",
                    "description": "読み終わりたい行番号。省略時はファイル末尾(または開始行から一定範囲)まで。",
                },
                "line": {
                    "type": "integer",
                    "description": (
                        "特定の行番号を中心に前後だけ読みたい場合に指定する"
                        "(context_linesと併用。start_line/end_lineの代わりに使える)。"
                    ),
                },
                "context_lines": {
                    "type": "integer",
                    "description": "lineを指定した場合に、その前後何行を含めるか(省略時は20行)。",
                },
            },
            "required": ["filename"],
        },
    },
}

SEARCH_IN_FILE_TOOL_NAME = "search_in_file"
SEARCH_IN_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": SEARCH_IN_FILE_TOOL_NAME,
        "description": (
            "他のファイルの中身全体をread_fileで読む前に、まずキーワードや関数名で"
            "ファイル内を検索し、一致した行番号とその前後数行の抜粋だけを確認する。"
            "大きなファイルの該当箇所に見当をつけてから、read_fileでその範囲だけを"
            "読むと、無駄なやり取り・トークン消費を減らせる。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "検索したいファイル名(例: storage.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "query": {"type": "string", "description": "検索したいキーワードまたは関数名"},
                "context_lines": {
                    "type": "integer",
                    "description": "一致箇所の前後何行を抜粋に含めるか(省略時は5行)。",
                },
            },
            "required": ["filename", "query"],
        },
    },
}

# 仮の判断(依頼への対応: search_in_file/read_fileの部分読み機能の導入):
# Claude Codeの「まずGrepで見当をつけ、その範囲だけをReadする」という
# 2段階アプローチをYoriaiにも導入するための共通の案内文。read_fileの
# 呼び出しを提供するすべてのプロンプト(レビュー専用・修正依頼専用の
# 両方)で同じ文言を使い回すことで、案内内容がずれないようにする。
_SEARCH_THEN_READ_GUIDANCE = (
    "大きなファイルを確認する際は、まずsearch_in_fileで関連箇所を探し、"
    "必要な範囲だけをread_fileで読むことを検討してください。"
)

# 仮の判断: 依頼の「1回のレビューにつき、ファイル読み取りツールの呼び出しは
# 最大3回程度までに制限してほしい」という要件に対応する上限。MAX_TOOL_CALL_ROUNDS
# (ツール呼び出しラウンド全体の上限、web_searchも含む)とは別に、read_file単体の
# 呼び出し回数をレビュー1回(=stream_chat_completionの1回の実行)ごとにカウントする。
MAX_READ_FILE_CALLS_PER_REVIEW = 3

# 仮の判断: モデルが延々とツール呼び出しを繰り返すループに陥らないよう、
# 1つの質問あたりのツール呼び出しラウンド数に上限を設ける。
MAX_TOOL_CALL_ROUNDS = 3

# 仮の判断: 4xxのうち400(Bad Request)/422(Unprocessable Entity)は
# 「リクエストの中身が原因で拒否された」ことを示す代表的なクラスのエラーだが、
# LM Studioなどのバックエンドは、tools付きリクエストが原因のエラー以外にも
# (モデルのロード失敗・メモリ不足など、tools無し再試行では絶対に解決しない
# 種類のエラーも含めて)同じ400を返してくることが実機の検証で分かった
# (例: 「Model loading was stopped due to insufficient system resources」)。
# ステータスコードだけでは判別できないため、エラーメッセージの本文に
# ツール/Function Calling関連の語が含まれているかどうかも合わせて確認し、
# 両方の条件を満たした場合のみ「ツールが原因で拒否された」とみなす。
TOOLS_UNSUPPORTED_STATUS_CODES = (400, 422)
_TOOLS_UNSUPPORTED_ERROR_KEYWORDS = ("tool", "function calling", "function_call")


def _looks_like_tools_related_error(error_text: str) -> bool:
    lowered = (error_text or "").lower()
    return any(keyword in lowered for keyword in _TOOLS_UNSUPPORTED_ERROR_KEYWORDS)

WEB_SEARCH_MAX_RESULTS = 5
WEB_SEARCH_TIMEOUT_SEC = 10
# 仮の判断: 検索バックエンドは自宅のDocker/Proxmox環境に構築中のSearXNGインスタンス
# (JSON APIとして問い合わせる)を使う。ホスト名・IPを決め打ちにせず、環境変数
# YORIAI_SEARXNG_URLで指定できるようにしている(Tailscale経由でホスト名から
# アクセスする可能性もあるため、IPに限らず普通のURL文字列として扱う)。
# 未設定時は開発時に確認済みのインスタンスのURLを既定値として使う。
#
# 注意: SearXNG側でJSON形式のレスポンスを許可しておく必要がある
# (settings.ymlの `search: formats: - json` を有効にすること)。
SEARXNG_BASE_URL = os.environ.get("YORIAI_SEARXNG_URL", "http://192.168.11.190:8888")


def web_search(query: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list:
    """SearXNGインスタンスのJSON APIに問い合わせてウェブ検索し、結果のリストを返す。

    仮の判断: SearXNGインスタンス側の障害(未起動・タイムアウト・不正な
    レスポンス等)が起きた場合も例外を投げずに空リストを返し、モデル側には
    「検索結果が得られなかった」ことだけ伝える(呼び出し元のフォールバック
    挙動を変えないため)。
    """
    try:
        resp = requests.get(
            f"{SEARXNG_BASE_URL}/search",
            params={"q": query, "format": "json"},
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, WEB_SEARCH_TIMEOUT_SEC),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("ウェブ検索に失敗しました: %s", exc)
        return []

    results = []
    for r in data.get("results", [])[:max_results]:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        results.append({"title": title, "url": r.get("url", ""), "snippet": (r.get("content") or "").strip()})
    return results


def _execute_tool_call(tool_call: dict) -> str:
    """モデルからのtool_call(OpenAI互換形式)を実行し、モデルに返す結果を
    JSON文字列として返す。

    仮の判断: read_file(協業モードのレビュー専用)はここでは実行しない。
    stream_chat_completionは、read_fileが要求されたラウンドではこの関数を
    呼ばずに{"pending_tool_calls": ...}をyieldして処理を呼び出し元に
    委ねる(理由はREAD_FILE_TOOL_NAME定義部のコメントを参照)。この関数は
    web_searchのように「どのメンバーのプロセスで実行しても結果が変わらない」
    ツールだけを扱う。
    """
    function = tool_call.get("function", {})
    name = function.get("name")
    arguments = function.get("arguments", {})
    # 仮の判断: argumentsはOllamaでは辞書、LM Studio(OpenAI互換)では
    # JSON文字列で来ることが多いため、両方に対応する。
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments = {}

    if name == WEB_SEARCH_TOOL_NAME:
        query = arguments.get("query", "")
        results = web_search(query)
        return json.dumps({"query": query, "results": results}, ensure_ascii=False)

    return json.dumps({"error": f"不明なツールです: {name}"}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# チャットのプロキシ(Ollama/LM Studioへのストリーミング問い合わせ)
# ---------------------------------------------------------------------------

# 仮の判断: requestsの`raise_for_status()`はステータス行(例: "400 Client Error:
# Bad Request for url: ...")しか例外メッセージに含めず、レスポンスボディ
# (バックエンドが返す具体的な原因メッセージ)は失われてしまう。実機で
# 「400になるが原因が分からない」という報告があったため、ボディの内容を
# ログとエラーイベントの両方に残すヘルパーを共通化した。
def _log_and_build_http_error(base_url: str, resp) -> dict:
    body_text = resp.text[:2000]  # 万一巨大なエラーページ等が返ってきても肥大化させない
    detail = body_text
    try:
        body_json = resp.json()
        error_field = body_json.get("error")
        if isinstance(error_field, dict):
            detail = error_field.get("message") or body_text
        elif isinstance(error_field, str):
            detail = error_field
    except Exception:
        pass
    logger.warning("%s への問い合わせが%dで拒否されました。レスポンス: %s", base_url, resp.status_code, body_text)
    return {"error": f"{resp.status_code}: {detail}", "status_code": resp.status_code}


def _stream_ollama_turn(model: str, messages: list, tools: list):
    """OllamaのネイティブAPI(/api/chat、NDJSONストリーミング)に1往復だけ
    問い合わせ、正規化したイベント({"content": ...} / {"error": ...})を
    順にyieldし、最後にそのターンで要求されたtool_calls一覧と、応答が
    CHAT_MAX_OUTPUT_TOKENS上限に達して打ち切られたかどうか
    ({"tool_calls": [...], "truncated": bool})をyieldする。
    """
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": model, "messages": messages, "tools": tools, "stream": True,
                "options": {"num_predict": CHAT_MAX_OUTPUT_TOKENS},
            },
            stream=True,
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, CHAT_READ_TIMEOUT_SEC),
        )
        if not resp.ok:
            yield _log_and_build_http_error(OLLAMA_BASE_URL, resp)
            return
    except Exception as exc:
        yield {"error": str(exc)}
        return

    tool_calls = []
    truncated = False
    try:
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = obj.get("message", {})
            content = message.get("content")
            if content:
                yield {"content": content}
            if message.get("tool_calls"):
                tool_calls.extend(message["tool_calls"])
            if obj.get("done"):
                # 仮の判断: Ollamaはdone_reasonで打ち切り理由を返す
                # ("stop"は正常終了、"length"はnum_predict上限による打ち切り)。
                truncated = obj.get("done_reason") == "length"
                break
    except Exception as exc:
        yield {"error": str(exc)}
        return
    yield {"tool_calls": tool_calls, "truncated": truncated}


def _stream_openai_compatible_turn(base_url: str, model: str, messages: list, tools: list):
    """OpenAI互換のstreaming chat completions API(/v1/chat/completions、SSE)に
    1往復だけ問い合わせ、正規化イベントを順にyieldする。LM Studio・MLX-LMは
    どちらもこの同じワイヤ形式を話すため、共通の実装として1箇所にまとめている。

    仮の判断: OpenAI互換のストリーミングではtool_callsが複数チャンクに
    分割されて送られてくる(引数のJSON文字列が少しずつ届く)ため、
    indexごとに文字列を連結して組み立てる。
    """
    try:
        resp = requests.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model, "messages": messages, "tools": tools, "stream": True,
                "max_tokens": CHAT_MAX_OUTPUT_TOKENS,
            },
            stream=True,
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, CHAT_READ_TIMEOUT_SEC),
        )
        if not resp.ok:
            yield _log_and_build_http_error(base_url, resp)
            return
    except Exception as exc:
        yield {"error": str(exc)}
        return

    tool_calls_by_index = {}
    truncated = False
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
            if not choices:
                continue
            choice = choices[0]
            # 仮の判断: OpenAI互換APIはfinish_reasonで打ち切り理由を返す
            # ("stop"は正常終了、"length"はmax_tokens上限による打ち切り)。
            if choice.get("finish_reason") == "length":
                truncated = True
            delta = choice.get("delta", {})
            content = delta.get("content")
            if content:
                yield {"content": content}
            for tc_delta in delta.get("tool_calls") or []:
                index = tc_delta.get("index", 0)
                entry = tool_calls_by_index.setdefault(index, {"id": None, "function": {"name": "", "arguments": ""}})
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                fn_delta = tc_delta.get("function") or {}
                if fn_delta.get("name"):
                    entry["function"]["name"] += fn_delta["name"]
                if fn_delta.get("arguments"):
                    entry["function"]["arguments"] += fn_delta["arguments"]
    except Exception as exc:
        yield {"error": str(exc)}
        return

    # 仮の判断: OpenAI互換のtool_calls形式は各要素に"type": "function"を
    # 必須で要求する。これを省いたまま次のラウンドでmessages履歴に含めて
    # 送り返すと、LM Studio側のスキーマ検証で400 Bad Requestになることが
    # 実機で見つかった。またidもストリーミングの途中で一度も送られてこず
    # nullのままになることがあり、これも同様に不正な形として拒否されうる
    # ため、その場合は仮のIDを補って必ず有効な文字列にする。
    tool_calls = [
        {
            "id": entry["id"] or f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": entry["function"],
        }
        for _, entry in sorted(tool_calls_by_index.items())
    ]
    yield {"tool_calls": tool_calls, "truncated": truncated}


def _stream_lmstudio_turn(model: str, messages: list, tools: list):
    yield from _stream_openai_compatible_turn(LMSTUDIO_BASE_URL, model, messages, tools)


def _stream_mlx_lm_turn(model: str, messages: list, tools: list):
    # 仮の判断: MLX-LMのFunction Calling対応はバックエンド/バージョンによって
    # 未対応・不安定な場合がある。未対応の場合はエラーになるかtoolsを無視して
    # 通常のチャットとして応答するかのどちらかになりうるが、いずれの場合も
    # 既存のエラーハンドリング/漏れ検出フォールバックがそのまま機能するため、
    # ここで特別な分岐は設けていない。
    yield from _stream_openai_compatible_turn(MLX_LM_BASE_URL, model, messages, tools)


# 仮の判断: 一部のモデル/バックエンドの組み合わせ(例: LM Studioで一部の
# DeepSeek系モデルを動かした場合)では、ツール呼び出しが構造化された
# tool_callsとして返らず、モデル自身の内部的なツール呼び出し記法
# (DeepSeekのDSML記法 `<｜DSML｜tool_calls>`、Qwen/Hermes系の`<tool_call>`、
# Mistralの`[TOOL_CALLS]`、Llamaの`<|python_tag|>`など)がそのまま回答本文
# (content)として画面に漏れてしまうことが実機での報告により見つかった。
#
# 仮の判断: 当初`<｜`(フルワイド縦棒 U+FF5C)や"DSML"という文字列そのものを直接
# パターンに含めていたが、いずれも実機で再現しなかった(検出できずに漏れが
# 素通りしてしまった)。スクリーンショットの見た目だけでは、実際の文字が
# 通常のASCII("D""S""M""L"、半角の`<`/`|`)なのか、それとも似た形の別の
# Unicode文字(全角のＤＳＭＬや｜など)なのかを正確に判別できない。
# そこで文字の見た目の一致に頼るのをやめ、判定前に文字列をUnicode正規化
# (NFKC)してから既知のパターンと照合するようにした。NFKCは全角英数字
# (Ｄ→D)や全角記号(｜→|、＜→<など「互換分解」を持つ文字)を対応する
# 半角/標準形に変換するため、モデルが全角文字で特殊トークンを表現していても、
# 半角のASCIIパターンだけを用意しておけば検出できるようになる。
_LEAKED_TOOL_CALL_PATTERN = re.compile(
    r"DSML"
    r"|<\/?\s*[|｜]"  # 正規化後は基本<|/</|の形になるが、念のため全角も残す
    r"|<tool_call>"
    r"|\[TOOL_CALLS\]"
    r"|<\|python_tag\|>"
    r"|<function_call>"
    r"|tool[_▁]calls?[_▁]begin",
    re.IGNORECASE,
)
# 検出のためにcontentの先頭だけ少量バッファする文字数の上限。
# ここまで溜めても記法にマッチしなければ、通常のストリーミング応答とみなす。
_LEAK_PEEK_CHARS = 30

# 仮の判断: 既知の漏れ記法はすべて(NFKC正規化後は)`<`か`[`で始まる。答えの
# 書き出しの1文字目がそれ以外であれば、その時点で「漏れではない」と確定して
# よい。これにより、大半の通常の応答では待ち時間なしでストリーミング表示が
# 始まる(バッファはあくまで`<`/`[`から始まる場合の"念のための確認"用)。
_LEAK_TRIGGER_CHARS = "<["


def _run_turn_with_leak_detection(turn, tools: list):
    """1ターン分の応答イベントを中継しつつ、ツール呼び出しをオファーした
    ときだけ、先頭のcontentが漏れたツール呼び出し記法でないかを確認する。
    漏れを検出した場合は、それ以降のcontentを画面に出さずに捨て、代わりに
    {"tool_call_failed": True} を1回だけyieldする(content/tool_callsの
    どちらも実質的には返さない)。

    仮の判断: 判定のためにcontentの先頭を少量バッファする間も、元のチャンクの
    区切り(トークン単位)は保ったままリプレイする。1つの大きな塊に結合して
    出すと、バッファ分だけ「一括表示」に戻ってしまい、ストリーミング表示という
    フェーズ5の目的が損なわれるため。
    """
    state = "peeking" if tools else "streaming"
    buffered_chunks = []
    buffered_text = ""
    for event in turn:
        if "content" not in event:
            yield event
            continue
        if state == "streaming":
            yield event
            continue
        if state == "leaked":
            continue  # 漏れ検出後のcontentは画面に出さず捨てる

        chunk = event["content"]
        buffered_chunks.append(chunk)
        buffered_text += chunk
        # 全角文字での偽装(全角のＤＳＭＬ、｜、＜など)を素の文字列比較で
        # 見逃さないよう、判定はNFKC正規化した文字列に対して行う。
        normalized = unicodedata.normalize("NFKC", buffered_text)
        if _LEAKED_TOOL_CALL_PATTERN.search(normalized):
            state = "leaked"
            yield {"tool_call_failed": True}
            buffered_chunks = []
            continue

        starts_safely = bool(normalized) and normalized[0] not in _LEAK_TRIGGER_CHARS
        if starts_safely or len(buffered_text) >= _LEAK_PEEK_CHARS:
            if not starts_safely:
                # 仮の判断: `<`/`[`で書き出されたのに既知のパターンに一致しないまま
                # バッファ上限に達したケース(=検出漏れの可能性がある境界事例)を、
                # 生のバイト列(repr)付きでログに残す。過去に見た目だけでは
                # 正確な文字が判別できず検出漏れを2度繰り返した反省から、次に
                # 同種の問題が起きた際は勘に頼らずログから正確に原因を特定できる
                # ようにしている。
                logger.info("ツール呼び出し記法の漏れチェック: 既知パターン不一致のまま先頭バッファを確定します: %r", buffered_text)
            state = "streaming"
            for buffered_chunk in buffered_chunks:
                yield {"content": buffered_chunk}
            buffered_chunks = []

    if state == "peeking":
        for buffered_chunk in buffered_chunks:
            yield {"content": buffered_chunk}


def stream_chat_completion(model: str, messages: list, extra_tools: list = None, client_tool_names: set = None):
    """モデル名からOllama/LM Studio/MLX-LMのどれにチャットを振るかを決め、
    正規化されたストリーミングイベント({"content": ...} / {"tool_call": <ツール名>} /
    {"pending_tool_calls": [...]} / {"done": True, "truncated": bool} /
    {"error": ...})を順にyieldする。`truncated`は、最後のラウンドの応答が
    CHAT_MAX_OUTPUT_TOKENS上限に達して途中で打ち切られたかどうかを表す。

    モデルがツール(既定ではweb_searchのみ)の呼び出しを要求した場合は、
    ここでツールを実行して結果を会話履歴に追加し、モデルに再度問い合わせる
    (最大MAX_TOOL_CALL_ROUNDSラウンドまで)。呼び出し元(REPL等)からは
    ツールの存在を意識せず、通常のチャットと同じように使える。

    `extra_tools`(協業モードのレビュー専用read_file等)を渡すと、常時
    有効なCHAT_TOOLSに加えてこのリクエストだけ追加のツールをオファーする。

    `client_tool_names`に名前が含まれるツールは、ここでは実行しない
    (`_execute_tool_call`を呼ばない)。代わりに、そのツールが要求された
    ラウンドで{"pending_tool_calls": [そのラウンドの全tool_calls]}を
    yieldしてジェネレータを終了し、実行を呼び出し元に委ねる。read_file
    (協業モードのレビュー専用)がこれに該当する: プロジェクトのファイルは
    実装依頼元のローカルディスクにしかなく、このジェネレータを実行している
    プロセス(レビュー担当自身のキッチン)では正しい結果を返せないため。
    呼び出し元(実装依頼元)は、pending_tool_callsを受け取ったら自分で
    実行し、結果を会話履歴に追加した新しいメッセージ列で改めて
    /chatを呼び直すことで会話を継続する(/chatはメッセージ履歴を毎回
    丸ごと送るステートレスな設計のため、この「続きから再開する」やり取りが
    自然に行える)。

    仮の判断: フェーズ5では「タスクの難易度に応じた賢い振り分け」はスコープ外の
    ため、モデルの選定自体は呼び出し元(候補選定ロジック)に任せ、ここでは
    単純に「Ollamaのロード済みモデル一覧に名前があればOllama、なければ
    MLX-LMのモデル一覧に名前があればMLX-LM、どちらでもなければLM Studio」
    というバックエンドの振り分けだけを行う。LM Studioを最後のデフォルトに
    しているのは、フェーズ5時点からの既存の挙動を変えないため。
    Ollama/MLX-LMどちらのモデル一覧にも同名のモデルが存在する場合はOllama
    側が優先される(仮の判断、Ollamaを優先する明確な理由は無いが決め打ちが必要だった)。

    仮の判断: バックエンド/モデルの組み合わせによっては、ツール呼び出しの
    構造化出力に対応しておらず、モデル自身の内部記法がそのまま回答本文に
    漏れてしまうことがある(_run_turn_with_leak_detectionで検出)。検出した
    場合は{"tool_call_failed": True}を1回だけyieldしたうえで、ツール無しで
    同じ質問を自動的に再試行する(以降のラウンドもツールは使わない)。

    仮の判断: 上記の「回答に漏れる」パターンとは別に、モデル/バックエンドの
    組み合わせによっては`tools`パラメータを含めたリクエスト自体をその場で
    (何も生成せずに)400/422で拒否してくることが実機の報告で見つかった
    (例: LM Studio上のqwen3-coder-30b)。これも「ツール呼び出しに対応して
    いない」の一種とみなし、同じくツール無しで自動的に再試行する。ただし
    400/422はモデルのロード失敗(メモリ不足など、tools無し再試行では
    絶対に解決しない別種の原因)でも返ってくることが実機で分かったため、
    ステータスコードに加えてエラー文にツール関連の語(_looks_like_tools_related_error)
    が含まれるかどうかも確認したうえで再試行の要否を判断する。
    """
    messages = list(messages)  # 呼び出し元のリストをツール実行の追記で汚さない
    tools = CHAT_TOOLS + list(extra_tools) if extra_tools else CHAT_TOOLS

    for round_num in range(MAX_TOOL_CALL_ROUNDS + 1):
        if model in get_ollama_loaded_models():
            turn = _stream_ollama_turn(model, messages, tools)
        elif model in get_mlx_lm_models():
            turn = _stream_mlx_lm_turn(model, messages, tools)
        else:
            turn = _stream_lmstudio_turn(model, messages, tools)

        tool_calls = []
        round_truncated = False
        leaked = False
        tools_rejected = False
        for event in _run_turn_with_leak_detection(turn, tools):
            if "error" in event:
                status_code = event.get("status_code")
                # 仮の判断: 「tools無し再試行が発動したかどうか」がログから一目で
                # 分かるよう、発動する場合・しない場合の両方で明示的にログを出す
                # (「ロジックが呼ばれたのか、そもそも古いコードのままなのか」の
                # 切り分けに使えるようにするため)。
                status_code_matches = status_code in TOOLS_UNSUPPORTED_STATUS_CODES
                text_looks_tools_related = _looks_like_tools_related_error(event.get("error", ""))
                if tools and status_code_matches and text_looks_tools_related:
                    logger.warning(
                        "[tools無し再試行] %s がツール付きリクエストをステータス%sで"
                        "拒否しました(エラー文にツール関連の記述が含まれるため、"
                        "モデルがツール呼び出しに対応していない可能性があります)。"
                        "同一モデル(%s)へツールなしで再試行します。",
                        model, status_code, model,
                    )
                    tools_rejected = True
                    yield {"tool_call_failed": True}
                    break
                if tools and status_code_matches and not text_looks_tools_related:
                    # 仮の判断: ステータスコードだけを見て機械的にツール無し再試行に
                    # 突入すると、モデルのロード失敗(メモリ不足など、ツールとは
                    # 無関係な原因)でも同じ400/422を返すバックエンドがあり、無駄な
                    # 再試行(=同じ理由でまた失敗するだけの試行)をしてしまうことが
                    # 実機で見つかった。エラー文にツール/Function Calling関連の
                    # 語が含まれない場合は「ツールが原因ではない」とみなし、
                    # 再試行せずそのままエラーとして扱う。
                    logger.warning(
                        "[tools無し再試行なし] %s への問い合わせがステータス%sで失敗しましたが、"
                        "エラー文にツール関連の記述が見つからなかったため、ツールが原因の"
                        "拒否ではないと判断しました。ツールなし再試行は行わず、"
                        "このままエラーとして扱います。詳細: %s",
                        model, status_code, event.get("error"),
                    )
                elif tools:
                    logger.warning(
                        "[tools無し再試行なし] %s への問い合わせでエラーが発生しましたが、"
                        "ツールなし再試行の対象(ステータス%s)ではないと判断しました"
                        "(status_code=%s)。このままエラーとして扱います。",
                        model, TOOLS_UNSUPPORTED_STATUS_CODES, status_code,
                    )
                yield event
                return
            if event.get("tool_call_failed"):
                leaked = True
                yield event
                continue
            if "content" in event:
                yield event
            elif "tool_calls" in event:
                tool_calls = event["tool_calls"]
                round_truncated = event.get("truncated", False)

        if (leaked or tools_rejected) and tools:
            tools = None  # このモデルにはツールを二度とオファーせず、素の会話として再試行する
            continue

        if not tool_calls or round_num == MAX_TOOL_CALL_ROUNDS:
            break

        # 仮の判断: このラウンドのtool_callsに1つでもclient_tool_names該当
        # (read_file等)が含まれる場合、ここでは一切実行せずラウンド全体を
        # 呼び出し元に渡して終了する。1ラウンドでweb_searchとread_fileが
        # 混在するようなケースの部分実行は複雑さの割に実利が薄いため扱わず、
        # 呼び出し元側にラウンド全体の実行を委ねる単純な設計にした。
        if client_tool_names and any(
            tool_call.get("function", {}).get("name", "") in client_tool_names for tool_call in tool_calls
        ):
            yield {"pending_tool_calls": tool_calls}
            return

        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        for tool_call in tool_calls:
            tool_name = tool_call.get("function", {}).get("name", "")
            yield {"tool_call": tool_name, "tool_call_arguments": tool_call.get("function", {}).get("arguments", "")}
            result = _execute_tool_call(tool_call)
            messages.append({
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.get("id"),
            })

    yield {"done": True, "truncated": round_truncated}


def build_profile_card(agent_id: str) -> dict:
    # 仮の判断: 「インストール済み/ロード済みモデル」と「利用可能なバックエンド」を
    # それぞれ別々の関数で問い合わせると、同じバックエンド(特にLM Studio・
    # MLX-LMのように問い合わせ1回でインストール済み=ロード済みを兼ねるもの)に
    # 何度も重複してHTTPリクエストを送ることになるため、ここで各バックエンドに
    # 1回ずつ問い合わせて使い回す。
    ollama_installed = get_ollama_installed_models()
    ollama_loaded = get_ollama_loaded_models()
    lmstudio_models = get_lmstudio_models()
    mlx_lm_models = get_mlx_lm_models()

    backends = []
    if ollama_installed:
        backends.append("ollama")
    if mlx_lm_models:
        backends.append("mlx_lm")
    if lmstudio_models:
        backends.append("lmstudio")

    # 仮の判断: 同一メンバーが複数のバックエンド(例: MacStudioでLM StudioとMLX-LMの
    # 両方)を同時に動かしている場合、_build_chat_candidateは`loaded`の先頭
    # (loaded[0])をそのメンバーの代表モデルとして使う。そのため、ここでの並び順が
    # そのまま「同一メンバー内でどのバックエンドのモデルが選ばれるか」を決めて
    # しまう。stream_chat_completion()のバックエンド振り分け優先順位
    # (Ollama→MLX-LM→LM Studio)と一致させないと、「ドキュメント上はMLX-LMが
    # 優先されるはずなのに、実際にはLM Studio側のモデルが選ばれる」という
    # 不整合が起きる(実機の検証で実際に発生した)。そのため、両方の優先順位を
    # 揃えてOllama→MLX-LM→LM Studioの順でマージする。
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
            "installed": _merge_model_lists(ollama_installed, mlx_lm_models, lmstudio_models),
            "loaded": _merge_model_lists(ollama_loaded, mlx_lm_models, lmstudio_models),
            "backends": backends,
            # 仮の判断(対話プロトコル用): 寄合の対話プロトコルが役割
            # (提案役・反論役・統合役)を動的に割り振る際の「得意分野」
            # 情報。厳密な専門分野推定は大きなテーマになるため、既存の
            # コーディング系モデル判定(_is_coding_model)を流用した
            # 簡易な二値分類にとどめる。
            "specialties": _infer_specialties(
                _merge_model_lists(ollama_installed, mlx_lm_models, lmstudio_models)
                + _merge_model_lists(ollama_loaded, mlx_lm_models, lmstudio_models)
            ),
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
            for event in stream_chat_completion(model, messages, extra_tools=extra_tools, client_tool_names=client_tool_names):
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


# ---------------------------------------------------------------------------
# 対話モード(フロント): 常駐エージェント(キッチン)への問い合わせ専用クライアント
#
# 仮の判断: 当初は「対話モードも1つのエージェントとして自分でmDNS/HTTPサーバーを
# 起動する」設計だったが、常駐化(systemd/launchd)している状態で手動で
# `python3 yoriai.py` すると同じポートへのbindが衝突する問題が実機で見つかった。
# カフェの「キッチン(常駐エージェント。自己紹介カードサーバー・mDNS/Tailscale探索・
# /chatの提供を担う)」と「フロント(対話モードのUI。注文=質問を取り次ぐだけ)」を
# 分けるように設計し直した。対話モードは自分ではポートを一切bindせず、mDNS/Tailscale
# 探索も行わない。常に既に起動しているキッチン(常駐エージェント)の`/status`と
# `/chat`にHTTPで問い合わせるだけのクライアントにした。これにより、対話モードは
# 常駐サービスを止めずに何度でも起動でき、複数の対話モードを同時に開くことすらできる。
# ---------------------------------------------------------------------------

def _fetch_org_snapshot(port: int, org_fingerprint: str, fail_fast: bool = False):
    """キッチン(常駐エージェント)の`/status`に問い合わせ、自分自身のカードと
    ピア一覧を取得する。接続できない場合はNoneを返す
    (fail_fast=Trueの場合は案内を表示してプロセスごと終了する)。
    """
    try:
        resp = requests.get(
            f"http://localhost:{port}/status",
            headers={ORG_FINGERPRINT_HEADER: org_fingerprint},
            timeout=CARD_REQUEST_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print("実行中のYoriaiエージェントに接続できませんでした。")
        print(f"詳細: {exc}")
        print("先に python3 yoriai.py でエージェント(常駐プロセス)を起動してください")
        print("(常駐化している場合は、想定しているポートで動作しているか確認してください)。")
        if fail_fast:
            sys.exit(1)
        return None


# ---------------------------------------------------------------------------
# タスクの性質に応じた振り分け(フェーズ6・項目1)
# ---------------------------------------------------------------------------

TASK_TYPE_CODING = "coding"
TASK_TYPE_GENERAL = "general"

# 仮の判断: 依頼の要件通り、モデルによる高度な分類ではなく単純なキーワード
# マッチによる簡易分類にとどめる。コードブロック(```や`...`)を含む場合、
# または以下のいずれかの単語(日本語・英語)を含む場合はコーディング系とみなす。
_CODE_BLOCK_PATTERN = re.compile(r"```|`[^`\n]+`")
_CODING_TASK_KEYWORDS = (
    "コード", "コーディング", "関数", "エラー", "バグ", "実装", "プログラム",
    "スクリプト", "クラス", "デバッグ", "リファクタ", "アルゴリズム", "コンパイル",
    "code", "coding", "function", "error", "bug", "debug", "script", "algorithm",
    "traceback", "stack trace", "exception", "compile", "refactor",
)


def _classify_task(text: str) -> str:
    """ユーザーの質問文を簡易的に「コーディング系」か「一般」かに分類する。

    仮の判断: タスクの難易度・専門性を厳密に判定するのは今回のスコープ外
    なので、コーディング系かどうかの二値分類のみを行う。
    """
    if _CODE_BLOCK_PATTERN.search(text):
        return TASK_TYPE_CODING
    lowered = text.lower()
    for keyword in _CODING_TASK_KEYWORDS:
        if keyword.lower() in lowered:
            return TASK_TYPE_CODING
    return TASK_TYPE_GENERAL


# 仮の判断: コーディング系モデルはほぼ例外なく名前に"coder"を含む
# (qwen2.5-coder、qwen3-coder、deepseek-coder、opencoderなど)。
# その他の代表的な派生名も併せて拾う。
_CODING_MODEL_KEYWORDS = ("coder", "codellama", "starcoder", "codegemma", "codestral")


def _is_coding_model(model_name: str) -> bool:
    lowered = model_name.lower()
    return any(keyword in lowered for keyword in _CODING_MODEL_KEYWORDS)


# 対話プロトコルの役割割り振りが参照する「得意分野(ファーストエッグ情報)」。
# 仮の判断: 既存のコーディング系モデル判定をそのまま流用した二値分類に
# とどめる(厳密な専門分野推定は今回のスコープ外)。
DIALOGUE_SPECIALTY_CODING = "coding"
DIALOGUE_SPECIALTY_GENERAL = "general"


def _infer_specialties(model_names) -> list:
    """インストール済み/ロード済みのモデル名一覧から、そのノードの
    得意分野を簡易的に推定し、自己紹介カードに載せるリストを返す。
    """
    if any(_is_coding_model(name) for name in model_names):
        return [DIALOGUE_SPECIALTY_CODING]
    return [DIALOGUE_SPECIALTY_GENERAL]


def _build_chat_candidate(card: dict, is_self: bool, address: str, port: int, task_type: str = TASK_TYPE_GENERAL):
    """自己紹介カードから、チャットの問い合わせ先候補(ロード済みモデルを
    持つメンバー)を作る。ロード済みモデルが無いメンバーはNoneを返す。

    仮の判断: task_typeが「コーディング系」で、かつロード済みモデルの中に
    コーディング系モデルがあれば、そのモデルを候補の代表モデルとして使う
    (先頭のモデルではなく)。それ以外は従来通り先頭のモデルを使う。
    """
    loaded = card.get("models", {}).get("loaded", [])
    if not loaded:
        return None
    label = card.get("device_name", "unknown")
    if is_self:
        label += "(自分)"

    coding_models = [m for m in loaded if _is_coding_model(m)]
    has_coding_model = bool(coding_models)
    if task_type == TASK_TYPE_CODING and has_coding_model:
        model = coding_models[0]
    else:
        # 仮の判断: ロード済みモデルが複数ある場合にどれを使うべきかの判断基準が
        # 他に無いため、単純に先頭のものを使う。
        model = loaded[0]

    return {
        "label": label,
        "model": model,
        "free_gb": card.get("memory", {}).get("free_gb"),
        "address": address,
        "port": port,
        "has_coding_model": has_coding_model,
        "specialties": card.get("models", {}).get("specialties") or _infer_specialties(loaded),
    }


def _select_chat_candidates(self_card: dict, peers: list, local_port: int, task_type: str = TASK_TYPE_GENERAL) -> list:
    """組織内から問い合わせ候補を集め、優先順位をつけて並べて返す。

    仮の判断: 「タスクの難易度に応じた賢い振り分け」は依頼の項目1で
    コーディング系/一般の二値分類のみサポートする。一般タスクはこれまで
    通り「ロード済みモデルがあり、空きメモリが最も多いメンバー」を選ぶ。
    コーディング系タスクは、コーディング系モデルを持つメンバーを優先し
    (その中では空きメモリの多い順)、コーディング系モデルを持つメンバーが
    誰もいない場合は一般タスクと同じ並び(空きメモリ順のみ)に自然と一致する。
    空きメモリが不明なメンバーは最下位として扱う。自分自身はキッチン
    (常駐エージェント)がlocalhost:local_portで`/chat`を提供している
    前提でアドレスを組み立てる。
    """
    candidates = []
    self_candidate = _build_chat_candidate(self_card, is_self=True, address="localhost", port=local_port, task_type=task_type)
    if self_candidate:
        candidates.append(self_candidate)
    for peer in peers:
        candidate = _build_chat_candidate(
            peer.get("card", {}), is_self=False, address=peer.get("address"), port=peer.get("port"), task_type=task_type,
        )
        if candidate:
            candidates.append(candidate)

    if task_type == TASK_TYPE_CODING:
        candidates.sort(
            key=lambda c: (c["has_coding_model"], c["free_gb"] if c["free_gb"] is not None else -1),
            reverse=True,
        )
    else:
        candidates.sort(key=lambda c: c["free_gb"] if c["free_gb"] is not None else -1, reverse=True)
    return candidates


def _stream_chat_from_candidate(
    candidate: dict, org_fingerprint: str, messages: list,
    offer_read_file_tool: bool = False, offer_project_tools: bool = False,
):
    """候補(自分自身を含む)のキッチンにHTTP経由(/chat)で問い合わせ、
    正規化されたストリーミングイベントを順にyieldする。

    `offer_read_file_tool=True`を渡すと、相手のキッチンはこのリクエスト
    限定でread_fileツールをオファーする。`offer_project_tools=True`を
    渡すと、read_fileに加えてファイル作成・移動・削除・ディレクトリ作成・
    一覧表示・テスト実行のツール一式をオファーする(既存プロジェクトへの
    修正依頼専用、より広い権限のツール束)。どちらも実行結果は相手の
    キッチンではなく呼び出し元がこの応答ストリームを見て自分で作る必要が
    ある(詳細はREAD_FILE_TOOL_NAME定義コメントを参照)。
    """
    body = {"model": candidate["model"], "messages": messages}
    if offer_project_tools:
        body["offer_project_tools"] = True
    elif offer_read_file_tool:
        body["offer_read_file_tool"] = True
    try:
        resp = requests.post(
            f"http://{candidate['address']}:{candidate['port']}/chat",
            json=body,
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


def _selection_reason_label(task_type: str, top_candidate: dict) -> str:
    """先頭候補が選ばれた理由を、画面表示用の短いラベルにする。"""
    if task_type == TASK_TYPE_CODING and top_candidate["has_coding_model"]:
        return "コーディング系の質問と判断し、コーディング系モデルを優先"
    if task_type == TASK_TYPE_CODING:
        return "コーディング系の質問と判断しましたが、コーディング系モデルを持つメンバーがいないため空きメモリの多さで選択"
    return "空きメモリの多さで選択"


def _ask_organization(port: int, org_fingerprint: str, messages: list) -> None:
    """自分のキッチン(常駐エージェント)の`/status`で組織内の候補を集め、順番に
    問い合わせて、失敗したら次に空きメモリが多い候補へ自動でフォールバックしながら
    回答をストリーミング表示する。

    成功した場合はassistantの回答を`messages`に追記する(会話履歴の継続)。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return  # 案内メッセージは_fetch_org_snapshot内で表示済み

    latest_question = messages[-1]["content"] if messages else ""
    task_type = _classify_task(latest_question)
    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, task_type)

    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    for i, candidate in enumerate(candidates):
        if i == 0:
            print(f"[{_selection_reason_label(task_type, candidate)}: {candidate['label']} に問い合わせています... (モデル: {candidate['model']})]")
        else:
            print(f"[フォールバック: {candidate['label']} に問い合わせています... (モデル: {candidate['model']})]")

        answer_parts = []
        failed = False
        for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages):
            if "error" in event:
                logger.warning("%s への問い合わせに失敗しました: %s", candidate["label"], event["error"])
                failed = True
                break
            if event.get("tool_call_failed"):
                print("\n[⚠️ このモデルはツール呼び出しの構造化出力に対応していないようです。ツールなしで再試行しています...]")
                continue
            tool_call_name = event.get("tool_call")
            if tool_call_name == WEB_SEARCH_TOOL_NAME:
                print("\n[🔍 ウェブ検索しています...]")
                continue
            elif tool_call_name:
                print(f"\n[🔧 {tool_call_name} を実行しています...]")
                continue
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
# 複数メンバーへの並列質問(フェーズ6・項目2、"//multi"コマンド)
# ---------------------------------------------------------------------------

MULTI_QUERY_COMMAND = "//multi"
# 仮の判断: 依頼文の「空きリソース上位2〜3台」という表現の範囲内で、
# 上限を3台に固定する(候補がそれより少ない場合は、いる分だけに送る)。
MULTI_QUERY_TARGET_COUNT = 3


def _parse_tool_call_arguments(tool_call_arguments) -> dict:
    """tool_callの引数(dictまたはJSON文字列)を辞書として返す。
    取り出せない場合は空の辞書を返すだけで例外は投げない
    (read_file・search_in_file共通で使う引数パース処理)。
    """
    if isinstance(tool_call_arguments, str):
        try:
            tool_call_arguments = json.loads(tool_call_arguments) if tool_call_arguments else {}
        except json.JSONDecodeError:
            return {}
    return tool_call_arguments if isinstance(tool_call_arguments, dict) else {}


def _extract_read_file_tool_filename(tool_call_arguments) -> str:
    """read_file/search_in_fileツールのtool_call引数からfilenameを取り出す。
    画面表示専用の用途のため、取り出せない場合は空文字列を返す。
    """
    return _parse_tool_call_arguments(tool_call_arguments).get("filename", "")


def _collect_answer_from_candidate(candidate: dict, org_fingerprint: str, messages: list):
    """1候補に問い合わせ、回答をすべて集めて文字列として返す
    (content, error, truncated)のタプル。エラー時はcontentが空文字列に
    なる。`truncated`は、応答がCHAT_MAX_OUTPUT_TOKENS上限に達して途中で
    打ち切られたかどうかを表す(この場合`content`には打ち切られた時点
    までの不完全な内容が入っている)。

    仮の判断: //multi では複数候補への問い合わせを並列に行うため、通常モード
    のように1文字ずつその場で画面に出す(ライブストリーミング)方式のままだと
    複数候補の出力が入り交じって読めなくなってしまう。そのため、この関数は
    ストリーミング自体はバックグラウンドで最後まで受信しきり、完成した
    回答をまとめて呼び出し元に返す(表示は呼び出し元が候補ごとに区切って行う)。

    仮の判断(マージ時の整理): 以前はレビュー専用のavailable_files引数と、
    read_fileのtool_call進捗をその場で表示するロジックをこの関数が
    持っていたが、read_fileの実行方式を「実装依頼元がその場でディスクを
    読み直す」設計に変更したことに伴い、その進捗表示は
    `_collect_review_answer_with_read_file`側の責務に移った。この関数は
    「1候補に問い合わせて回答を集めるだけ」という元のシンプルな役割に戻し、
    印字は一切行わない(呼び出し元が候補ごとに区切って表示する)。
    """
    answer_parts = []
    error = None
    truncated = False
    for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages):
        if "error" in event:
            error = event["error"]
            break
        content = event.get("content")
        if content:
            answer_parts.append(content)
        if event.get("done"):
            truncated = bool(event.get("truncated"))
            break
    return "".join(answer_parts), error, truncated


# 仮の判断: 複雑な依頼(HTML/CSSの学習サイト等)では、1ファイルの中身が
# 数万文字に達することがあり、read_fileでその内容をそのまま返すと、1回の
# レビュー・修正セッションの中で複数ファイルを読むだけで会話全体の
# トークン使用量が大きく膨らむことが実機の報告で判明した。/chatは毎回
# それまでのやり取り全体を送信し直すステートレスな設計のため、読み込んだ
# ファイルが大きいほど、以降の各ラウンドの送信量にそのまま積み上がる。
# 内容を丸ごと握りつぶすと判断材料が失われてしまうため、削除ではなく
# 「先頭からこの文字数までに切り詰め、切り詰めたことを明示する」形で
# 上限を設ける。読みたい範囲を絞り込む機能(read_fileへのオフセット指定
# 等)までは今回のスコープ外とし、まずは無条件の上限による歯止めのみを
# 入れた。
_FILE_CONTENT_TRUNCATE_CHARS = 12000


def _truncate_file_content(content: str, limit: int = _FILE_CONTENT_TRUNCATE_CHARS) -> tuple:
    """`(切り詰め後の内容, 切り詰めたかどうか)`を返す。"""
    if len(content) <= limit:
        return content, False
    return content[:limit], True


def _planned_filenames_from_progress(out_dir: str) -> set:
    """`out_dir`(プロジェクトディレクトリ)のPROGRESS.mdを読み直し、
    モジュール分割案に含まれるファイル名の集合を返す。読み取れない場合
    (PROGRESS.mdが存在しない・想定形式で解析できない)は空集合を返す
    (呼び出し元は、この場合は既存の汎用メッセージにフォールバックする)。

    仮の判断(バグ報告への対応): read_fileで指定されたファイルが存在
    しない場合、それが「まだ実装されていないだけの、計画通りのファイル」
    なのか、「そもそも計画に無い架空のファイル名」なのかを区別できる
    ようにするため、PROGRESS.mdを毎回読み直す(read_fileの既存の
    「ディスク上の実際の状態を正として扱う」設計方針と同じ)。
    """
    parsed = _parse_progress_markdown(os.path.join(out_dir, PROGRESS_FILENAME))
    if parsed is None:
        return set()
    return {fn for fn, _content in parsed["tasks"]}


# 仮の判断(部分読み機能の追加): start_line/end_lineを指定した部分読みでも、
# 極端に広い範囲(例: 1〜100000行目)を指定されて丸ごと返してしまうと、
# 文字数上限による切り詰めと同じ問題が再発する。行数ベースでも上限を
# 設け、それを超える場合は開始行からこの行数分までに切り詰める。
_READ_FILE_MAX_LINES_PER_CALL = 400
# `line`(中心行)指定時、context_linesを省略した場合のデフォルトの前後行数。
_READ_FILE_DEFAULT_CONTEXT_LINES = 20


def _read_project_file_fresh(
    out_dir: str, filename: str,
    start_line: int = None, end_line: int = None,
    line: int = None, context_lines: int = None,
) -> str:
    """read_fileツールの応答本体。呼び出された「その瞬間」に`out_dir`配下を
    ディスクから直接読み直して結果を作る(依頼発行時や前回のラウンド時点の
    古いスナップショットを使い回さない)。

    仮の判断: `filename`はモデルが自由な文字列として指定してくる値のため、
    `_resolve_safe_project_path`(write_file等と共通)で`out_dir`配下の
    相対パスとしてのみ解決する(`../../etc/passwd`のようなパストラバーサルで
    プロジェクト外のファイルを読ませられないようにする安全対策)。

    仮の判断(バグ報告への対応): 以前は`os.path.basename`でディレクトリ
    部分を一律取り除いていたため、write_fileでサブディレクトリ内に
    書き込んだファイル(例: templates/base.html)を、その後read_fileで
    読み返すことができなかった。書き込み側と同じ`_resolve_safe_project_
    path`を使うことで、サブディレクトリ内のファイルも読めるようにした。

    仮の判断(部分読み機能の追加): `start_line`/`end_line`(またはその代わりに
    `line`+`context_lines`)がすべて省略された場合は、従来通りファイル全体を
    返す(既存の呼び出し元・既存のプロンプトとの後方互換性を保つ)。いずれか
    が指定された場合は、その範囲だけを行番号付きで返す(search_in_fileの
    出力と同じ「行番号: 内容」形式にして、両ツールの結果を突き合わせやすく
    している)。
    """
    path, path_error = _resolve_safe_project_path(out_dir, filename)
    if path_error or not os.path.isfile(path):
        # 仮の判断: 依頼の項目4に対応。存在しないファイル名を指定された
        # 場合は、例外を投げたりエラー扱いにしたりせず、「まだ実装されて
        # いない」という情報自体をモデルへの正常な回答として返す。
        #
        # 仮の判断(バグ報告への対応): さらに、そのファイル名がそもそも
        # PROGRESS.mdの計画に含まれているかを確認する。計画に無い名前で
        # あれば、「未実装」ではなく「計画そのものに存在しない」ことを
        # 明示し、計画に実在するファイル一覧を提示する。これにより、
        # 設計担当の記述ミス(実際には無いファイルへの言及)が原因で
        # レビュー往復・自動再開が無限に解決しないという事態を防ぐ
        # (架空のファイル名を待ち続けるのではなく、モデル自身がその場で
        # 「参照すべきは実在するどのファイルか」を判断し直せるようにする)。
        planned = _planned_filenames_from_progress(out_dir)
        if planned and os.path.basename(filename or "") not in planned:
            return json.dumps({
                "filename": filename, "exists": False,
                "message": (
                    f"'{filename}' は実装計画(PROGRESS.mdのモジュール分割案)に"
                    "含まれていないファイル名です。新規に作成するつもりであれば"
                    "write_fileを使ってください。そうでなければ、計画の記述ミスの"
                    "可能性があります。計画に実在するファイル: "
                    f"{', '.join(sorted(planned))}"
                ),
            }, ensure_ascii=False)
        return json.dumps({
            "filename": filename, "exists": False,
            "message": "そのファイルはまだ存在しません(未実装です)。",
        }, ensure_ascii=False)

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as exc:
        return json.dumps({"filename": filename, "exists": False, "message": f"読み取りに失敗しました: {exc}"}, ensure_ascii=False)

    if start_line is None and end_line is None and line is None:
        truncated_content, was_truncated = _truncate_file_content(content)
        result = {"filename": filename, "exists": True, "content": truncated_content}
        if was_truncated:
            result["truncated"] = True
            result["message"] = (
                f"ファイルサイズが大きいため、先頭{_FILE_CONTENT_TRUNCATE_CHARS}文字のみを返しています"
                "(トークン使用量を抑えるための制限です)。"
            )
        return json.dumps(result, ensure_ascii=False)

    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return json.dumps(
            {"filename": filename, "exists": True, "start_line": 0, "end_line": 0, "total_lines": 0, "content": ""},
            ensure_ascii=False,
        )

    if start_line is None and end_line is None and line is not None:
        span = context_lines if context_lines and context_lines > 0 else _READ_FILE_DEFAULT_CONTEXT_LINES
        start_line, end_line = line - span, line + span

    resolved_start = start_line if start_line and start_line > 0 else 1
    resolved_end = end_line if end_line and end_line > 0 else total_lines
    resolved_start = max(1, min(resolved_start, total_lines))
    resolved_end = max(resolved_start, min(resolved_end, total_lines))

    range_was_capped = False
    if resolved_end - resolved_start + 1 > _READ_FILE_MAX_LINES_PER_CALL:
        resolved_end = resolved_start + _READ_FILE_MAX_LINES_PER_CALL - 1
        range_was_capped = True

    excerpt = "\n".join(f"{n}: {lines[n - 1]}" for n in range(resolved_start, resolved_end + 1))
    result = {
        "filename": filename, "exists": True,
        "start_line": resolved_start, "end_line": resolved_end, "total_lines": total_lines,
        "content": excerpt,
    }
    if range_was_capped:
        result["message"] = (
            f"指定範囲が広すぎるため、{resolved_start}〜{resolved_end}行目までを返しています"
            f"(1回あたり最大{_READ_FILE_MAX_LINES_PER_CALL}行までの制限です)。"
        )
    return json.dumps(result, ensure_ascii=False)


_SEARCH_IN_FILE_DEFAULT_CONTEXT_LINES = 5
# 仮の判断: 一致件数が多いキーワード(短い変数名等)を指定された場合に
# 抜粋が際限なく膨らまないよう、返す一致件数自体にも上限を設ける。
_SEARCH_IN_FILE_MAX_MATCHES = 20


def _search_in_project_file(out_dir: str, filename: str, query: str, context_lines: int = None) -> str:
    """search_in_fileツールの応答本体。`_read_project_file_fresh`と同様、
    呼び出された瞬間に`out_dir`配下をディスクから直接読み直す。

    大文字小文字を区別しない単純な部分一致検索で、一致した行番号の一覧と、
    各一致箇所の前後`context_lines`行(省略時は
    `_SEARCH_IN_FILE_DEFAULT_CONTEXT_LINES`行)の抜粋を返す。ファイル全体を
    read_fileで読む前に、まずここで見当をつけることを想定している。

    仮の判断(バグ報告への対応): `_read_project_file_fresh`と同じ理由で、
    `os.path.basename`によるディレクトリ部分の除去ではなく
    `_resolve_safe_project_path`を使い、サブディレクトリ内のファイルも
    検索できるようにする。
    """
    path, path_error = _resolve_safe_project_path(out_dir, filename)
    if path_error or not os.path.isfile(path):
        planned = _planned_filenames_from_progress(out_dir)
        if planned and os.path.basename(filename or "") not in planned:
            return json.dumps({
                "filename": filename, "exists": False,
                "message": (
                    f"'{filename}' は実装計画(PROGRESS.mdのモジュール分割案)に"
                    "含まれていないファイル名です。計画に実在するファイル: "
                    f"{', '.join(sorted(planned))}"
                ),
            }, ensure_ascii=False)
        return json.dumps({
            "filename": filename, "exists": False,
            "message": "そのファイルはまだ存在しません(未実装です)。",
        }, ensure_ascii=False)

    query = (query or "").strip()
    if not query:
        return json.dumps(
            {"filename": filename, "exists": True, "message": "検索キーワードが指定されていません。"},
            ensure_ascii=False,
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception as exc:
        return json.dumps({"filename": filename, "exists": False, "message": f"読み取りに失敗しました: {exc}"}, ensure_ascii=False)

    query_lower = query.lower()
    matched_line_numbers = [n for n, text in enumerate(lines, start=1) if query_lower in text.lower()]
    if not matched_line_numbers:
        return json.dumps({
            "filename": filename, "exists": True, "query": query, "match_count": 0,
            "message": f"'{query}' に一致する行は見つかりませんでした。",
        }, ensure_ascii=False)

    span = context_lines if context_lines and context_lines > 0 else _SEARCH_IN_FILE_DEFAULT_CONTEXT_LINES
    reported_line_numbers = matched_line_numbers[:_SEARCH_IN_FILE_MAX_MATCHES]
    excerpts = []
    for line_num in reported_line_numbers:
        start = max(1, line_num - span)
        end = min(len(lines), line_num + span)
        excerpt = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        excerpts.append({"line": line_num, "excerpt": excerpt})

    result = {
        "filename": filename, "exists": True, "query": query,
        "match_count": len(matched_line_numbers),
        "matched_lines": matched_line_numbers,
        "excerpts": excerpts,
    }
    if len(matched_line_numbers) > _SEARCH_IN_FILE_MAX_MATCHES:
        result["message"] = (
            f"一致件数が多いため、最初の{_SEARCH_IN_FILE_MAX_MATCHES}件のみ抜粋しています。"
            "より具体的なキーワードで絞り込んでください。"
        )
    return json.dumps(result, ensure_ascii=False)


def _collect_review_answer_with_read_file(
    candidate: dict, org_fingerprint: str, messages: list, out_dir: str,
    print_lock: threading.Lock = None, tag: str = None,
):
    """レビュー専用: read_file/search_in_fileツールの呼び出しには、実装依頼元
    である自分自身がその場で`out_dir`を読み直して応答し、会話を継続する
    (candidateのキッチンプロセスにはこれらのツールを実行させない)。
    (content, error, truncated)のタプルを返す。`truncated`は最終回答が
    CHAT_MAX_OUTPUT_TOKENS上限に達して途中で打ち切られたかどうかを表す。

    仮の判断: /chatはメッセージ履歴(messages)を毎回丸ごと送るステート
    レスな設計のため、「ツール実行結果を会話に追加した新しいmessagesで
    もう一度/chatを呼ぶ」だけで、モデルからは1つの連続した会話として
    見える。この呼び出しごとにout_dirを読み直す(_read_project_file_fresh・
    _search_in_project_file)ため、前回のラウンド以降に他のワーカースレッドが
    完成させたファイルも正しく反映される。

    `print_lock`/`tag`(タスクキュー方式専用)を渡すと、ツール呼び出しの
    進捗表示も`_print_tagged`でタグ付き・ロック保護付きに出力され、他の
    ワーカースレッドの出力と混ざらない。渡さない場合はタグとして
    candidateのラベルを使い、ロックなしで出力する。

    仮の判断(search_in_file追加): read_fileとsearch_in_fileは「他ファイルを
    参照するための下調べ」という同じ目的のツールのため、往復回数の上限
    (MAX_READ_FILE_CALLS_PER_REVIEW)は両者で共有する。search_in_fileで
    見当をつけてからread_fileで絞った範囲だけを読む、という2段階の使い方を
    しても呼び出し回数の枠を圧迫しないよう、上限自体は変えていない。
    """
    messages = list(messages)
    file_lookup_calls_used = 0

    # 仮の判断: read_file/search_in_fileの実行が許されるのは最大
    # MAX_READ_FILE_CALLS_PER_REVIEW回だが、それを使い切った後に「上限に
    # 達しました」という通知を送ったラウンドの次にも、モデルが最終回答を
    # 返すための1往復が必要になる。そのため往復の総ラウンド数には+2の
    # 余裕を持たせる(上限到達の通知を受け取ってさらにもう一度呼び出しを
    # 試みても弾かれるだけなので、実質的にはこの範囲で必ず終息する)。
    for _round_num in range(MAX_READ_FILE_CALLS_PER_REVIEW + 2):
        answer_parts = []
        pending_tool_calls = None
        error = None
        truncated = False
        for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages, offer_read_file_tool=True):
            if "error" in event:
                error = event["error"]
                break
            if "pending_tool_calls" in event:
                pending_tool_calls = event["pending_tool_calls"]
                break
            content = event.get("content")
            if content:
                answer_parts.append(content)
            if event.get("done"):
                truncated = bool(event.get("truncated"))
                break

        if error:
            return "", error, False
        if not pending_tool_calls:
            return "".join(answer_parts), None, truncated

        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
        for tool_call in pending_tool_calls:
            tool_name = tool_call.get("function", {}).get("name", "")
            arguments = tool_call.get("function", {}).get("arguments")
            if tool_name in (READ_FILE_TOOL_NAME, SEARCH_IN_FILE_TOOL_NAME) and file_lookup_calls_used < MAX_READ_FILE_CALLS_PER_REVIEW:
                file_lookup_calls_used += 1
                parsed_args = _parse_tool_call_arguments(arguments)
                filename = parsed_args.get("filename", "")
                if tool_name == SEARCH_IN_FILE_TOOL_NAME:
                    query = parsed_args.get("query", "")
                    _print_tagged(
                        print_lock, tag or candidate["label"],
                        f"[🔍 {candidate['label']} が {filename or '他のファイル'} 内で '{query}' を検索しています...]",
                    )
                    result = _search_in_project_file(out_dir, filename, query, parsed_args.get("context_lines"))
                else:
                    _print_tagged(
                        print_lock, tag or candidate["label"],
                        f"[📖 {candidate['label']} が {filename or '他のファイル'} を読みに行っています...]",
                    )
                    result = _read_project_file_fresh(
                        out_dir, filename,
                        parsed_args.get("start_line"), parsed_args.get("end_line"),
                        parsed_args.get("line"), parsed_args.get("context_lines"),
                    )
            else:
                result = json.dumps({
                    "error": (
                        f"ファイル読み取りツールの呼び出し回数が上限({MAX_READ_FILE_CALLS_PER_REVIEW}回)"
                        "に達しました。これまでに確認した内容をもとに判断してください。"
                    ),
                }, ensure_ascii=False)
            messages.append({"role": "tool", "content": result, "tool_call_id": tool_call.get("id")})

    # 仮の判断: 上限ラウンドに達してもモデルがまだread_fileを呼び続けようと
    # した場合、暴走防止のためここで打ち切る。この経路に到達した場合、
    # _review_one_file側は「問い合わせに失敗した」扱いとして「問題あり」に
    # 倒す(安全側)。
    return "", f"read_fileの往復回数が上限({MAX_READ_FILE_CALLS_PER_REVIEW}回)に達しました", False


# ---------------------------------------------------------------------------
# プロジェクトファイル操作ツール(既存プロジェクトへの修正依頼専用)
# ---------------------------------------------------------------------------
#
# 仮の判断: read_fileと同じ「実行はモデルのキッチンプロセスではなく、
# プロジェクトの実ファイルにアクセスできる呼び出し元が行う
# (client_tool_names)」という設計をそのまま踏襲し、ファイル作成・移動・
# 削除・ディレクトリ作成・一覧表示・テスト実行を追加する。Claude Codeの
# Bashツールのような「任意のシェルコマンドを実行できる」形は避け、用途を
# 絞った専用ツールのみを提供する。

def _resolve_safe_project_path(project_dir: str, filename: str) -> tuple:
    """`(絶対パス, エラーメッセージ)`を返す。`project_dir`配下(サブ
    ディレクトリを含む)の相対パスだけを許可する(依頼の項目2・5:
    パストラバーサル対策と、Yoriai本体・他プロジェクトへのアクセスを
    絶対に許さないための唯一の入口)。この関数を経由しない限り以下の
    ツールはどれも実際のファイルパスを作らないため、モデルからの入力が
    どのような文字列であってもproject_dirの外に出ることはない。

    仮の判断(バグ報告への対応): 当初はディレクトリ区切りを含む名前を
    一律拒否し、project_dir直下のフラットなファイルのみ許可していた。
    しかしmake_directoryでサブディレクトリ自体は作成できるにもかかわらず、
    write_fileではその中に一切書き込めないという矛盾した制約になって
    いた。実機で、モデルが「templates/base.html」のようなサブディレクトリ
    内のパスをfilenameに指定し続けて書き込みが常に拒否され、結果として
    templates・lessons等のディレクトリだけが空のまま残る不具合が報告
    された。パストラバーサル対策(絶対パス・'..'による親ディレクトリ参照の
    拒否)は維持しつつ、project_dir配下のサブディレクトリの利用を許可する
    形に緩和した(サブディレクトリが実在しなくても、書き込み側
    (`_write_project_file`・`_move_project_file`)が自動的に作成する)。

    仮の判断: PROGRESS.mdはYoriai自身が状態管理に使うファイルであり、
    モデルが自由に書き換えたり消したりできてしまうと、進行状況の
    永続化・巡回モード・自動再開の前提が壊れる。そのため(どのサブ
    ディレクトリに置かれていても)名前(最後の要素)で明示的に弾く。
    """
    name = (filename or "").strip().replace("\\", "/")
    if not name:
        return None, "ファイル名が指定されていません。"
    if name.startswith("/"):
        return None, f"'{name}' は絶対パスのため使用できません(プロジェクト直下、またはそのサブディレクトリ内の相対パスを指定してください)。"
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None, f"'{name}' は使用できないパスです(プロジェクトディレクトリの外を指す'..'は使用できません)。"
    if parts[-1] == PROGRESS_FILENAME:
        return None, "PROGRESS.mdはYoriai自身が管理するファイルのため、このツールでは操作できません。"
    return os.path.join(project_dir, *parts), None


def _project_relpath(project_dir: str, path: str) -> str:
    """`path`(`_resolve_safe_project_path`が返した絶対パス)を、
    `project_dir`からの相対パス(区切りは常にスラッシュ)として返す。
    モデルへの応答(書き込み・移動・削除・一覧の結果)で、サブディレクトリ
    内のファイルがどこに置かれたかを正確に伝えるために使う。
    """
    return os.path.relpath(path, project_dir).replace(os.sep, "/")


LIST_DIR_TOOL_NAME = "list_dir"
LIST_DIR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": LIST_DIR_TOOL_NAME,
        "description": (
            "このプロジェクトディレクトリ内にあるファイルの一覧を取得する"
            "(サブディレクトリ内のファイルも'サブディレクトリ名/ファイル名'の形で含まれる)。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

WRITE_FILE_TOOL_NAME = "write_file"
WRITE_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": WRITE_FILE_TOOL_NAME,
        "description": (
            "このプロジェクトディレクトリ内に、指定した内容でファイルを新規作成・上書きする。"
            "'サブディレクトリ名/ファイル名'のようにサブディレクトリ内のパスを指定することもでき、"
            "そのサブディレクトリがまだ存在しなければ自動的に作成される"
            "(make_directoryを先に呼ぶ必要はない)。"
            "既存ファイルの一部だけを直したい場合も、read_fileで現在の中身を確認したうえで"
            "ファイル全体の新しい内容をここに渡すこと(差分ではなく全体を渡す)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "書き込むファイル名(例: utils.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "content": {"type": "string", "description": "ファイルの新しい中身(ファイル全体)"},
            },
            "required": ["filename", "content"],
        },
    },
}

MOVE_FILE_TOOL_NAME = "move_file"
MOVE_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MOVE_FILE_TOOL_NAME,
        "description": (
            "このプロジェクトディレクトリ内のファイルの名前を変更(移動)する。"
            "サブディレクトリ内へ、またはサブディレクトリ間で移動することもできる"
            "('サブディレクトリ名/ファイル名'の形で指定する)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "old_filename": {"type": "string", "description": "変更前のファイル名(サブディレクトリ内のパスも可)"},
                "new_filename": {"type": "string", "description": "変更後のファイル名(サブディレクトリ内のパスも可)"},
            },
            "required": ["old_filename", "new_filename"],
        },
    },
}

DELETE_FILE_TOOL_NAME = "delete_file"
DELETE_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": DELETE_FILE_TOOL_NAME,
        "description": "このプロジェクトディレクトリ内のファイルを削除する(サブディレクトリ内のファイルも可)。取り消せない操作なので、本当に不要なファイルにのみ使うこと。",
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": "string", "description": "削除するファイル名(サブディレクトリ内のパスも可)"}},
            "required": ["filename"],
        },
    },
}

MAKE_DIRECTORY_TOOL_NAME = "make_directory"
MAKE_DIRECTORY_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MAKE_DIRECTORY_TOOL_NAME,
        "description": (
            "このプロジェクトディレクトリ直下にサブディレクトリを作成する。"
            "write_fileはサブディレクトリが無くても自動的に作成するため、このツールは"
            "空のディレクトリだけを先に用意しておきたい場合にのみ使えばよい(通常は不要)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"dirname": {"type": "string", "description": "作成するディレクトリ名"}},
            "required": ["dirname"],
        },
    },
}

# 仮の判断: 依頼(「検証専用の、より柔軟なコマンド実行ツールへの刷新」)
# への対応。以前のrun_testは拡張子ごとに決め打ちのコマンドしか実行でき
# ず、対応していない言語・状況(HTML/CSS等)ではモデルが見当違いの
# コマンドを何度も試してツール呼び出し上限を無駄にする不具合が繰り返し
# 報告された。run_commandはコマンド文字列を自由に指定できる代わりに、
# 実行前に機械的なフィルタ(ネットワークアクセス・破壊的操作・権限昇格の
# 拒否、プロジェクトディレクトリへの実行範囲の固定)を必ず通す設計に
# 置き換えた(run_testは廃止し、check_htmlの機能はrun_command経由で
# 引き続き使えるようにした)。Claude CodeのBashツールのような無制限な
# 実行権限とは異なり、あくまで「検証・確認」の範囲内での柔軟性向上が
# 目的であることに注意(詳細は_validate_run_commandのコメントを参照)。
RUN_COMMAND_TOOL_NAME = "run_command"
RUN_COMMAND_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": RUN_COMMAND_TOOL_NAME,
        "description": (
            "プロジェクトディレクトリ内で、ファイルの検証に適したコマンドを自由に実行して動作を"
            "確認する(例: 'python3 app.py'・'node --check app.js'・'gcc -fsyntax-only calc.c'・"
            "'pytest'。HTML/CSSファイルは実機にPlaywrightがあれば"
            "'check_html <HTMLファイル名>' でヘッドレスブラウザによる検証ができる)。"
            "検証専用のツールであり、ファイルの作成・上書き・移動・削除にはwrite_file・"
            "move_file・delete_fileを使うこと(run_commandでは行えない)。"
            "ネットワークアクセス(curl・wget・ssh等)・ファイルの削除や移動(rm・mv・cp等)・"
            "権限昇格(sudo等)・シェル自体の起動(bash・sh等)を行うコマンド、"
            "プロジェクトディレクトリの外を指すパス(絶対パス・'..'を含む相対パス)は"
            "実行前に拒否される。実行時間は10秒、出力は4000文字まで。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "実行するコマンド(例: python3 app.py)"},
            },
            "required": ["command"],
        },
    },
}

# 仮の判断: read_file・search_in_fileも含めて「このプロジェクトディレクトリの
# 中だけを対象にした、用途を絞ったツール」としてまとめて1つの束
# (offer_project_tools)にする。既存のレビューフェーズ(offer_read_file_tool、
# read_file+search_in_fileのみ)とは別の、修正依頼専用のより広い権限のツール束
# として明確に分離する。
PROJECT_TOOLS_SCHEMAS = [
    READ_FILE_TOOL_SCHEMA, SEARCH_IN_FILE_TOOL_SCHEMA, LIST_DIR_TOOL_SCHEMA, WRITE_FILE_TOOL_SCHEMA,
    MOVE_FILE_TOOL_SCHEMA, DELETE_FILE_TOOL_SCHEMA, MAKE_DIRECTORY_TOOL_SCHEMA,
    RUN_COMMAND_TOOL_SCHEMA,
]
PROJECT_TOOLS_CLIENT_NAMES = {
    READ_FILE_TOOL_NAME, SEARCH_IN_FILE_TOOL_NAME, LIST_DIR_TOOL_NAME, WRITE_FILE_TOOL_NAME,
    MOVE_FILE_TOOL_NAME, DELETE_FILE_TOOL_NAME, MAKE_DIRECTORY_TOOL_NAME,
    RUN_COMMAND_TOOL_NAME,
}


def _list_project_directory(project_dir: str) -> str:
    return json.dumps({"files": _list_project_files(project_dir)}, ensure_ascii=False)


def _write_project_file(project_dir: str, filename: str, content) -> str:
    """ファイルを書き込む。書き込み直後に、拡張子に応じた機械的な構文
    チェック(`_check_file_syntax`、言語非依存)を行い、結果をそのまま
    モデルへの返り値に含める(依頼の「構文チェックを通してから」を、
    複数ファイルを自由に操作できるこのツール群では「書いた直後にその場で
    フィードバックし、モデル自身が次のラウンドで直せるようにする」形で
    満たす。最終的な安全網として、_ask_organization_fix_project側でも
    修正完了後にプロジェクト全体の構文チェックを別途行う)。
    """
    safe_path, error = _resolve_safe_project_path(project_dir, filename)
    if error:
        return json.dumps({"ok": False, "message": error}, ensure_ascii=False)
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    try:
        # 仮の判断(バグ報告への対応): filenameがサブディレクトリを含む
        # パス(例: templates/base.html)の場合、そのサブディレクトリが
        # まだ存在しなければここで自動的に作成する。make_directoryを
        # 事前に呼んでいなくてもwrite_fileだけで完結できるようにするため
        # (flatなファイル名の場合、dirnameはproject_dir自身になり、
        # 既に存在するため実質的に何もしない)。
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"書き込みに失敗しました: {exc}"}, ensure_ascii=False)
    result = {"ok": True, "filename": _project_relpath(project_dir, safe_path)}
    status, detail = _check_file_syntax(os.path.basename(safe_path), text, file_path=safe_path)
    if status == "ok":
        result["syntax_ok"] = True
    elif status == "error":
        result["syntax_ok"] = False
        result["syntax_error"] = detail
    else:  # skipped
        result["syntax_check_skipped"] = detail
    return json.dumps(result, ensure_ascii=False)


def _move_project_file(project_dir: str, old_filename: str, new_filename: str) -> str:
    old_path, old_error = _resolve_safe_project_path(project_dir, old_filename)
    if old_error:
        return json.dumps({"ok": False, "message": old_error}, ensure_ascii=False)
    new_path, new_error = _resolve_safe_project_path(project_dir, new_filename)
    if new_error:
        return json.dumps({"ok": False, "message": new_error}, ensure_ascii=False)
    if not os.path.isfile(old_path):
        return json.dumps({"ok": False, "message": f"{old_filename} が見つかりません。"}, ensure_ascii=False)
    try:
        # write_fileと同様、移動先のサブディレクトリが無ければ自動作成する。
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        os.replace(old_path, new_path)
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"移動に失敗しました: {exc}"}, ensure_ascii=False)
    return json.dumps(
        {
            "ok": True,
            "old_filename": _project_relpath(project_dir, old_path),
            "new_filename": _project_relpath(project_dir, new_path),
        },
        ensure_ascii=False,
    )


def _delete_project_file(project_dir: str, filename: str, print_lock: threading.Lock = None, tag: str = None) -> str:
    """ファイルを削除する。依頼の項目4に対応するため、実際に削除する前に
    PROGRESS.mdの更新履歴へ記録し、画面にも明確なログを出す(削除は
    取り消せない操作のため、実行後の記録では遅い)。
    """
    safe_path, error = _resolve_safe_project_path(project_dir, filename)
    if error:
        return json.dumps({"ok": False, "message": error}, ensure_ascii=False)
    if not os.path.isfile(safe_path):
        return json.dumps({"ok": False, "message": f"{filename} が見つかりません。"}, ensure_ascii=False)
    safe_name = _project_relpath(project_dir, safe_path)

    progress_path = os.path.join(project_dir, PROGRESS_FILENAME)
    parsed = _parse_progress_markdown(progress_path)
    if parsed is not None:
        today = datetime.date.today().isoformat()
        changelog = list(parsed["changelog"])
        changelog.append(f"- {today}: {safe_name} を削除")
        _write_progress_md(
            project_dir, parsed["request"], parsed["tasks"], parsed["checklist"],
            review_feedback={}, auto_resume_count=parsed["auto_resume_count"], changelog=changelog,
        )
    _print_tagged(print_lock, tag or "ツール実行", f"[🗑️ {safe_name} を削除します]")

    try:
        os.remove(safe_path)
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"削除に失敗しました: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "filename": safe_name}, ensure_ascii=False)


def _make_project_directory(project_dir: str, dirname: str) -> str:
    safe_path, error = _resolve_safe_project_path(project_dir, dirname)
    if error:
        return json.dumps({"ok": False, "message": error}, ensure_ascii=False)
    try:
        os.makedirs(safe_path, exist_ok=True)
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"ディレクトリの作成に失敗しました: {exc}"}, ensure_ascii=False)
    return json.dumps({"ok": True, "dirname": _project_relpath(project_dir, safe_path)}, ensure_ascii=False)


# 仮の判断: 依頼(「検証専用の、より柔軟なコマンド実行ツールへの刷新」)
# への対応。以前は拡張子ごとに決め打ちのコマンドしか実行できず、対応
# していない言語・状況ではモデルが見当違いのコマンドを何度も試して
# ツール呼び出し上限を無駄にしていた。ここではその逆に、コマンド文字列
# 自体は自由に指定できる代わりに、実行前に機械的なフィルタ(ブロック
# リスト方式)を必ず通す設計にした。
#
# 重要な注意: これはホワイトリスト(許可リスト)ではなくブロックリスト
# (拒否リスト)方式であるため、原理的に「絶対安全」を保証するものでは
# ない(未知の抜け道が存在しうる)。Claude CodeのBashツールのような
# 無制限の実行権限とは異なり、あくまで「検証・確認」の範囲内での柔軟性
# 向上が目的であることを踏まえ、以下の観点で防御している:
# (1) ネットワークアクセス・破壊的なファイル操作(削除・移動・上書き。
#     これらはwrite_file/delete_file/move_fileの役割)・権限昇格・
#     シェル自体の起動を、コマンド名の一致で拒否する。argv[0]だけでなく
#     コマンド文字列内の全トークンをチェックすることで、
#     'env curl ...'や'timeout 5 curl ...'のような、他コマンドを間接的に
#     起動できる「ラッパー」経由の迂回もある程度防ぐ(basenameで比較する
#     ため'/usr/bin/curl'のようなパス付き指定も検出できる)。
# (2) 既知のインタプリタ(python3/node等)がインラインコード実行フラグ
#     (-c/-e/-m等)を通じて任意のコードを実行できてしまう抜け道を、
#     フラグ単位で個別に拒否する。gccの-c(リンクなしコンパイル、
#     'gcc -fsyntax-only'と同様によく使う正当なフラグ)のような無関係な
#     同名フラグを誤って禁止しないよう、コマンドごとに個別のフラグ集合を
#     持つ。
# (3) shell=Trueは使わないため';'・'&&'・'|'・バッククォート・'$()'の
#     ようなシェル構文はそもそも解釈されないが、念のため文字列レベルでも
#     拒否する(縦深防御。将来shell=Trueに変更された場合の事故も防ぐ)。
#     リダイレクト('>'/'<')も同様の理由で拒否する。
# (4) 絶対パス・'..'を含む相対パスをどの引数にも許可しないことで、
#     プロジェクトディレクトリの外への読み書きを防ぐ(cdはシェル無しでは
#     単体のコマンドとして存在しないため既に実行できないが、念のため
#     ブロックリストにも加えている)。相対パスに'..'が無ければ、
#     cwd=project_dirの外を指すことは原理的にできない。
#
# なお、書き込まれたファイル自体に危険なコードが含まれていた場合
# (例: 生成された.pyファイルが内部でファイル削除を行う)、そのファイルを
# 検証のために実行すれば当然そのコードは動いてしまう。これは以前の
# run_testでも同じ性質の既存のリスクであり、今回新たに生まれたもの
# ではない(サンドボックス化・コンテナ化のような対策は今回のスコープ外)。
RUN_COMMAND_TIMEOUT_SEC = 10
RUN_COMMAND_MAX_OUTPUT_CHARS = 4000
# 'yes'のように出力が際限なく続くコマンドでも、タイムアウトを待たずに
# 読み取りを打ち切ることでメモリを圧迫しないようにする上限
# (最終的な表示用の切り詰め幅より十分大きいが無制限ではない値。
# Raspberry Piのようなメモリの少ない実機での常駐も想定した値)。
_RUN_COMMAND_OUTPUT_READ_CAP_CHARS = RUN_COMMAND_MAX_OUTPUT_CHARS * 10

_RUN_COMMAND_BLOCKED_SHELL_CHARS = ";|&`<>\n"

_RUN_COMMAND_BLOCKED_COMMAND_NAMES = {
    # ネットワークアクセス
    "curl", "wget", "wget2", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp",
    "ftp", "ftps", "tftp", "telnet", "rsync", "rlogin", "rsh", "ping", "traceroute",
    "dig", "nslookup", "host", "whois", "nmap", "git",
    # 破壊的なファイル操作(write_file/move_file/delete_fileを使うべき)
    "rm", "rmdir", "mv", "cp", "dd", "shred", "unlink", "truncate", "install", "ln",
    "mkfifo", "tee", "patch",
    # 権限昇格
    "sudo", "su", "doas", "pkexec", "runas",
    # プロセス・システム制御
    "kill", "pkill", "killall", "reboot", "shutdown", "halt", "poweroff",
    "systemctl", "service", "launchctl", "init", "telinit", "crontab", "at",
    # 権限・所有者の変更
    "chmod", "chown", "chgrp", "setfacl", "chattr", "attrib", "icacls",
    # シェル自体・ディレクトリ移動(shell=Trueを使わないため実体は無いが念のため)
    "bash", "sh", "zsh", "csh", "tcsh", "ksh", "fish", "dash", "ash", "busybox",
    "powershell", "pwsh", "cmd", "osascript", "cd",
    # 他コマンドを間接的に起動できる「ラッパー」(ブロックリストの迂回経路)
    "env", "xargs", "find", "nohup", "setsid", "exec", "eval", "source",
}

# インタプリタごとの「インラインコード実行/危険なモジュール実行」フラグ。
# gccの-c(リンクなしコンパイル)のような無関係な同名フラグを誤って
# 禁止しないよう、コマンドごとに個別の集合として持つ(依頼の「.c → gcc
# -fsyntax-onlyのみ許可」のような決め打ちをやめ、モデルが自分でコマンドを
# 選べるようにしたことの裏返しとして必要になった防御)。
_RUN_COMMAND_BLOCKED_FLAGS_BY_INTERPRETER = {
    "python3": {"-c", "-m", "-i"},
    "python": {"-c", "-m", "-i"},
    "node": {"-e", "--eval", "-p", "--print", "-r", "--require", "-i", "--interactive"},
    "ruby": {"-e"},
    "perl": {"-e", "-E"},
    "php": {"-r"},
}


def _validate_run_command(command: str) -> tuple:
    """`(argv, エラーメッセージ)`を返す。ブロックリスト方式の各種検証を
    行う(詳細は直前のコメントを参照)。
    """
    raw = command or ""
    if not raw.strip():
        return None, "コマンドが指定されていません。"
    if any(ch in raw for ch in _RUN_COMMAND_BLOCKED_SHELL_CHARS) or "$(" in raw:
        return None, (
            "シェルの制御文字(; | & ` $() < > や改行)を含むコマンドは実行できません。"
            "1つの単純なコマンドだけを指定してください。"
        )

    parts = raw.split()
    if not parts:
        return None, "コマンドが指定されていません。"

    for token in parts:
        basename = re.sub(r"\.(exe|bat|cmd|com)$", "", os.path.basename(token).lower())
        if basename in _RUN_COMMAND_BLOCKED_COMMAND_NAMES:
            return None, (
                f"'{token}' は使用できません(ネットワークアクセス・破壊的なファイル操作・"
                "権限昇格・シェルの起動につながるコマンドは禁止されています)。"
                "ファイルの作成・上書き・移動・削除にはwrite_file・move_file・delete_fileを使ってください。"
            )
        if token.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", token):
            return None, f"'{token}': プロジェクトディレクトリの外を指す絶対パスは使用できません。"
        if ".." in re.split(r"[\\/]", token):
            return None, f"'{token}': プロジェクトディレクトリの外に出る'..'を含むパスは使用できません。"

    blocked_flags = _RUN_COMMAND_BLOCKED_FLAGS_BY_INTERPRETER.get(parts[0])
    if blocked_flags:
        used = blocked_flags.intersection(parts[1:])
        if used:
            return None, (
                f"'{parts[0]}'の{'/'.join(sorted(used))}は任意のコード実行やネットワークアクセスに"
                f"つながるため使用できません。検証したいファイルを直接指定してください"
                f"(例: '{parts[0]} app.py')。"
            )

    return parts, None


def _run_subprocess_with_output_cap(argv: list, cwd: str, timeout_sec: float, read_cap_chars: int) -> tuple:
    """`argv`を`cwd`で実行し、`(returncode, output, timed_out)`を返す。

    仮の判断: 素朴に`subprocess.run(..., capture_output=True, timeout=N)`
    だけを使うと、'yes'のように出力が際限なく続くコマンドの場合、
    タイムアウトで強制終了されるまでの間に出力全体をメモリ上に貯め
    続けてしまい、実機(Raspberry Pi等メモリの少ない機体を含む)で
    メモリを圧迫しかねない。`select`でパイプの読み取り可否を都度確認
    しながら少しずつ読むことで、(1)出力量が`read_cap_chars`を超えたら
    それ以上読み取らずタイムアウトによる強制終了を待つ、(2)出力を
    まったく出さずに無応答なコマンド(`sleep 100`等)でも、`select`の
    タイムアウト経由で確実に`timeout_sec`で強制終了できる、の両方を
    満たす。
    """
    proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks = []
    total_len = 0
    deadline = time.monotonic() + timeout_sec
    timed_out = False
    fd = proc.stdout.fileno()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if total_len >= read_cap_chars:
                if proc.poll() is not None:
                    break
                time.sleep(min(0.1, remaining))
                continue
            ready, _, _ = select.select([fd], [], [], min(0.2, remaining))
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                break
            if not data:
                if proc.poll() is not None:
                    break
                continue
            text = data.decode("utf-8", errors="replace")
            chunks.append(text)
            total_len += len(text)
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass

    output = "".join(chunks)
    returncode = proc.returncode if proc.returncode is not None else -1
    return returncode, output, timed_out


_PLAYWRIGHT_CHECK_TIMEOUT_MS = 15000


def _check_html_with_playwright(file_path: str) -> tuple:
    """`(status, detail)`を返す。`status`は次のいずれか:
    - `"ok"`: ヘッドレスブラウザで開けて、コンソールエラー・実行時エラーが無かった
    - `"error"`: コンソールエラーまたは未捕捉の実行時エラーを検出した(`detail`に内容)
    - `"skipped"`: Playwrightが実機に無い、またはブラウザの起動自体に失敗した
      ためスキップした(`detail`にその理由。gcc/node等の構文チェックと同じく、
      ツール不在はエラー扱いにしない)

    仮の判断: idの不一致のようなJS実行時エラーは、静的な構文チェック
    (node --check)では検出できない(構文としては正しく、実行してみて
    初めて`getElementById`がnullを返す等で表面化するため)。ヘッドレス
    ブラウザで実際にファイルを開き、コンソールに出力されたエラー
    (`console.error`・未捕捉の例外)を拾うことで、この種の不具合を
    機械的に検出できるようにする。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "skipped", "Playwright(ヘッドレスブラウザ操作ツール)が見つからないため、動作検証をスキップしました。"

    console_errors = []
    page_errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(timeout=_PLAYWRIGHT_CHECK_TIMEOUT_MS)
            try:
                page = browser.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.goto(f"file://{file_path}", timeout=_PLAYWRIGHT_CHECK_TIMEOUT_MS)
                page.wait_for_timeout(500)
            finally:
                browser.close()
    except Exception as exc:
        return "skipped", f"ヘッドレスブラウザでの検証実行に失敗したためスキップしました: {exc}"

    errors = console_errors + page_errors
    if errors:
        return "error", "\n".join(errors[:10])
    return "ok", ""


def _run_project_command(project_dir: str, command: str) -> str:
    """`run_command`ツール本体。バリデーションを通過したコマンドを
    `project_dir`をカレントディレクトリとして実行し、結果をモデルに
    返すJSON文字列を組み立てる。
    """
    argv, error = _validate_run_command(command)
    if error:
        return json.dumps({"ok": False, "message": error}, ensure_ascii=False)

    if argv[0] == "check_html":
        if len(argv) != 2:
            return json.dumps(
                {"ok": False, "message": "check_htmlは 'check_html <HTMLファイル名>' の形式で指定してください。"},
                ensure_ascii=False,
            )
        safe_path, path_error = _resolve_safe_project_path(project_dir, argv[1])
        if path_error:
            return json.dumps({"ok": False, "message": path_error}, ensure_ascii=False)
        if not os.path.isfile(safe_path):
            return json.dumps({"ok": False, "message": f"{argv[1]} が見つかりません。"}, ensure_ascii=False)
        status, detail = _check_html_with_playwright(safe_path)
        if status == "ok":
            return json.dumps(
                {"ok": True, "returncode": 0, "output": "ヘッドレスブラウザでコンソールエラーは検出されませんでした。"},
                ensure_ascii=False,
            )
        if status == "error":
            return json.dumps(
                {"ok": False, "returncode": 1, "output": detail[:RUN_COMMAND_MAX_OUTPUT_CHARS]}, ensure_ascii=False,
            )
        return json.dumps({"ok": False, "message": detail}, ensure_ascii=False)  # skipped

    try:
        returncode, output, timed_out = _run_subprocess_with_output_cap(
            argv, project_dir, RUN_COMMAND_TIMEOUT_SEC, _RUN_COMMAND_OUTPUT_READ_CAP_CHARS,
        )
    except FileNotFoundError:
        # 仮の判断: './<出力名>'のようにコンパイル済みバイナリをまだ
        # 作っていない場合にありがちな失敗のため、その場合だけヒントを添える。
        hint = "(先に必要な準備(コンパイル等)をしてください)" if argv[0].startswith("./") else ""
        return json.dumps({"ok": False, "message": f"'{argv[0]}' が見つかりませんでした。{hint}"}, ensure_ascii=False)
    except PermissionError:
        return json.dumps({"ok": False, "message": f"'{argv[0]}' を実行する権限がありません。"}, ensure_ascii=False)

    if timed_out:
        return json.dumps(
            {"ok": False, "message": f"実行が{RUN_COMMAND_TIMEOUT_SEC}秒でタイムアウトしたため強制終了しました。"},
            ensure_ascii=False,
        )
    return json.dumps({
        "ok": returncode == 0, "returncode": returncode,
        "output": output[:RUN_COMMAND_MAX_OUTPUT_CHARS],
    }, ensure_ascii=False)


def _execute_project_tool_call(
    project_dir: str, tool_call: dict, print_lock: threading.Lock = None, tag: str = None,
) -> str:
    """1つのtool_call(OpenAI互換形式)を、プロジェクトツールとして実行し、
    モデルに返す結果をJSON文字列として返す。
    """
    tag = tag or "ツール実行"
    function = tool_call.get("function", {})
    name = function.get("name", "")
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}

    if name == READ_FILE_TOOL_NAME:
        filename = arguments.get("filename", "")
        _print_tagged(print_lock, tag, f"[📖 {filename or '他のファイル'} を読みに行っています...]")
        return _read_project_file_fresh(
            project_dir, filename,
            arguments.get("start_line"), arguments.get("end_line"),
            arguments.get("line"), arguments.get("context_lines"),
        )
    if name == SEARCH_IN_FILE_TOOL_NAME:
        filename = arguments.get("filename", "")
        query = arguments.get("query", "")
        _print_tagged(print_lock, tag, f"[🔍 {filename or '他のファイル'} 内で '{query}' を検索しています...]")
        return _search_in_project_file(project_dir, filename, query, arguments.get("context_lines"))
    if name == LIST_DIR_TOOL_NAME:
        _print_tagged(print_lock, tag, "[📂 ファイル一覧を確認しています...]")
        return _list_project_directory(project_dir)
    if name == WRITE_FILE_TOOL_NAME:
        filename = arguments.get("filename", "")
        _print_tagged(print_lock, tag, f"[✏️ {filename or '(不明なファイル)'} を書き込んでいます...]")
        return _write_project_file(project_dir, filename, arguments.get("content", ""))
    if name == MOVE_FILE_TOOL_NAME:
        old_filename = arguments.get("old_filename", "")
        new_filename = arguments.get("new_filename", "")
        _print_tagged(print_lock, tag, f"[📦 {old_filename} を {new_filename} に変更しています...]")
        return _move_project_file(project_dir, old_filename, new_filename)
    if name == DELETE_FILE_TOOL_NAME:
        return _delete_project_file(project_dir, arguments.get("filename", ""), print_lock, tag)
    if name == MAKE_DIRECTORY_TOOL_NAME:
        dirname = arguments.get("dirname", "")
        _print_tagged(print_lock, tag, f"[📁 {dirname} ディレクトリを作成しています...]")
        return _make_project_directory(project_dir, dirname)
    if name == RUN_COMMAND_TOOL_NAME:
        command = arguments.get("command", "")
        _print_tagged(print_lock, tag, f"[🧪 {command} を実行しています...]")
        return _run_project_command(project_dir, command)
    return json.dumps({"ok": False, "message": f"未知のツールです: {name}"}, ensure_ascii=False)


# 仮の判断: rename→import修正→テスト実行のような複数手順が必要な
# 修正依頼に対応するため、read_file専用のMAX_READ_FILE_CALLS_PER_REVIEW
# (3回)より多くのラウンドを許容する。それでも無制限にはせず、暴走防止の
# 上限を設ける。当初は12回としていたが、大規模なタスクの分解・レビュー・
# 修正では実機で頻繁に上限に達してしまうことが分かった。CHAT_MAX_OUTPUT_TOKENS
# 等のトークン数上限による暴走防止は別途効いているため、往復回数の上限は
# 50回まで緩和する。
MAX_PROJECT_TOOL_ROUNDS = 50

# 仮の判断: 実機で、//fixが「修正が完了しました」と表示し、PROGRESS.mdにも
# 更新履歴を記録したにもかかわらず、実際にはファイルの中身が一切変更
# されていないという不具合が報告された。原因は、モデルが「修正しました」
# という文章だけを返してwrite_file等を一度も呼ばなかった場合や、
# write_fileが呼ばれたがパストラバーサル対策等で実際には失敗した場合に、
# それを検出せず無条件に完了扱いにしていたこと。これらのツールは
# 「プロジェクトの実ファイルを実際に変更する」ツールであり、その成功
# (JSON結果の"ok"フィールド)を追跡することで、「本当に何か変更されたか」
# を機械的に判定できるようにする。make_directoryは空のディレクトリを
# 作るだけで「修正の実体」とは言えないため含めない。read_file・list_dir・
# run_commandはファイルを変更しないため対象外。
_MUTATING_PROJECT_TOOL_NAMES = {WRITE_FILE_TOOL_NAME, MOVE_FILE_TOOL_NAME, DELETE_FILE_TOOL_NAME}


def _mutated_filename_from_tool_result(tool_call: dict, result: str) -> str:
    """`tool_call`が実際にファイルを変更した(write_file/move_file/
    delete_fileが成功した)場合、そのファイル名を返す。それ以外
    (対象外のツール・失敗した呼び出し・結果が解析できない場合)は
    空文字列を返す。
    """
    function = tool_call.get("function", {})
    name = function.get("name", "")
    if name not in _MUTATING_PROJECT_TOOL_NAMES:
        return ""
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        return ""
    return parsed.get("filename") or parsed.get("new_filename") or "(不明なファイル)"


# 仮の判断: 実機で、モデル(qwen3-235b等)が「〇〇ファイルを修正しました」
# 「write_fileツールを使用して上書きしました」と、あたかも実行したかの
# ような説明文を返しながら、実際にはwrite_file等のツールを一度も呼んで
# いない(＝ツール呼び出しの構造化出力ではなく、ただの説明文としてしか
# 出力していない)現象が報告された。この場合、以前は(前回の修正で)
# 「実行されませんでした」と正直に報告するようにしたが、それだけでは
# 依頼が達成されないままで終わってしまう。そこで、最終回答が
# pending_tool_callsを伴わずに終わった時点でmodified_filesが空であれば、
# セッションを終了させる前に1回だけ「実際にツールを呼んでください」と
# 明示的に促し、再試行の機会を与える(無制限に促し続けると暴走に
# つながるため、1回のみ)。それでも改善しなければ、これまで通り正直に
# 「実行されませんでした」と報告する。
_NO_TOOL_CALL_NUDGE_MESSAGE = (
    "あなたは変更を説明していますが、write_file・move_file・delete_fileのいずれの"
    "ツールも実際には呼び出されていません。説明文だけでは、プロジェクトのファイルには"
    "何も反映されません。先ほど説明した変更内容を、実際にwrite_file等のツールを"
    "呼び出して反映してください。よく確認した結果、本当に変更が不要だと判断した場合は、"
    "その理由を説明してください。"
)


def _collect_answer_with_project_tools(
    candidate: dict, org_fingerprint: str, messages: list, project_dir: str,
    print_lock: threading.Lock = None, tag: str = None,
):
    """既存プロジェクトへのファイル操作・テスト実行ツール群を提供しながら
    1つの回答を集める。`_collect_review_answer_with_read_file`と同じ
    「/chatはステートレスなので、ツール実行結果を足したmessagesで再度
    /chatを呼び直せば会話を継続できる」という設計を踏襲するが、read_file
    単体ではなく複数種類のツールを1つのループで扱えるように汎化した。
    `(content, error, truncated, modified_files)`のタプルを返す。
    `truncated`は最終回答がCHAT_MAX_OUTPUT_TOKENS上限に達して途中で
    打ち切られたかどうかを表す。`modified_files`は、write_file/move_file/
    delete_fileが実際に成功した結果、変更が確認できたファイル名のリスト
    (呼び出し順、重複あり)。エラーで打ち切られた場合・往復回数の上限に
    達した場合でも、それまでに実際に成功した変更は失われず反映される
    (呼び出し元が「途中終了でも一部は変更されている」ことを正しく
    報告できるようにするため)。

    モデルが説明文だけを返してツールを一度も呼ばなかった場合は、
    `_NO_TOOL_CALL_NUDGE_MESSAGE`参照。
    """
    messages = list(messages)
    modified_files = []
    nudged = False
    for _round_num in range(MAX_PROJECT_TOOL_ROUNDS):
        answer_parts = []
        pending_tool_calls = None
        error = None
        truncated = False
        for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages, offer_project_tools=True):
            if "error" in event:
                error = event["error"]
                break
            if "pending_tool_calls" in event:
                pending_tool_calls = event["pending_tool_calls"]
                break
            content = event.get("content")
            if content:
                answer_parts.append(content)
            if event.get("done"):
                truncated = bool(event.get("truncated"))
                break

        if error:
            return "", error, False, modified_files
        if not pending_tool_calls:
            final_answer = "".join(answer_parts)
            if not modified_files and not nudged:
                nudged = True
                _print_tagged(
                    print_lock, tag or candidate["label"],
                    "[⚠️ 説明文だけでツールが実際には呼ばれていないため、実行するよう促して再試行しています...]",
                )
                messages.append({"role": "assistant", "content": final_answer})
                messages.append({"role": "user", "content": _NO_TOOL_CALL_NUDGE_MESSAGE})
                continue
            return final_answer, None, truncated, modified_files

        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
        for tool_call in pending_tool_calls:
            result = _execute_project_tool_call(project_dir, tool_call, print_lock, tag or candidate["label"])
            messages.append({"role": "tool", "content": result, "tool_call_id": tool_call.get("id")})
            mutated_filename = _mutated_filename_from_tool_result(tool_call, result)
            if mutated_filename:
                modified_files.append(mutated_filename)

    return "", f"ツール呼び出しの往復回数が上限({MAX_PROJECT_TOOL_ROUNDS}回)に達しました", False, modified_files


def _syntax_check_all_files(project_dir: str) -> list:
    """プロジェクトディレクトリ内(サブディレクトリを含む)の全ファイルに、
    拡張子に応じた機械的な構文チェック(`_check_file_syntax`、言語非依存)
    を行い、構文エラーが残っているファイルの相対パスのリストを返す
    (対応していない拡張子・スキップされたファイルはこのリストに含めない。
    あくまで「確実に壊れている」と判定できたものだけを報告する)。

    仮の判断: 個々の`write_file`呼び出し直後の即時フィードバックに加えて、
    修正セッション全体が終わった後にプロジェクト全体を最終確認する
    安全網として用意した(モデルが「直したつもり」でも別のファイルへの
    影響を見落とす可能性があるため)。`_list_project_files`と同じく
    再帰的に走査する(サブディレクトリ内のファイルも対象)。
    """
    broken = []
    for filename in _list_project_files(project_dir):
        path = os.path.join(project_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception:
            continue
        status, _detail = _check_file_syntax(filename, code, file_path=path)
        if status == "error":
            broken.append(filename)
    return broken


def _ask_organization_multi(port: int, org_fingerprint: str, messages: list) -> None:
    """`//multi <質問>` 用: 組織内の空きリソース上位複数台(既定3台)に同時に
    同じ質問を送り、それぞれの回答を完了した順に表示する。

    仮の判断: 通常モードと同じ「タスクの性質に応じた優先順位」で候補を
    並べたうえで、上位から複数台を選ぶ。会話履歴には、成功した候補のうち
    優先順位が最も高いものの回答だけをassistantの発言として追記する
    (全員分の回答を履歴に混ぜると、次の質問時に「会話の前提」が曖昧に
    なってしまうため)。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return

    latest_question = messages[-1]["content"] if messages else ""
    task_type = _classify_task(latest_question)
    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, task_type)

    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    targets = candidates[:MULTI_QUERY_TARGET_COUNT]
    target_labels = ", ".join(f"{c['label']}(モデル: {c['model']})" for c in targets)
    print(f"[📡 {_selection_reason_label(task_type, targets[0])}: {len(targets)}台に同時問い合わせしています: {target_labels}]")

    results = [None] * len(targets)
    print_lock = threading.Lock()

    def worker(index: int, candidate: dict) -> None:
        # 仮の判断: 複数スレッドが同じmessagesを同時に読むこと自体は安全だが、
        # 誤って書き換えてしまうバグを将来混入させないよう、念のため
        # スレッドごとにコピーを渡す。
        answer, error, truncated = _collect_answer_from_candidate(candidate, org_fingerprint, list(messages))
        results[index] = (candidate, answer, error)
        with print_lock:
            print()
            print(f"--- {candidate['label']} (モデル: {candidate['model']}) ---")
            if error:
                print(f"(問い合わせに失敗しました: {error})")
            else:
                print(answer if answer else "(応答がありませんでした)")
                if truncated:
                    print(f"(⚠️ 応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました)")

    threads = [threading.Thread(target=worker, args=(i, c), daemon=True) for i, c in enumerate(targets)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print()

    # 優先順位が最も高い(targets先頭に近い)候補の中から、最初に成功したものの
    # 回答だけを会話履歴に残す。
    for candidate, answer, error in results:
        if not error and answer:
            messages.append({"role": "assistant", "content": answer})
            break


# ---------------------------------------------------------------------------
# 異なる依頼を異なるメンバーへ同時に振り分ける("//parallel"コマンド、実験1)
# ---------------------------------------------------------------------------
#
# //multi は「同じ質問」を複数メンバーに投げて回答を見比べる仕組みだが、
# 「storage.pyの実装をメンバーA、cli.pyの実装をメンバーBに、同時にそれぞれ
# 依頼する」という並行モジュール開発の実験には使えない。//parallel はこの
# 「異なる依頼を、異なるメンバーに同時に送り、各回答からコード部分を
# 抽出して手元に保存する」というユースケース専用のコマンドとして追加した。

PARALLEL_QUERY_COMMAND = "//parallel"

# 仮の判断: 「<ファイル名>:<依頼内容>」を"|"区切りで並べる構文にした。
# 依頼内容そのものに"|"を含めたい場合には対応できないが、今回のスコープ
# (短い1行の依頼文を複数メンバーに振り分ける)では十分と判断した。
_PARALLEL_TASK_PATTERN = re.compile(r"^([^:]+):(.+)$", re.DOTALL)


def _parse_parallel_tasks(text: str) -> list:
    """`//parallel <ファイル名1>:<依頼1> | <ファイル名2>:<依頼2> ...` の
    引数部分を解析し、[(ファイル名, 依頼内容), ...] のリストにする。
    構文に合わない要素(":"が無い等)は無視する。
    """
    tasks = []
    for chunk in text.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = _PARALLEL_TASK_PATTERN.match(chunk)
        if not match:
            continue
        filename = match.group(1).strip()
        request = match.group(2).strip()
        if filename and request:
            tasks.append((filename, request))
    return tasks


# 仮の判断: 応答テキストの中から最初のフェンス付きコードブロック
# (```python ... ``` や ``` ... ```)の中身だけを取り出す。言語名の有無は
# 問わない。
_CODE_BLOCK_PATTERN = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _extract_code_from_answer(answer: str) -> str:
    """回答テキストからコードブロックの中身を抽出する。コードブロックが
    見つからない場合は、応答テキスト全体をそのまま返す(仮の判断:
    モデルがコードブロックを使わずテキストだけでコードを返してくる
    可能性もあるため、抽出失敗を理由に何も保存しないより、応答全体を
    保存して人間が後で判断できるようにする方を優先した)。
    """
    match = _CODE_BLOCK_PATTERN.search(answer)
    if match:
        return match.group(1).rstrip("\n") + "\n"
    return answer.rstrip("\n") + "\n" if answer else answer


def _dispatch_and_save_parallel_tasks(tasks: list, candidates: list, org_fingerprint: str, out_dir: str) -> list:
    """タスク(ファイル名, 依頼内容)のリストを、優先順位順に並べた候補へ先頭から
    1対1で割り当て、同時に問い合わせて各回答からコード部分を抽出し、
    out_dir 配下にファイル名で保存する。`//parallel`(手動指定)専用の
    下請け関数(協業モード・`//agree`は、タスク数がメンバー数を超えても
    全タスクを実装できるタスクキュー方式`_run_collaborative_task_queue`を
    別途使う)。

    保存に成功したタスクについて、`[{"filename":, "candidate":, "code":}, ...]`
    (タスクを書いた順)を返す。`//parallel`(手動指定)側は戻り値を使わない。

    仮の判断:
    - 割り当ては「タスクを書いた順」と「優先順位順に並べた候補」を先頭から
      1対1で対応させるだけの単純な方式にした。特定のメンバーを名指しで
      選ぶ構文は今回のスコープ外。タスク数が候補数を超える場合は、超えた
      分をスキップする(`//parallel`はユーザー自身がファイル名・割り当てを
      明示的に指定するコマンドであり、キュー方式による自動繰り越しは
      今回のスコープ外とした)。
    - 各タスクは独立した1回きりのやりとりとして扱い、対話モードの会話履歴
      (messages)には追加しない(結果はファイル保存が主目的で、後続の会話の
      前提として扱うべき性質のものではないため)。
    - 候補がタスク数より少ない場合は、先頭から割り当てられる分だけ実行し、
      残りはスキップしてその旨をログに残す(今回のスコープでは自動リトライや
      1台への複数タスク割り当てまでは行わない)。
    """
    if len(candidates) < len(tasks):
        print(
            f"[⚠️ 依頼数({len(tasks)}件)に対して、問い合わせ可能なメンバーが{len(candidates)}台しかいません。"
            f"先頭から割り当てられる分だけ実行します。]"
        )
        skipped = tasks[len(candidates):]
        for filename, _request in skipped:
            logger.warning("%s の依頼は割り当て可能なメンバーが無かったためスキップしました。", filename)
        tasks = tasks[:len(candidates)]

    assignments = list(zip(tasks, candidates))
    if not assignments:
        print("実行できるタスクがありませんでした。")
        return []

    target_labels = ", ".join(
        f"{filename}→{c['label']}(モデル: {c['model']})" for (filename, _req), c in assignments
    )
    print(f"[📡 {len(assignments)}件のタスクを異なるメンバーに同時に依頼しています: {target_labels}]")

    results = [None] * len(assignments)
    print_lock = threading.Lock()

    def worker(index: int, filename: str, request: str, candidate: dict) -> None:
        task_messages = [{"role": "user", "content": request}]
        answer, error, truncated = _collect_answer_from_candidate(candidate, org_fingerprint, task_messages)
        results[index] = (filename, candidate, answer, error)
        with print_lock:
            print()
            print(f"--- {filename} ← {candidate['label']} (モデル: {candidate['model']}) ---")
            if error:
                print(f"(問い合わせに失敗しました: {error})")
            else:
                print(answer if answer else "(応答がありませんでした)")
                if truncated:
                    print(f"(⚠️ 応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました)")

    threads = [
        threading.Thread(target=worker, args=(i, filename, request, candidate), daemon=True)
        for i, ((filename, request), candidate) in enumerate(assignments)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print()

    os.makedirs(out_dir, exist_ok=True)
    saved = []
    for filename, candidate, answer, error in results:
        if error or not answer:
            logger.warning(
                "%s の生成に失敗したため保存をスキップしました(担当: %s): %s",
                filename, candidate["label"], error or "応答なし",
            )
            continue
        code = _extract_code_from_answer(answer)
        dest_path = os.path.join(out_dir, filename)
        with open(dest_path, "w", encoding="utf-8") as f:
            f.write(code)
        saved.append({"filename": filename, "candidate": candidate, "code": code})
        print(f"[💾 {dest_path} に保存しました (担当: {candidate['label']})]")

    if not saved:
        print("保存できたファイルがありませんでした。")

    return saved


def _ask_organization_parallel(port: int, org_fingerprint: str, command_text: str, out_dir: str) -> None:
    """`//parallel <ファイル名1>:<依頼1> | <ファイル名2>:<依頼2> ...` 用:
    タスクごとに異なるメンバーへ同時に依頼を送り、各回答からコード部分を
    抽出して out_dir 配下にファイル名で保存する。

    仮の判断: コード生成の依頼という性質上、候補の優先順位付けは常に
    コーディング系タスク(TASK_TYPE_CODING)として行う(コーディング系
    モデルを持つメンバーを優先)。実際の割り当て・実行は
    `_dispatch_and_save_parallel_tasks`に委ねる。
    """
    tasks = _parse_parallel_tasks(command_text)
    if not tasks:
        print(
            f"使い方: {PARALLEL_QUERY_COMMAND} <ファイル名1>:<依頼内容1> | <ファイル名2>:<依頼内容2> ...\n"
            f"例: {PARALLEL_QUERY_COMMAND} storage.py:ToDoをJSONで管理する関数群を書いて | cli.py:storageを使うCLIを書いて"
        )
        return

    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return

    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    _dispatch_and_save_parallel_tasks(tasks, candidates, org_fingerprint, out_dir)


# ---------------------------------------------------------------------------
# 対話プロトコル(共通基盤) — 複数ノードによる合意形成の仕組み
# ---------------------------------------------------------------------------
#
# これまでの寄合は、計画フェーズ(//agree)で1〜2往復程度のやり取りを
# しただけで、そのまま実装フェーズに進んでしまっていた。「最初に思いついた
# 案がそのまま最後まで固定される」「自己反証のループが実質ゼロ」という
# 問題への対応として、複数ノードが役割(提案役・反論役・統合役)を持って
# 議論し、合意形成する対話プロトコルを実装する。特定のフェーズ専用の
# 機構にはせず、計画フェーズ(//agree)・修正フェーズ(//fix)・
# レビューフェーズ・計画のみモード(//plan-only)から共通で呼び出せる
# 基盤として1つだけ実装する。

DIALOGUE_ROLE_PROPOSER = "proposer"
DIALOGUE_ROLE_CRITIC = "critic"
DIALOGUE_ROLE_INTEGRATOR = "integrator"

_DIALOGUE_ROLE_LABEL_JA = {
    DIALOGUE_ROLE_PROPOSER: "提案役",
    DIALOGUE_ROLE_CRITIC: "反論役",
    DIALOGUE_ROLE_INTEGRATOR: "統合役",
}

# セーフティリミット: 「1つの議題」についての対話1回あたり、参加している
# 全ノードの発言回数の合計がこれを超えたら強制的に一時停止し、それまでの
# 議事録全文と要約を人間に提示して判断を仰ぐ(寄合全体を通しての累積では
# なく、_run_dialogue呼び出し1回ごとに数える)。
DIALOGUE_SAFETY_LIMIT_UTTERANCES = 50

DIALOGUE_STATUS_CONSENSUS = "consensus"
DIALOGUE_STATUS_SAFETY_LIMIT = "safety_limit"
DIALOGUE_STATUS_NEEDS_HUMAN = "needs_human"
# 提案役からすら実のある応答が一度も得られなかった場合(問い合わせの失敗・
# 空応答)。これは議論が難航した結果ではなく単純な疎通の問題である可能性が
# 高いため、他の3状態(議論の結果として人間の判断を仰ぐべき状態)とは区別し、
# 呼び出し元がこれまで通りの経路に安全側フォールバックできるようにする。
DIALOGUE_STATUS_NO_ENGAGEMENT = "no_engagement"

_DIALOGUE_STATUS_LABEL_JA = {
    DIALOGUE_STATUS_CONSENSUS: "合意",
    DIALOGUE_STATUS_SAFETY_LIMIT: "セーフティリミットにより一時停止",
    DIALOGUE_STATUS_NEEDS_HUMAN: "人間の確認が必要",
    DIALOGUE_STATUS_NO_ENGAGEMENT: "応答なし",
}


def _assign_discourse_roles(candidates: list) -> dict:
    """優先順位順(得意分野・空きメモリ順)に並んだ候補から、提案役・
    反論役・統合役を割り当てる。

    仮の判断: 役割は固定人格ではなく、議題ごとに候補の並び順
    (`_select_chat_candidates`が得意分野・空きメモリで並べた順)に基づいて
    動的に決める。候補が3台に満たない場合は、同じノードが複数の役割を
    兼務する(2台なら2台目が反論役・統合役を兼務、1台なら1台がすべての
    役割を兼務する自己対話になる。組織に1台しかいない場合でも対話
    プロトコル自体は動作する)。
    """
    if not candidates:
        return {}
    proposer = candidates[0]
    critic = candidates[1] if len(candidates) >= 2 else candidates[0]
    integrator = candidates[2] if len(candidates) >= 3 else critic
    return {
        DIALOGUE_ROLE_PROPOSER: proposer,
        DIALOGUE_ROLE_CRITIC: critic,
        DIALOGUE_ROLE_INTEGRATOR: integrator,
    }


_DIALOGUE_PROPOSER_ROUND1_TEMPLATE = """あなたは{speaker_label}さんとして、複数のメンバーによる対話(寄合の対話プロトコル)に「提案役」として参加しています。以下の議題について、具体的な提案をしてください。

【議題】
{topic}

【背景・参考情報】
{background}

【出力形式についての指示】
{output_instruction}

他の説明文や前置きは不要です。指示された出力形式のみで答えてください。
"""

_DIALOGUE_PROPOSER_REVISE_TEMPLATE = """あなたは{speaker_label}さんとして、複数のメンバーによる対話(寄合の対話プロトコル)に「提案役」として参加しています。以下はこれまでの議論の経過です。反論役からの指摘を踏まえ、提案を改善してください。

【議題】
{topic}

【背景・参考情報】
{background}

【これまでの議論】
{transcript}

【出力形式についての指示】
{output_instruction}

他の説明文や前置きは不要です。指示された出力形式のみで、改善した提案を答えてください。
"""

_DIALOGUE_CRITIC_TEMPLATE = """あなたは{speaker_label}さんとして、複数のメンバーによる対話(寄合の対話プロトコル)に「反論役」として参加しています。あえて穴やリスクを探し、批判的に検討する役目です。

【議題】
{topic}

【背景・参考情報】
{background}

【これまでの議論】
{transcript}

提案の問題点・リスク・抜け漏れを具体的に指摘してください。問題が無ければその旨を書いてください。

出力の最後に、次のいずれか1つを必ず独立した行として書いてください:
評価: 合意
評価: 要修正
評価: 情報不足

(「評価: 情報不足」は、議論を重ねてもアイデアが出尽くしており、人間の追加の助言が必要だと判断した場合に使ってください。)
"""

_DIALOGUE_INTEGRATOR_TEMPLATE = """あなたは{speaker_label}さんとして、複数のメンバーによる対話(寄合の対話プロトコル)に「統合役」として参加しています。複数の意見を俯瞰し、まとめる役目です。

【議題】
{topic}

【背景・参考情報】
{background}

【これまでの議論】
{transcript}

これまでの議論を踏まえ、次のいずれか1つを独立した行として必ず書いてください:
判定: 合意
判定: 継続
判定: 人間に確認

「判定: 合意」の場合は、続けて「最終合意内容:」という見出しの下に、以下の指示に従って最終的な合意内容を書いてください:
{output_instruction}

「判定: 人間に確認」の場合は、続けて「人間への確認事項:」という見出しの下に、人間に確認・相談したい内容を具体的に書いてください(議論を進める上で人間の判断が必要な場合、または議論を重ねてもアイデアが出尽くした場合)。

それ以外(「判定: 継続」)の場合は、続けて議論の現状を2〜3文で要約してください。
"""

_CRITIC_VERDICT_PATTERN = re.compile(r"評価:\s*(合意|要修正|情報不足)")
_INTEGRATOR_VERDICT_PATTERN = re.compile(r"判定:\s*(合意|継続|人間に確認)")


def _parse_critic_verdict(answer: str) -> str:
    """反論役の回答から評価(合意/要修正/情報不足)を取り出す。見つから
    ない場合は、暴走(誤った合意判定)より安全な「要修正」に倒す。
    """
    match = _CRITIC_VERDICT_PATTERN.search(answer or "")
    return match.group(1) if match else "要修正"


def _parse_integrator_verdict(answer: str) -> str:
    """統合役の回答から判定(合意/継続/人間に確認)を取り出す。見つから
    ない場合は「継続」に倒し、誤って合意扱いにしないようにする。
    """
    match = _INTEGRATOR_VERDICT_PATTERN.search(answer or "")
    return match.group(1) if match else "継続"


def _extract_dialogue_section(answer: str, heading: str) -> str:
    """統合役の回答から`heading`(例: "最終合意内容:")直後の本文を取り
    出す。見出しが見つからない場合は回答全体を返す(見出しの付け忘れに
    対するフォールバック)。
    """
    text = answer or ""
    idx = text.find(heading)
    if idx == -1:
        return text.strip()
    return text[idx + len(heading):].strip()


def _format_dialogue_transcript_for_prompt(transcript: list) -> str:
    lines = []
    for utterance in transcript:
        role_ja = _DIALOGUE_ROLE_LABEL_JA.get(utterance["role"], utterance["role"])
        lines.append(f"[{utterance['speaker_label']}さん・{role_ja}・ラウンド{utterance['round']}]")
        lines.append(utterance["content"] or "(応答なし)")
        lines.append("")
    return "\n".join(lines).strip()


def _summarize_dialogue(status: str, transcript: list, final_content: str, human_message: str) -> str:
    """要約(サマリー)を組み立てる。

    仮の判断: 要約専用に追加のLLM問い合わせを行うと、対話プロトコル
    自体が消費する発言回数の枠(セーフティリミット)を圧迫してしまう。
    統合役が最後に述べた内容(最終合意内容、または未合意の場合はその
    時点の要約・人間への確認事項)をそのまま要約として再利用すること
    で、追加の問い合わせを発生させない。
    """
    if status == DIALOGUE_STATUS_CONSENSUS:
        return f"{len(transcript)}件の発言を経て合意に達しました。\n\n{final_content or ''}".strip()
    if status == DIALOGUE_STATUS_NEEDS_HUMAN:
        return (human_message or "合意に至りませんでした。").strip()
    if status == DIALOGUE_STATUS_SAFETY_LIMIT:
        last_content = transcript[-1]["content"] if transcript else ""
        base = human_message or f"発言回数の合計が上限({DIALOGUE_SAFETY_LIMIT_UTTERANCES}回)に達したため一時停止しました。"
        return f"{base}\n\n直近の発言:\n{last_content}".strip() if last_content else base
    return "議論への応答が得られませんでした。"


def _finish_dialogue(status: str, transcript: list, total_utterances: int, final_content: str, human_message: str) -> dict:
    return {
        "status": status,
        "transcript": transcript,
        "summary": _summarize_dialogue(status, transcript, final_content, human_message),
        "final_content": final_content,
        "human_message": human_message,
        "total_utterances": total_utterances,
    }


def _run_dialogue(
    org_fingerprint: str, topic: str, background: str, candidates: list, output_instruction: str,
    print_lock: threading.Lock = None, tag: str = None, min_rounds: int = 2, max_rounds: int = 8,
    resume_transcript: list = None, resume_round: int = 1,
) -> dict:
    """複数ノードが役割(提案役・反論役・統合役)を持って議論し、合意形成
    する対話プロトコルの中核。寄合全体で使い回せる共通基盤として、計画
    フェーズ・修正フェーズ・レビューフェーズ・計画のみモードから共通で
    呼び出される。

    ラウンド数は固定せず、統合役が「判定: 合意」(かつ反論役も「評価:
    合意」)と判断し、`min_rounds`以上のラウンドを経ている場合に終了する。
    暴走防止のため、全ノードの発言回数の合計が
    `DIALOGUE_SAFETY_LIMIT_UTTERANCES`を超えた時点で強制的に一時停止する。
    どちらの終了条件でも、統合役(または反論役)が「人間に確認」したい
    事項があると判断した場合は、その時点で打ち切って人間に判断を仰ぐ。

    戻り値は次のキーを持つ辞書:
    - "status": DIALOGUE_STATUS_* のいずれか
    - "transcript": [{"round", "role", "speaker_label", "content"}, ...]
      (議事録。誰が・どの役割で・何を発言したかを発言順に保持する)
    - "summary": 短い要約(人間が素早く全体像を把握するためのもの)
    - "final_content": 合意に達した場合の最終合意内容(それ以外はNone)
    - "human_message": 人間への確認事項がある場合のメッセージ(それ以外はNone)
    - "total_utterances": 実際に行われた発言(問い合わせ)の回数

    ログ(議事録)・要約の実際のファイルへの永続化は、この関数自身では
    行わない(呼び出し元が`_write_dialogue_log`/`_write_dialogue_summary`
    を使って、用途に応じた保存先に書き出す)。

    `resume_transcript`/`resume_round`(既定はNone/1、通常の新規対話)を
    渡すと、以前一時停止した対話の議事録をそのまま引き継ぎ、指定した
    ラウンド番号から続きとして再開する(一時停止直後に人間の回答を得た
    場合の再開ルーティング用。提案役・反論役・統合役のやり取りや合意
    判定のロジック自体はここを起点に始まる各ラウンドの処理としてそのまま
    再利用され、変更しない)。
    """
    roles = _assign_discourse_roles(candidates)
    if not roles:
        return _finish_dialogue(DIALOGUE_STATUS_NO_ENGAGEMENT, [], 0, None, None)

    transcript = list(resume_transcript) if resume_transcript else []
    state = {"total_utterances": 0, "any_real_content": False}

    def speak(role_key: str, prompt: str, round_num: int) -> str:
        candidate = roles[role_key]
        answer, _error, _truncated = _collect_answer_from_candidate(
            candidate, org_fingerprint, [{"role": "user", "content": prompt}],
        )
        state["total_utterances"] += 1
        answer = (answer or "").strip()
        if answer:
            state["any_real_content"] = True
        role_ja = _DIALOGUE_ROLE_LABEL_JA[role_key]
        _print_tagged(
            print_lock, tag or topic,
            f"[💬 {candidate['label']}さん({role_ja}・ラウンド{round_num})] "
            + (answer if answer else "(応答がありませんでした)"),
        )
        transcript.append({
            "round": round_num, "role": role_key, "speaker_label": candidate["label"], "content": answer,
        })
        return answer

    round_num = resume_round
    while True:
        if state["total_utterances"] >= DIALOGUE_SAFETY_LIMIT_UTTERANCES:
            return _finish_dialogue(DIALOGUE_STATUS_SAFETY_LIMIT, transcript, state["total_utterances"], None, None)

        if round_num == 1:
            proposer_prompt = _DIALOGUE_PROPOSER_ROUND1_TEMPLATE.format(
                speaker_label=roles[DIALOGUE_ROLE_PROPOSER]["label"], topic=topic, background=background,
                output_instruction=output_instruction,
            )
        else:
            proposer_prompt = _DIALOGUE_PROPOSER_REVISE_TEMPLATE.format(
                speaker_label=roles[DIALOGUE_ROLE_PROPOSER]["label"], topic=topic, background=background,
                transcript=_format_dialogue_transcript_for_prompt(transcript), output_instruction=output_instruction,
            )
        speak(DIALOGUE_ROLE_PROPOSER, proposer_prompt, round_num)
        if not state["any_real_content"]:
            # 提案役からすら一度も実のある応答が得られていない場合、
            # これ以上反論役・統合役に問い合わせても無駄になる可能性が
            # 高いため、早期に切り上げる(疎通の問題であって議論の
            # 結果ではないため、呼び出し元は安全側フォールバックしてよい)。
            return _finish_dialogue(DIALOGUE_STATUS_NO_ENGAGEMENT, transcript, state["total_utterances"], None, None)
        if state["total_utterances"] >= DIALOGUE_SAFETY_LIMIT_UTTERANCES:
            return _finish_dialogue(DIALOGUE_STATUS_SAFETY_LIMIT, transcript, state["total_utterances"], None, None)

        critic_prompt = _DIALOGUE_CRITIC_TEMPLATE.format(
            speaker_label=roles[DIALOGUE_ROLE_CRITIC]["label"], topic=topic, background=background,
            transcript=_format_dialogue_transcript_for_prompt(transcript),
        )
        critic_answer = speak(DIALOGUE_ROLE_CRITIC, critic_prompt, round_num)
        critic_verdict = _parse_critic_verdict(critic_answer)
        if state["total_utterances"] >= DIALOGUE_SAFETY_LIMIT_UTTERANCES:
            return _finish_dialogue(DIALOGUE_STATUS_SAFETY_LIMIT, transcript, state["total_utterances"], None, None)

        integrator_prompt = _DIALOGUE_INTEGRATOR_TEMPLATE.format(
            speaker_label=roles[DIALOGUE_ROLE_INTEGRATOR]["label"], topic=topic, background=background,
            transcript=_format_dialogue_transcript_for_prompt(transcript), output_instruction=output_instruction,
        )
        integrator_answer = speak(DIALOGUE_ROLE_INTEGRATOR, integrator_prompt, round_num)
        integrator_verdict = _parse_integrator_verdict(integrator_answer)

        if critic_verdict == "情報不足" or integrator_verdict == "人間に確認":
            human_message = _extract_dialogue_section(integrator_answer, "人間への確認事項:")
            return _finish_dialogue(DIALOGUE_STATUS_NEEDS_HUMAN, transcript, state["total_utterances"], None, human_message)

        if integrator_verdict == "合意" and critic_verdict == "合意" and round_num >= min_rounds:
            final_content = _extract_dialogue_section(integrator_answer, "最終合意内容:")
            return _finish_dialogue(DIALOGUE_STATUS_CONSENSUS, transcript, state["total_utterances"], final_content, None)

        round_num += 1
        if round_num > max_rounds:
            return _finish_dialogue(
                DIALOGUE_STATUS_NEEDS_HUMAN, transcript, state["total_utterances"], None,
                "議論を重ねましたが、まだアイデアが不足しており合意に至りませんでした。アドバイスをください。",
            )


def _format_dialogue_log_markdown(topic: str, transcript: list) -> str:
    """議事録(逐語ログ)を組み立てる。誰が・どの役割で・何を発言したかを
    発言順にすべて保持する(後から「なぜこの結論になったのか」を遡れる
    ようにするための、要約とは別に必ず保存する記録)。
    """
    lines = [f"# 対話ログ: {topic}", ""]
    for utterance in transcript:
        role_ja = _DIALOGUE_ROLE_LABEL_JA.get(utterance["role"], utterance["role"])
        lines.append(f"## ラウンド{utterance['round']} - {utterance['speaker_label']}さん({role_ja})")
        lines.append("")
        lines.append(utterance["content"] or "(応答なし)")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _format_dialogue_summary_markdown(topic: str, result: dict) -> str:
    lines = [
        f"# 対話サマリー: {topic}", "",
        "## ステータス", "", _DIALOGUE_STATUS_LABEL_JA.get(result["status"], result["status"]), "",
        "## 要約", "", result["summary"] or "(要約なし)", "",
    ]
    if result.get("human_message"):
        lines += ["## 人間への確認事項", "", result["human_message"], ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _write_dialogue_log(project_dir: str, dialogue_id: str, topic: str, transcript: list) -> str:
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, f"DIALOGUE_LOG_{dialogue_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_format_dialogue_log_markdown(topic, transcript))
    return path


def _write_dialogue_summary(project_dir: str, dialogue_id: str, topic: str, result: dict) -> str:
    os.makedirs(project_dir, exist_ok=True)
    path = os.path.join(project_dir, f"DIALOGUE_SUMMARY_{dialogue_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_format_dialogue_summary_markdown(topic, result))
    return path


# 仮の判断(平文からの自動ツール選択の復活への対応): 対話プロトコルが
# 合意に至らず一時停止した(セーフティリミット・人間の確認が必要)場合に
# 共通で表示するこの一言を、`_run_repl_client`側でも「直前の会話履歴が
# 一時停止の報告だったかどうか」を判定するための目印として使う
# (`_last_turn_is_dialogue_pause`を参照)。文言を変更する場合は両方の
# 参照箇所を同時に更新すること。
_DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER = (
    "[👤 続行するか終了するかは人間の判断です。内容を確認のうえ、必要であれば依頼文を調整して再度お試しください]"
)


def _report_dialogue_result(result: dict, project_dir: str, dialogue_id: str, topic: str) -> None:
    """対話の結果を画面に表示し、議事録・要約を(与えられた場合は)
    ディスクに保存する共通処理。

    合意に達した場合(DIALOGUE_STATUS_CONSENSUS)以外は、それまでの
    議事録全文と要約を人間に提示し、続行するか終了するかの判断を
    人間に委ねる(対話プロトコルだけで次のフェーズへ自動的に進む
    ことはしない)。
    """
    if result["status"] == DIALOGUE_STATUS_NO_ENGAGEMENT:
        return
    if project_dir:
        log_path = _write_dialogue_log(project_dir, dialogue_id, topic, result["transcript"])
        summary_path = _write_dialogue_summary(project_dir, dialogue_id, topic, result)
        print(f"[📜 対話の議事録を保存しました: {log_path}]")
        print(f"[📄 対話の要約を保存しました: {summary_path}]")
    if result["status"] == DIALOGUE_STATUS_CONSENSUS:
        print(f"[🤝 対話プロトコル: {result['total_utterances']}件の発言を経て合意に達しました]")
        return
    if result["status"] == DIALOGUE_STATUS_SAFETY_LIMIT:
        print(f"[⏸️ 対話プロトコル: {_DIALOGUE_STATUS_LABEL_JA[DIALOGUE_STATUS_SAFETY_LIMIT]}]")
    else:
        print("[🙋 対話プロトコル: 合意に至らず、人間の確認が必要です]")
    if result.get("human_message"):
        print(result["human_message"])
    print(_DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)


# ---------------------------------------------------------------------------
# 制作依頼の事前すり合わせ("//agree"コマンド・協業モード、実験2)
# ---------------------------------------------------------------------------
#
# 実験1で、複数メンバーに別々のファイルを実装させると、依頼文にインター
# フェースを書いても関数名・データ形式がズレる不具合が実機で見つかった
# (storage.py側はadd_todo等の仕様通りの名前だったが、cli.py側はadd等の
# 汎用的な名前でimportしており連携が失敗した)。//agree は、実装を並行
# させる前に「モジュール分割・インターフェース設計」をまず1台に考えさせ、
# その合意内容をそのまま各メンバーへの依頼文に含めることで、この不一致を
# 減らすことを狙った仕組み。

AGREE_COMMAND = "//agree"

# 対話プロトコルを使い、成果物(コード等)は作らず計画だけを出力する単体
# モード(依頼の「計画のみを出す単体モード」)。既存の新規作成モード
# (//agree)・修正モード(//fix)とは独立したコマンドとして実装する。
PLAN_ONLY_COMMAND = "//plan-only"

# 仮の判断: 設計担当への指示は、出力形式をできるだけ単純な1行1ファイル形式に
# 寄せるために日本語のプロンプトテンプレートとして持つ。ただし実際には
# モデルがこの形式を厳密に守るとは限らず、ファイル名を見出しとして詳細を
# 複数行にぶら下げる階層的な形式で返してくることもあるため、解析側
# (`_parse_module_breakdown`)はそれも扱えるようにしてある。
_MODULE_BREAKDOWN_PROMPT_TEMPLATE = """あなたはソフトウェア設計を担当します。以下の依頼を、複数人で分担して実装できるよう、複数のファイルに分割する実装計画を考えてください。

{language_instruction}

各ファイルが実装すべき内容には、他のファイルから呼び出される関数のシグネチャ(関数名・引数名・型・戻り値の型)や、やり取りするデータの形式(辞書のキー名など)を具体的に明記してください。担当が異なるファイル同士が正しく連携できるよう、インターフェースの記述は曖昧にせず、できるだけ厳密に書いてください。

重要: 複数のファイルの説明に同じ関数(例: あるファイルが呼び出す、別のファイルが定義する関数)が登場する場合、その関数名・引数名・戻り値の型は、すべてのファイルの説明文で一字一句まったく同じ表記に揃えてください。あるファイルの説明では`add_todo`、別のファイルの説明では`add_task`のように、同じ役割の関数に別々の名前を使うことは厳禁です。

さらに重要: あるファイルの説明の中で、他のファイル(スクリプトの読み込み・スタイルシートの参照・importなど)に言及する場合、そのファイル名は、あなたがこの計画の中で実際に別の1行として挙げるファイル名と一字一句完全に一致させてください。あなたの説明の中にしか登場しない、実際にはファイルとして挙げていない架空のファイル名(例: 別のファイルで`editor.js`と`preview.js`を挙げているのに、説明文の中では両方をまとめて`script.js`と呼ぶ、のような食い違い)を、他のファイルから読み込む・呼び出すものとして書いてはいけません。

出力は次の形式のみとし、他の説明文や前置き・後書きは一切含めないでください。ファイルごとに1行、「ファイル名: 実装すべき内容(インターフェースの詳細を含む)」という形式で出力してください(2〜4ファイル程度を想定します。ファイル名の拡張子は、上で指定した言語に合わせてください):

<ファイル名1>: <実装すべき内容をここに>
<ファイル名2>: <実装すべき内容をここに(<ファイル名1>の行と完全に同じ関数名を使うこと)>

依頼内容: {request}
"""

# 仮の判断: 依頼の項目1(合意フェーズでの言語判定)への対応。ユーザーが
# 依頼文中で言語・技術を明示した場合はそれを最優先で設計担当への指示に
# 埋め込む。明示が無い場合、「どんな依頼にどの言語が最適か」という判断は
# 単純なキーワード判定では対応しきれないほど曖昧なため(既存の
# _classify_execution_mode等とは性質が異なる)、Yoriai側で分類器を持たず
# 設計担当(LLM)自身の判断に委ねる。
#
# 以前は例として"storage.py"/"cli.py"という具体的なPython向けファイル名を
# プロンプトに直接書いていたが、これが設計担当をPython風のファイル分割に
# 引きずってしまう一因になっていた(具体例は指示文以上にモデルの出力を
# 支配しやすい)ため、`<ファイル名1>`のような言語非依存のプレースホルダーに
# 置き換えた。
_EXPLICIT_LANGUAGE_KEYWORDS = (
    # 長い/具体的なキーワードほど先に判定する
    # (例: "JavaScript"は"Java"の判定より先に確認する)。
    ("C++", "C++"),
    ("C言語", "C"),
    ("HTML", "HTML/CSS/JavaScript"),
    ("CSS", "HTML/CSS/JavaScript"),
    ("JavaScript", "HTML/CSS/JavaScript"),
    ("Java", "Java"),
    ("Python", "Python"),
    ("Go言語", "Go"),
    ("Rust", "Rust"),
    ("Ruby", "Ruby"),
    ("PHP", "PHP"),
)


def _detect_requested_language(request_text: str) -> str:
    """依頼文中に明示的な言語・技術の指定があれば、その表記を返す。
    無ければ空文字列を返す(この場合、設計担当自身の判断に委ねる)。
    """
    lowered = (request_text or "").lower()
    for keyword, label in _EXPLICIT_LANGUAGE_KEYWORDS:
        if keyword.lower() in lowered:
            return label
    return ""


def _build_module_breakdown_prompt(request: str) -> str:
    requested_language = _detect_requested_language(request)
    if requested_language:
        language_instruction = (
            f"必ず{requested_language}を使って実装してください。ファイルの拡張子もこの言語に合わせてください。"
        )
    else:
        language_instruction = (
            "依頼内容から、実装に最も適した言語・技術を判断してください"
            "(Pythonとは限りません。HTML/CSS/JavaScript・C言語等、依頼の内容に応じて適切なものを選んでください)。"
            "ファイルの拡張子もその言語に合わせてください。"
        )
    return _MODULE_BREAKDOWN_PROMPT_TEMPLATE.format(request=request, language_instruction=language_instruction)


def _build_design_dialogue_background(request: str) -> str:
    return (
        "以下の制作依頼について、複数人で分担して実装できるよう、複数のファイルに分割する"
        f"実装計画を検討しています。\n\n依頼内容: {request}"
    )


def _build_design_dialogue_output_instruction(request: str) -> str:
    """対話プロトコルの提案役・統合役に渡す、計画の出力形式についての
    指示。既存の`_MODULE_BREAKDOWN_PROMPT_TEMPLATE`の言語判定・出力形式の
    指示をそのまま踏襲し、統合役が出す「最終合意内容」が既存の
    `_parse_module_breakdown`でそのまま解析できる形式になるようにする。
    """
    requested_language = _detect_requested_language(request)
    if requested_language:
        language_instruction = f"必ず{requested_language}を使って実装してください。ファイルの拡張子もこの言語に合わせてください。"
    else:
        language_instruction = (
            "依頼内容から、実装に最も適した言語・技術を判断してください"
            "(Pythonとは限りません。HTML/CSS/JavaScript・C言語等、依頼の内容に応じて適切なものを選んでください)。"
            "ファイルの拡張子もその言語に合わせてください。"
        )
    return (
        f"{language_instruction} 各ファイルが実装すべき内容には、他のファイルから呼び出される関数のシグネチャ"
        "(関数名・引数名・型・戻り値の型)や、やり取りするデータの形式(辞書のキー名など)を具体的に明記してください。"
        "複数のファイルの説明に同じ関数が登場する場合、その関数名・引数名・戻り値の型はすべてのファイルの説明文で"
        "一字一句まったく同じ表記に揃えてください。\n\n"
        "出力は次の形式のみとし、他の説明文や前置き・後書きは一切含めないでください。ファイルごとに1行、"
        "「ファイル名: 実装すべき内容(インターフェースの詳細を含む)」という形式で出力してください"
        "(2〜4ファイル程度を想定します):\n"
        "<ファイル名1>: <実装すべき内容をここに>\n"
        "<ファイル名2>: <実装すべき内容をここに(<ファイル名1>の行と完全に同じ関数名を使うこと)>"
    )


def _run_design_dialogue(org_fingerprint: str, request: str, candidates: list, project_dir: str) -> tuple:
    """//agreeの合意フェーズを、対話プロトコル(`_run_dialogue`)による
    複数ノード・複数ラウンドの合意形成に置き換える(依頼の「計画フェーズ
    での利用」)。戻り値は`(合意した計画テキスト, 中断すべきかどうか, 対話の
    生の結果`_run_dialogue`の戻り値)`。3つ目の値は、一時停止(中断)した
    場合に人間の回答を受けて対話を再開する`_resume_design_dialogue`が、
    それまでの議事録・要約を引き継ぐために使う。

    合意に至らなかった場合(セーフティリミット・人間の確認が必要)は、
    議事録・要約をディスクに保存したうえで中断を指示する(呼び出し元は
    実装フェーズに進まない)。対話プロトコルだけで次のフェーズへ自動的に
    進むことはしないという要件のため、最終的な続行判断は人間に委ねる。
    """
    architect = candidates[0]
    print(
        f"[🧭 対話プロトコルによる合意フェーズ開始: {architect['label']}さんを中心に、"
        "モジュール分割案とインターフェース設計について議論します...]"
    )
    result = _run_dialogue(
        org_fingerprint=org_fingerprint,
        topic=f"制作依頼「{request}」のモジュール分割・インターフェース設計",
        background=_build_design_dialogue_background(request),
        candidates=candidates,
        output_instruction=_build_design_dialogue_output_instruction(request),
        tag="設計",
    )
    _report_dialogue_result(result, project_dir, "design", "モジュール分割・インターフェース設計")

    if result["status"] == DIALOGUE_STATUS_NO_ENGAGEMENT:
        print("設計担当から応答が得られませんでした。")
        return "", True, result
    if result["status"] != DIALOGUE_STATUS_CONSENSUS:
        return "", True, result

    final_content = result["final_content"] or ""
    print("[🧭 対話プロトコルで合意した計画(そのまま表示)]")
    print(final_content)
    return final_content, False, result


# 仮の判断(不具合修正: 対話プロトコル一時停止後、実装フェーズに繋がらない
# 問題への対応): 一時停止(セーフティリミット・人間に確認)した合意フェーズに
# 続く人間の発言は、`_run_dialogue`へそれまでの議事録とラウンド番号を渡して
# 「続きから」再開する。ここで対話プロトコル自体のロジック(提案役・反論役・
# 統合役のやり取り、ラウンドの進み方)には一切手を入れず、`_run_dialogue`
# 側に用意した再開用の引数(`resume_transcript`/`resume_round`)を渡すだけに
# とどめる。
_DIALOGUE_RESUME_EXTRA_ROUNDS = 4


def _build_design_dialogue_resume_background(request: str, prior_result: dict, human_reply: str) -> str:
    background = _build_design_dialogue_background(request)
    if prior_result.get("summary"):
        background += f"\n\n【一時停止までの議論の要約】\n{prior_result['summary']}"
    if prior_result.get("human_message"):
        background += f"\n\n【人間に確認したかった事項】\n{prior_result['human_message']}"
    background += f"\n\n【人間からの回答】\n{human_reply}"
    return background


def _resume_design_dialogue(pending: "_PendingDesignDialogue", human_reply: str) -> tuple:
    """一時停止した//agreeの合意フェーズ(対話プロトコル)を、人間の回答を
    踏まえて続きから再開する。戻り値は`(計画テキスト, 中断すべきかどうか)`。

    `_run_dialogue`に一時停止までの議事録(`pending.result["transcript"]`)と
    続きのラウンド番号を渡すだけで「続きから」の再開を実現し、対話プロト
    コル自体のラウンド進行ロジックは変更しない。

    再開後もなお合意に至らなかった場合(それでも人間に確認したい・セーフ
    ティリミットに再度到達した等)は、これ以上一時停止を人間との往復で
    重ねることはせず、それまでの議論と人間の回答を踏まえて設計担当1名が
    単独で計画を確定させ、実装フェーズへ進む(要件: 既に議論が十分尽くされ
    ている場合は対話フェーズを再開せずそのままEXECUTION_MODE_COLLABORATE
    相当の実装フェーズへ直接進む、に相当する扱い)。応答が一切得られな
    かった場合(`DIALOGUE_STATUS_NO_ENGAGEMENT`)のみ、中断を指示する。
    """
    request = pending.request
    candidates = pending.candidates
    project_dir = pending.project_dir
    prior_result = pending.result
    architect = candidates[0]

    prior_transcript = prior_result.get("transcript") or []
    resume_round = (prior_transcript[-1]["round"] if prior_transcript else 0) + 1
    print(f"[🧭 対話プロトコルを再開します: 人間の回答を踏まえてラウンド{resume_round}から続けます...]")
    result = _run_dialogue(
        org_fingerprint=pending.org_fingerprint,
        topic=f"制作依頼「{request}」のモジュール分割・インターフェース設計",
        background=_build_design_dialogue_resume_background(request, prior_result, human_reply),
        candidates=candidates,
        output_instruction=_build_design_dialogue_output_instruction(request),
        tag="設計",
        min_rounds=1,
        max_rounds=resume_round + _DIALOGUE_RESUME_EXTRA_ROUNDS,
        resume_transcript=prior_transcript,
        resume_round=resume_round,
    )
    _report_dialogue_result(result, project_dir, "design", "モジュール分割・インターフェース設計(再開)")

    if result["status"] == DIALOGUE_STATUS_CONSENSUS:
        final_content = result["final_content"] or ""
        print("[🧭 対話プロトコルで合意した計画(そのまま表示)]")
        print(final_content)
        return final_content, False

    if result["status"] == DIALOGUE_STATUS_NO_ENGAGEMENT:
        print("設計担当から応答が得られませんでした。")
        return "", True

    print(
        f"[🧭 再開後も合意には至りませんでしたが、{architect['label']}さんが人間の回答を踏まえて"
        "計画を単独で確定し、実装フェーズへ進みます...]"
    )
    fallback_prompt = (
        f"{_build_module_breakdown_prompt(request)}\n\n"
        f"【これまでの議論の経緯】\n{_format_dialogue_transcript_for_prompt(result['transcript'])}\n\n"
        f"【人間からの回答】\n{human_reply}\n\n"
        "上記を踏まえて、あなた一人の判断でファイル分割案を確定してください。"
    )
    answer, error, truncated = _collect_answer_from_candidate(
        architect, pending.org_fingerprint, [{"role": "user", "content": fallback_prompt}],
    )
    if error or not answer:
        print(f"設計担当への問い合わせに失敗しました: {error or '応答がありませんでした'}")
        return "", True
    if truncated:
        print(f"[⚠️ 設計担当の応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました。以下は不完全な内容の可能性があります]")
    print(f"[🧭 {architect['label']} の回答(単独での計画確定、そのまま表示)]")
    print(answer)
    return answer, False


def _resume_organization_collaborate(pending: "_PendingDesignDialogue", human_reply: str) -> None:
    """一時停止した//agreeの合意フェーズへの人間の回答を受け取り、対話
    プロトコルを続きから再開したうえで、合意(または単独での計画確定)が
    得られ次第そのままタスク分解・実装フェーズへ橋渡しする。

    橋渡し先(`_run_collaborate_implementation_phase`)は、新規の`//agree`が
    一発で合意に達した場合(`_ask_organization_collaborate`)と完全に同じ
    関数であり、一時停止からの再開でも既存の正常系と同じ実装フェーズに
    合流する。
    """
    answer, aborted = _resume_design_dialogue(pending, human_reply)
    if aborted:
        return
    _run_collaborate_implementation_phase(
        pending.request, answer, pending.candidates, pending.org_fingerprint, pending.project_dir, pending.port,
    )


def _build_plan_only_output_instruction() -> str:
    return (
        "計画は人間が読んで判断するためのものです。次の観点を含めて、箇条書き中心に具体的にまとめてください:\n"
        "- 全体方針・アプローチ\n"
        "- 主要な作業項目・手順(ファイル分割案がある場合はファイル名ごとに)\n"
        "- 想定されるリスク・懸念点\n"
        "- 決めきれなかった点・人間に確認したいことがあれば明記する\n\n"
        "他の説明文や前置きは不要です。上記の観点に沿った計画の本文のみを出力してください。"
    )


def _ask_organization_plan_only(port: int, org_fingerprint: str, request: str, out_dir: str) -> None:
    """成果物(コード等)を実際に作る・作らないに関わらず、対話プロトコル
    だけを使って計画を出力する単体モード(`//plan-only`、依頼の「計画のみ
    を出す単体モード」)。実装フェーズには一切進まず、対話の結果としての
    計画書(要約+議事録)を人間に提示して終了する。既存の新規作成モード
    (//agree)・修正モード(//fix)とは独立したコマンドとして実装する。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return

    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    # 仮の判断: 計画のみモードは実装を行わないため、専用のプロジェクト
    # ディレクトリ名に"-plan"を付け、同名の//agreeによる実装プロジェクトと
    # 区別できるようにする(議事録・要約だけが保存され、コード等の
    # 成果物は一切生成されない)。
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    project_name = _project_name_with_date_prefix(request, suffix="-plan")
    project_dir = _resolve_project_dir(projects_root, project_name)

    architect = candidates[0]
    print(
        f"[🧭 計画のみモード開始: {architect['label']}さんを中心に、対話プロトコルで"
        "計画について議論します(成果物の実装は行いません)...]"
    )
    result = _run_dialogue(
        org_fingerprint=org_fingerprint,
        topic=request,
        background=f"以下の依頼について、実装には進まず計画のみを検討しています。\n\n依頼内容: {request}",
        candidates=candidates,
        output_instruction=_build_plan_only_output_instruction(),
        tag="計画",
    )
    _report_dialogue_result(result, project_dir, "plan", request)

    if result["status"] == DIALOGUE_STATUS_NO_ENGAGEMENT:
        print("計画についての応答が得られませんでした。")
        return
    if result["status"] == DIALOGUE_STATUS_CONSENSUS:
        print("[🧭 対話プロトコルでまとまった計画(そのまま表示)]")
        print(result["final_content"] or "")


# 仮の判断: PROGRESS.mdに記録する「使用言語」は、依頼文から推測した言語
# (_detect_requested_language、設計担当への「ヒント」に過ぎない)ではなく、
# 実際に設計担当が提案したファイルの拡張子から逆算する。依頼文の推測と
# 設計担当の実際の応答が食い違う可能性は排除できないため、「実際に何が
# 生成されるか」を正とすることで、記録された言語設定と実ファイルとの
# 食い違いを防ぐ。
_EXTENSION_TO_LANGUAGE = {
    ".py": "Python",
    ".html": "HTML/CSS/JavaScript", ".htm": "HTML/CSS/JavaScript",
    ".css": "HTML/CSS/JavaScript", ".js": "HTML/CSS/JavaScript",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".php": "PHP",
}


def _infer_language_from_tasks(tasks: list) -> str:
    """モジュール分割案のファイル名一覧から、使用言語を逆算する。複数の
    言語にまたがる拡張子が混在する場合は、重複を除いて登場順に連結する
    (例: index.html/style.css/app.jsが混在する典型的な構成は、まとめて
    "HTML/CSS/JavaScript"という1つのラベルに集約される)。認識できる
    拡張子が1つも無い場合は、この機能追加より前からの既定挙動(構文
    チェックは常にPythonとして扱っていた)を踏襲し"Python"を返す。
    """
    labels = []
    for filename, _content in tasks:
        _root, ext = os.path.splitext(filename)
        label = _EXTENSION_TO_LANGUAGE.get(ext.lower())
        if label and label not in labels:
            labels.append(label)
    return "/".join(labels) if labels else "Python"


# 仮の判断: 各ファイルの実装担当には、そのファイル自身の説明行だけでなく、
# 設計担当が決めた「実装計画全体」もそのまま埋め込んで渡す。担当ファイルの
# 説明行だけを渡す実装だと、たとえ設計担当の回答内で関数名が一致していても、
# 実装担当のモデル自身が独自の名前で書き始めてしまうケースを防げない
# (実機で、storage.py担当とcli.py担当が異なる関数名を使ってしまう不具合が
# 実際に発生した)。全ファイルの説明を毎回見せることで、担当外のファイルが
# 期待する関数名にも実装時点で気づける可能性を高める。
_COLLABORATIVE_IMPLEMENTATION_REQUEST_TEMPLATE = """以下は、組織内の設計担当が決めた複数ファイルの実装計画全体です。ファイル間でインターフェース(関数名・引数・戻り値の型・データ構造)が一致するよう、この計画に記載された関数名やデータ形式を一字一句変えずにそのまま使って実装してください。

【実装計画全体】
{full_plan}

【あなたが実装を担当するファイル】
{filename}: {own_content}
"""


def _build_collaborative_implementation_request(filename: str, own_content: str, full_breakdown: list) -> str:
    full_plan = "\n".join(f"{fn}: {content}" for fn, content in full_breakdown)
    return _COLLABORATIVE_IMPLEMENTATION_REQUEST_TEMPLATE.format(
        full_plan=full_plan, filename=filename, own_content=own_content,
    )


# 仮の判断: 設計担当の回答を解析する際、`//parallel`用の`_PARALLEL_TASK_PATTERN`
# (":"の前を無条件にファイル名として扱う)をそのまま流用すると、設計担当が
# 「ファイル名を見出しとし、その配下に関数シグネチャを箇条書きでぶら下げる」
# という階層的な形式で回答してきた場合に、見出し配下の詳細行(関数シグネチャ等)
# までファイル名として誤認識してしまう(実機で、storage.py/cli.pyの2ファイルの
# はずが、関数シグネチャまで含めて9項目のフラットなリストとして解析され、
# 本来ファイルではない行が実装依頼の宛先にされてしまう不具合が発生した)。
#
# これはToDoリストというお題に限った問題ではなく、「見出し(ファイル)」と
# 「詳細(そのファイルの内部要素)」を区別せずに全行を同列に解析するという、
# パース処理そのものの設計上の問題である。そのため、「見出し行として扱って
# よいのは、拡張子付きの裸のファイル名(空白・括弧・矢印等を含まない)が
# コロンの直前にある行に限る」という、ファイル名の形そのものに基づく汎用的な
# 判定に変更した。関数シグネチャの行(例: `load_todos() -> list: 説明`)は
# 括弧を含むため見出しとは判定されず、直前の見出し(ファイル)の詳細として
# まとめられる。
_FILE_HEADER_PATTERN = re.compile(r"^([\w./\-]+\.[A-Za-z0-9]+):\s*(.*)$")


def _parse_module_breakdown(text: str) -> list:
    """設計担当メンバーの回答を [(ファイル名, 内容), ...] のリストに変換する。

    対応する形式:
    - フラットな1行1ファイル形式(例: `storage.py: <実装内容>`)
    - 階層的な形式(例: `storage.py:` を見出し行とし、その配下に
      `- load_todos() -> list: ...` のような関数シグネチャ等の詳細を
      複数行の箇条書きでぶら下げる形式)

    見出し行かどうかは、コロンの直前が「拡張子付きの裸のファイル名」
    (空白・括弧・矢印等を含まない)であるかどうかだけで判定する
    (`_FILE_HEADER_PATTERN`)。見出しに該当しない行は、直前に出てきた
    見出し(ファイル)の詳細としてまとめる。実行単位は常にファイル単位を
    保つ(ファイルより細かい粒度には分割しない)。
    """
    # tasks: [[ファイル名, [詳細行, ...]], ...]
    tasks = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("```"):
            continue
        # 仮の判断: 箇条書き記号("- "等)やMarkdown見出し記号("#"等)、
        # バッククォートは見出し判定の妨げになるだけなので取り除く。
        stripped = line.lstrip("-*・# ").strip().replace("`", "")
        if not stripped:
            continue

        match = _FILE_HEADER_PATTERN.match(stripped)
        if match:
            filename = match.group(1).strip()
            inline_content = match.group(2).strip()
            tasks.append([filename, [inline_content] if inline_content else []])
        elif tasks:
            # 直前に見出しが1つも無い場合(設計担当の前置き文など)は無視する。
            tasks[-1][1].append(stripped)

    return [
        (filename, "\n".join(details))
        for filename, details in tasks
        if details  # 詳細が全く無いファイル(見出しだけ)は実装依頼のしようがないため除外
    ]


# 仮の判断(バグ報告への対応): 設計担当は、ある1ファイルの説明文の中で
# 他のファイル(スクリプトの読み込み・スタイルシートの参照等)に言及する際、
# 実際には計画に無い架空のファイル名を使ってしまうことがある(実機で、
# editor.js/preview.jsという2ファイルを計画に挙げていながら、index.htmlの
# 説明文の中では両方をまとめて「script.js」と呼んでしまい、実装された
# index.htmlがscript.jsを読み込むコードになった結果、レビューが
# 「script.jsが存在しない」という指摘を無限に繰り返す不具合が報告された)。
# タスクキューへの割り当て自体は常に計画通りのファイル名で行われる
# (_run_collaborative_task_queueのworkerはtasksタプルのファイル名を
# そのままdest_pathに使う)ため、これはYoriai側の処理の不具合ではなく、
# 設計担当(LLM)の1回の応答内での自己矛盾である。プロンプト側の指示
# (_MODULE_BREAKDOWN_PROMPT_TEMPLATEの「さらに重要」の段落)で予防を
# 図ったが、確率的な性質上完全には防げないため、機械的に検出して
# ユーザーに警告する仕組みも用意する。
# 仮の判断: 文字クラスをASCII(英数字・アンダースコア・ハイフン)に限定する。
# Pythonの`\w`は既定でUnicodeの単語文字(日本語の漢字・かな等も含む)に
# マッチするため、`[\w\-]+`のままだと「scriptタグでscript.js」のような
# 日本語の説明文中で、直前の日本語部分までファイル名の一部として誤って
# 連結して抽出してしまう(実際に発生した誤検出)。実在するファイル名は
# 通常ASCII文字だけで構成されるため、ASCIIに限定しても実用上の取りこぼしは
# 少ないと判断した。
_FILENAME_LIKE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5}")


def _find_undeclared_file_references(tasks: list) -> list:
    """各タスクの説明文中に登場する「ファイル名らしきトークン」
    (`<名前>.<拡張子>`の形)のうち、このプロジェクトが実際に使っている
    拡張子(=他のタスクのいずれかの拡張子)を持ちながら、実際にはどの
    タスクのファイル名とも一字一句一致しないものを探し、
    `[(言及元のファイル名, 未宣言のファイル名らしき文字列), ...]`を返す。

    仮の判断: 説明文中の全ての"x.y"形式トークンをファイル名として扱うと、
    バージョン番号やその他の紛らわしい文字列を誤検出するリスクが高い。
    「このプロジェクトが実際に使っている拡張子」に絞ることで、無関係な
    誤検出を抑えつつ、実用上価値のある検出だけに絞り込む。
    """
    declared_names = {fn for fn, _ in tasks}
    declared_exts = {os.path.splitext(fn)[1].lower() for fn in declared_names if os.path.splitext(fn)[1]}
    issues = []
    for filename, content in tasks:
        for match in _FILENAME_LIKE_TOKEN_PATTERN.finditer(content):
            token = match.group(0)
            if os.path.splitext(token)[1].lower() not in declared_exts:
                continue
            if token in declared_names:
                continue
            issues.append((filename, token))
    return issues


# 仮の判断: 協業モードの生成物をYoriai本体(yoriai.py・config.py等)と
# 同じディレクトリに置くと、どれが本体でどれが生成物か見分けにくく、
# 生成物のファイル名がYoriai本体のファイル名(config.py等)と衝突する
# リスクもある。そのため、生成物は必ず「<--dirで指定したディレクトリ>/
# projects/<プロジェクト名>/」というサブディレクトリにまとめる。
PROJECTS_SUBDIR_NAME = "projects"

# プロジェクト名に使うトークンとして、依頼文中のASCII英数字のまとまり
# (ToDo、CLI、API等、日本語の依頼文に埋め込まれた英語表現)を拾う。
_PROJECT_NAME_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")
_PROJECT_NAME_MAX_TOKENS = 4
_PROJECT_NAME_MAX_LENGTH = 40
_DEFAULT_PROJECT_NAME = "project"


def _generate_project_name(request: str) -> str:
    """依頼文から簡易的なプロジェクト名を生成する。

    仮の判断: 依頼文の内容を厳密に要約するには本来LLMへの問い合わせが
    必要になるが、それではプロジェクト名を決めるためだけに追加の
    ネットワーク往復が発生してしまう。依頼文には「ToDoリスト」
    「CLIツール」のように英語表現がそのまま埋め込まれていることが多い
    ため、ASCII英数字のまとまりを拾ってハイフンで連結するだけの簡易的な
    変換にとどめた(依頼に明記された「厳密な命名規則は不要」という方針
    に沿っている)。英語表現が全く含まれない依頼文の場合は
    `_DEFAULT_PROJECT_NAME`(+一意化のための連番、`_resolve_project_dir`側で
    付与)にフォールバックする。
    """
    tokens = [t.lower() for t in _PROJECT_NAME_TOKEN_PATTERN.findall(request) if len(t) >= 2]
    name = "-".join(tokens[:_PROJECT_NAME_MAX_TOKENS]) if tokens else _DEFAULT_PROJECT_NAME
    return name[:_PROJECT_NAME_MAX_LENGTH]


def _project_name_with_date_prefix(request: str, suffix: str = "") -> str:
    """新規に作るプロジェクトディレクトリの名前を、依頼文由来の名前
    (`_generate_project_name`)の先頭に`YYMMDD`形式の日付を付けた形で
    返す(依頼: プロジェクトフォルダを作るたびに日付が先頭に付くように
    してほしい、への対応)。

    仮の判断: `_generate_project_name`自体には日付を混ぜず、依頼文からの
    命名という既存の役割のまま単体でテスト・再利用できるようにし、日付を
    付けるかどうかは「新規にディレクトリを作る」呼び出し元
    (`_ask_organization_collaborate`・`_ask_organization_plan_only`)側の
    関心事として分離した。日付は生成した瞬間の実時刻を使う(依頼文からの
    推測ではなく、実際にディレクトリを作る時点を正とする)。
    """
    return f"{time.strftime('%y%m%d')}-{_generate_project_name(request)}{suffix}"


def _resolve_project_dir(projects_root: str, name: str) -> str:
    """`projects_root`配下に、`name`をベースにした未使用のディレクトリパスを
    決める。既に同名のディレクトリが存在する場合、既存の生成物を上書き
    しないよう`<name>-2`、`<name>-3`、...のように連番を振る。
    """
    candidate = os.path.join(projects_root, name)
    if not os.path.exists(candidate):
        return candidate
    suffix = 2
    while True:
        candidate = os.path.join(projects_root, f"{name}-{suffix}")
        if not os.path.exists(candidate):
            return candidate
        suffix += 1


# ---------------------------------------------------------------------------
# 組織自身が立てた計画を最後まで完遂させるためのタスクチェックリスト
# ---------------------------------------------------------------------------
#
# 実機で、合意フェーズが「4ファイル構成が適切」と自ら提案したにもかかわらず、
# 実装フェーズで候補不足等によりconfig.pyがスキップされたまま
# 「✅ レビュー完了」と表示されてしまう不具合が見つかった。合意フェーズの
# 構成案は人間が直接指定したものではなく組織自身(設計担当)が生み出した
# 計画だが、「組織側が4つ必要だと判断した以上、その4つを完遂する責任は
# 組織側にある」という前提のもと、合意フェーズが確定した瞬間にその計画を
# 「必ず完遂すべきタスクリスト」として固定し、Claude CodeのTodoWriteと
# 同様に進行状況を追跡したうえで、最後に必ず全タスクの完了を確認してから
# でなければ「完了」を報告しない仕組みを追加した。

_TASK_STATUS_PENDING = "pending"
_TASK_STATUS_IN_PROGRESS = "in_progress"
_TASK_STATUS_COMPLETED = "completed"

_TASK_STATUS_SYMBOLS = {
    _TASK_STATUS_PENDING: "⬜",
    _TASK_STATUS_IN_PROGRESS: "⏳",
    _TASK_STATUS_COMPLETED: "✅",
}


def _build_task_checklist(tasks: list) -> list:
    """合意フェーズで確定したファイル一覧(`tasks`: `[(ファイル名, 内容), ...]`)
    から、ファイルごとに「実装」「レビュー」の2タスクを持つチェックリストを
    作る。各タスクは`{"filename":, "kind": "impl"|"review", "label":, "status":}`
    の辞書で、生成した時点ではすべて`_TASK_STATUS_PENDING`。
    """
    checklist = []
    for filename, _content in tasks:
        checklist.append({"filename": filename, "kind": "impl", "label": f"{filename} の実装", "status": _TASK_STATUS_PENDING})
        checklist.append({"filename": filename, "kind": "review", "label": f"{filename} のレビュー", "status": _TASK_STATUS_PENDING})
    return checklist


def _set_task_status(checklist: list, filename: str, kind: str, status: str) -> None:
    for task in checklist:
        if task["filename"] == filename and task["kind"] == kind:
            task["status"] = status
            return


def _format_checklist_lines(checklist: list) -> list:
    return [f"{_TASK_STATUS_SYMBOLS[task['status']]} {task['label']}" for task in checklist]


def _format_task_checklist(checklist: list) -> str:
    return "\n".join(["[📝 タスク状況(組織が自ら立てた計画)]"] + _format_checklist_lines(checklist))


def _incomplete_task_labels(checklist: list) -> list:
    return [task["label"] for task in checklist if task["status"] != _TASK_STATUS_COMPLETED]


# ---------------------------------------------------------------------------
# 進行状況の永続化(PROGRESS.md)
# ---------------------------------------------------------------------------
#
# 協業モードは合意フェーズ→タスクキュー方式による実装・レビューと複数の
# フェーズにまたがり、実機では数分単位の時間がかかることもある。途中で
# プロセスが終了した場合(--chatを再起動した、非常口で強制終了した等)、
# それまでの進行状況(元の依頼・確定した計画・タスクの状態・直近の
# レビュー指摘)が失われ、最初からやり直すしかなかった。read_fileツール
# (実装依頼元がディスク上のファイルを都度読み直す設計)と同じ「ディスク上の
# 実際の状態を正として扱う」方針に基づき、プロジェクトディレクトリに
# PROGRESS.mdを作成・更新し続けることで、後から(`//resume-all`等で)
# 未完了タスクだけを対象に再開できるようにする。

PROGRESS_FILENAME = "PROGRESS.md"

_PROGRESS_SECTION_REQUEST = "## 元の依頼"
_PROGRESS_SECTION_LANGUAGE = "## 使用言語"
_PROGRESS_SECTION_PLAN = "## モジュール分割案"
_PROGRESS_SECTION_CHECKLIST = "## タスク状況"
_PROGRESS_SECTION_AUTO_RESUME_COUNT = "## 自動再開の試行回数"
_PROGRESS_SECTION_REVIEW = "## 直近のレビュー指摘"
_PROGRESS_SECTION_CHANGELOG = "## 更新履歴"
# 仮の判断(バグ報告への対応): //fixのタスク分割(_run_fix_task_queue)が
# ツール呼び出しの往復回数の上限等で一部のサブタスクを完了させられずに
# 終わった場合、この2節に「元の修正依頼」と「まだ完了していないサブ
# タスクの一覧」を記録する。これが無いと、//resume-allが従来通り
# モジュール分割案のチェックリスト(_PROGRESS_SECTION_CHECKLIST、合意
# フェーズ由来で//fixでは書き換えない)しか見ないため、大規模な//fixが
# 一部のサブタスクを残したまま終わっても「未完了のプロジェクトなし」
# として扱われ、再開する手段が無くなってしまう。
_PROGRESS_SECTION_PENDING_FIX_REQUEST = "## 未完了の修正依頼"
_PROGRESS_SECTION_PENDING_FIX_SUBTASKS = "## 未完了の修正サブタスク"


def _format_progress_markdown(
    request: str, tasks: list, checklist: list, review_feedback: dict, auto_resume_count: int = 0,
    changelog: list = None, language: str = "",
    pending_fix_request: str = "", pending_fix_subtasks: list = None,
) -> str:
    """PROGRESS.mdの内容を組み立てる。

    仮の判断: 「モジュール分割案」セクションは、既存の設計担当への
    問い合わせ結果を表示する際と同じ`f"{filename}: {content}"`という
    フラットな1行1ファイル形式で書き出す。この形式は既存の
    `_parse_module_breakdown`がそのまま解析できるため、再開時
    (`_parse_progress_markdown`)に新しいパーサーを書き起こす必要がなく、
    書き込み・読み込みの両方で実績のある同じコードを再利用できる。

    仮の判断: 「自動再開の試行回数」も、この試行回数だけを内容とする
    シンプルなセクションとしてPROGRESS.mdに直接記録する(別ファイルや
    JSON等の付随データファイルは持たせない)。read_fileツール・
    PROGRESS.md全体で踏襲している「ディスク上の実際の状態を正として
    扱う」方針により、対話モードのプロセスを再起動しても試行回数の
    記録が失われないようにするため。

    仮の判断: 「更新履歴」(既存プロジェクトへの修正依頼の記録)も、
    `"- YYYY-MM-DD: <内容> (<ファイル名>)"`という行の単純なリストとして
    記録する。新しいエントリは末尾に追記されるだけで、既存のエントリを
    書き換えることはない(`_ask_organization_fix_project`参照)。

    仮の判断: 「使用言語」は、合意フェーズが確定したモジュール分割案の
    ファイル拡張子から導出した値(`_infer_language_from_tasks`)を渡す
    (依頼側で明示された言語と、実際に生成されたファイルが食い違う
    リスクを避けるため、常に「実際のファイル」を正とする)。空文字列
    (旧バージョンのPROGRESS.mdや、この節を省きたい呼び出し元)の場合は
    この節自体を出力しない(既存の「直近のレビュー指摘」等と同じ、
    値が無ければ節ごと省略する方針)。
    """
    lines = ["# プロジェクト進行状況", "", _PROGRESS_SECTION_REQUEST, "", request, ""]
    if language:
        lines.append(_PROGRESS_SECTION_LANGUAGE)
        lines.append("")
        lines.append(language)
        lines.append("")
    lines.append(_PROGRESS_SECTION_PLAN)
    lines.append("")
    for filename, content in tasks:
        lines.append(f"{filename}: {content}")
    lines.append("")
    lines.append(_PROGRESS_SECTION_CHECKLIST)
    lines.append("")
    lines.extend(_format_checklist_lines(checklist))
    lines.append("")
    lines.append(_PROGRESS_SECTION_AUTO_RESUME_COUNT)
    lines.append("")
    lines.append(str(auto_resume_count))
    lines.append("")
    if review_feedback:
        lines.append(_PROGRESS_SECTION_REVIEW)
        lines.append("")
        for filename, feedback in review_feedback.items():
            lines.append(f"### {filename}")
            lines.append("")
            lines.append(feedback)
            lines.append("")
    if changelog:
        lines.append(_PROGRESS_SECTION_CHANGELOG)
        lines.append("")
        lines.extend(changelog)
        lines.append("")
    if pending_fix_subtasks:
        lines.append(_PROGRESS_SECTION_PENDING_FIX_REQUEST)
        lines.append("")
        lines.append(pending_fix_request)
        lines.append("")
        lines.append(_PROGRESS_SECTION_PENDING_FIX_SUBTASKS)
        lines.append("")
        lines.extend(f"- {subtask}" for subtask in pending_fix_subtasks)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _write_progress_md(
    project_dir: str, request: str, tasks: list, checklist: list, review_feedback: dict, auto_resume_count: int = 0,
    changelog: list = None, language: str = "",
    pending_fix_request: str = "", pending_fix_subtasks: list = None,
) -> None:
    os.makedirs(project_dir, exist_ok=True)
    content = _format_progress_markdown(
        request, tasks, checklist, review_feedback, auto_resume_count, changelog, language,
        pending_fix_request, pending_fix_subtasks,
    )
    with open(os.path.join(project_dir, PROGRESS_FILENAME), "w", encoding="utf-8") as f:
        f.write(content)


def _extract_progress_section(text: str, heading: str) -> str:
    """`text`(PROGRESS.md全体)から、`heading`(例: "## モジュール分割案")の
    直後から次の"## "見出し(または末尾)までの本文を取り出す。見出しが
    見つからない場合は空文字列を返す。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end]).strip("\n")


# 仮の判断: チェックリストの各行のラベルは`_build_task_checklist`が
# `f"{filename} の実装"`/`f"{filename} のレビュー"`という固定の形式で
# 生成しており、PROGRESS.mdにもこの形式のまま書き出される。再開時は、
# この形式を逆算してfilename/kindを復元する(新しい機械可読フォーマットを
# 別途持たせるのではなく、人間にも読めるこの文言をそのまま構造化データの
# 源として再利用する)。
_CHECKLIST_LABEL_PATTERN = re.compile(r"^(.+) の(実装|レビュー)$")
_CHECKLIST_KIND_FROM_JA = {"実装": "impl", "レビュー": "review"}
_CHECKLIST_STATUS_FROM_SYMBOL = {v: k for k, v in _TASK_STATUS_SYMBOLS.items()}


def _parse_checklist_markdown(text: str) -> list:
    checklist = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        symbol, sep, rest = line.partition(" ")
        if not sep:
            continue
        status = _CHECKLIST_STATUS_FROM_SYMBOL.get(symbol)
        if status is None:
            continue
        match = _CHECKLIST_LABEL_PATTERN.match(rest)
        if not match:
            continue
        filename, kind_ja = match.groups()
        checklist.append({
            "filename": filename, "kind": _CHECKLIST_KIND_FROM_JA[kind_ja],
            "label": rest, "status": status,
        })
    return checklist


def _parse_auto_resume_count(text: str) -> int:
    """「自動再開の試行回数」セクションの内容を整数として読み取る。
    セクションが無い(旧バージョンのPROGRESS.md)・内容が数値として
    解釈できない場合は、まだ1回も自動再開を試みていないとみなして
    `0`を返す。
    """
    section = _extract_progress_section(text, _PROGRESS_SECTION_AUTO_RESUME_COUNT).strip()
    try:
        return int(section)
    except ValueError:
        return 0


def _parse_changelog_markdown(text: str) -> list:
    return [line for line in text.splitlines() if line.strip()]


def _parse_bullet_lines(text: str) -> list:
    """箇条書き(-・*)の各行の内容だけを、件数による足切りをせず
    そのまま取り出す。//fixのタスク分割の判定担当への問い合わせ結果を
    解釈する`_parse_fix_split_subtasks`(2件未満は「分割不要」とみなし
    空リストに切り捨てる)とは異なり、こちらはPROGRESS.mdに自分自身が
    書き出した「未完了の修正サブタスク」を読み戻すための単純な抽出用
    途で、1件しか無くても正しく読み戻せる必要がある。
    """
    items = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line and line[0] in "-・*":
            content = line.lstrip("-・*").strip()
            if content:
                items.append(content)
    return items


def _parse_progress_markdown(path: str):
    """PROGRESS.mdを読み込み、`{"request":, "language":, "tasks":,
    "checklist":, "auto_resume_count":, "changelog":, "pending_fix_request":,
    "pending_fix_subtasks":}`の辞書として返す。ファイルが存在しない・
    想定した形式で解析できない場合は`None`を返す(呼び出し元は、その
    プロジェクトの再開をスキップすべきというシグナルとして扱う)。

    仮の判断: `language`は、この節が無い旧バージョンのPROGRESS.mdでは
    空文字列になる(この機能追加より前に作られた既存プロジェクトを
    エラー扱いにしない後方互換のため)。空文字列の場合、構文チェックは
    ファイルの拡張子で判定するため実害は無く、`_ask_organization_
    fix_project`のプロンプトでは「不明」として扱う。

    仮の判断: `pending_fix_subtasks`も同じ理由でこの節が無いPROGRESS.md
    では空リストになる(//fixのタスク分割機能より前に作られたプロジェクト・
    分割が発生したことのないプロジェクトのどちらも、単に「未完了の
    修正サブタスクは無い」として扱われる)。
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None

    request = _extract_progress_section(text, _PROGRESS_SECTION_REQUEST).strip()
    tasks = _parse_module_breakdown(_extract_progress_section(text, _PROGRESS_SECTION_PLAN))
    checklist = _parse_checklist_markdown(_extract_progress_section(text, _PROGRESS_SECTION_CHECKLIST))
    if not tasks or not checklist:
        return None
    return {
        "request": request, "tasks": tasks, "checklist": checklist,
        "language": _extract_progress_section(text, _PROGRESS_SECTION_LANGUAGE).strip(),
        "auto_resume_count": _parse_auto_resume_count(text),
        "changelog": _parse_changelog_markdown(_extract_progress_section(text, _PROGRESS_SECTION_CHANGELOG)),
        "pending_fix_request": _extract_progress_section(text, _PROGRESS_SECTION_PENDING_FIX_REQUEST).strip(),
        "pending_fix_subtasks": _parse_bullet_lines(_extract_progress_section(text, _PROGRESS_SECTION_PENDING_FIX_SUBTASKS)),
    }


def _progress_checklist_is_incomplete(checklist: list) -> bool:
    return any(task["status"] != _TASK_STATUS_COMPLETED for task in checklist)


def _project_has_pending_work(parsed: dict) -> bool:
    """`_parse_progress_markdown`の解析結果から、このプロジェクトに
    再開すべき作業が残っているかどうかを判定する。従来の(合意フェーズ
    由来の)モジュールタスクのチェックリストの未完了に加え、//fixの
    タスク分割(`_run_fix_task_queue`)が往復回数の上限等で完了させ
    られなかった「未完了の修正サブタスク」(`pending_fix_subtasks`)も
    合わせて確認する。

    仮の判断(バグ報告への対応): 大規模な//fixが一部のサブタスクを
    残したまま終わった場合、モジュールタスクのチェックリスト自体は
    (//fixでは書き換えないため)完了したままになっている。この関数を
    使わず`_progress_checklist_is_incomplete`だけで判定していた箇所
    (`_find_incomplete_projects`等)では、この未完了状態が一切見えず、
    `//resume-all`から再開する手段が無くなってしまっていた。
    """
    return _progress_checklist_is_incomplete(parsed["checklist"]) or bool(parsed.get("pending_fix_subtasks"))


def _pending_tasks_from_checklist(tasks: list, checklist: list) -> list:
    """`tasks`(全ファイル)のうち、チェックリスト上いずれかのタスク
    (実装・レビュー)が未完了のファイルだけを返す。

    仮の判断: 再開の粒度はファイル単位とする。実装は完了しているが
    レビューだけが未完了のファイルも、既存のタスクキュー方式
    (`_run_collaborative_task_queue`)にそのまま乗せて実装からやり直す
    (「レビューだけを再開する」という専用の経路は用意しない)。実装
    済みのコードが再度実装し直されるのは無駄ではあるが、既存の
    タスクキュー方式・レビューフェーズを変更せずにそのまま再利用できる
    (依頼の「既存のタスクキュー方式・レビューフェーズをそのまま使い」
    という要件に沿う)。
    """
    incomplete_filenames = {
        task["filename"] for task in checklist if task["status"] != _TASK_STATUS_COMPLETED
    }
    return [(filename, content) for filename, content in tasks if filename in incomplete_filenames]


def _find_incomplete_projects(out_dir: str) -> list:
    """`<out_dir>/projects/`配下で、PROGRESS.mdのチェックリストに未完了の
    タスクが残っているプロジェクトディレクトリの一覧を返す(ディスク上の
    実際の状態を正として扱う。呼び出し側で状態をキャッシュしない)。
    """
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    if not os.path.isdir(projects_root):
        return []
    incomplete = []
    for name in sorted(os.listdir(projects_root)):
        project_dir = os.path.join(projects_root, name)
        parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
        if parsed is not None and _project_has_pending_work(parsed):
            incomplete.append(project_dir)
    return incomplete


class _PendingDesignDialogue:
    """//agreeの合意フェーズ(対話プロトコル)が「セーフティリミット」または
    「人間に確認」で一時停止した状態を保持し、次の人間の発言をその一時停止
    への回答として合意フェーズに合流させ、合意(または単独での計画確定)に
    達し次第そのままタスク分解・実装フェーズへ橋渡しするためのセッション
    情報。一時停止時点までに確定していた情報一式(対象組織・依頼文・候補
    メンバー・プロジェクトディレクトリ・議事録)をここに保持する。
    """

    def __init__(
        self, port: int, org_fingerprint: str, request: str, candidates: list, project_dir: str, result: dict,
    ):
        self.port = port
        self.org_fingerprint = org_fingerprint
        self.request = request
        self.candidates = candidates
        self.project_dir = project_dir
        self.result = result


class _PendingDesignDialogueBox:
    """`_ask_organization_collaborate`はバックグラウンドジョブ
    (`_BackgroundJobRunner`の別スレッド)として実行されるため、対話プロト
    コルが一時停止した状態(`_PendingDesignDialogue`)をそのスレッドから
    `_run_repl_client`のメインループへ安全に受け渡すための箱。`_ChatLog`と
    同様、別スレッドからの書き込みを想定してロックで保護する。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = None

    def set(self, pending: "_PendingDesignDialogue") -> None:
        with self._lock:
            self._pending = pending

    def take(self):
        """保持している`_PendingDesignDialogue`(無ければ`None`)を取り出し、
        箱を空にする(一度きりの消費。同じ一時停止に対して二重に反応しない
        ようにするため)。
        """
        with self._lock:
            pending, self._pending = self._pending, None
            return pending

    def peek(self):
        with self._lock:
            return self._pending


def _run_collaborate_implementation_phase(
    request: str, answer: str, candidates: list, org_fingerprint: str, project_dir: str, port: int,
) -> None:
    """合意フェーズ(対話プロトコルによる複数ラウンドの議論・設計担当1名への
    1回きりの相談・一時停止後に人間の回答を踏まえて再開した議論、のいずれ
    であっても)がまとめた計画テキストを受け取り、タスク分解・実装・レビュー
    フェーズへ橋渡しする、正常系の共通処理。

    `_ask_organization_collaborate`が一発で合意に達した場合と、一時停止
    からの再開(`_resume_organization_collaborate`)が合意(または単独での
    計画確定)に達した場合の両方から呼ばれ、同じ実装フェーズに合流させる。
    """
    tasks = _parse_module_breakdown(answer)
    if not tasks:
        print("設計担当の回答からファイル分割案を読み取れませんでした。上記の回答内容を確認してください。")
        return

    print(f"[📐 モジュール分割案がまとまりました({len(tasks)}ファイル): {', '.join(f for f, _ in tasks)}]")
    for filename, content in tasks:
        print(f"  - {filename}: {content}")

    # 仮の判断(バグ報告への対応): 設計担当の説明文が、計画に無い架空の
    # ファイル名に言及していないかをここで機械的にチェックする。実装・
    # レビューを始める前(=無駄な問い合わせが発生する前)に、最も早い
    # タイミングで検出・警告することを狙った。処理は中断せず、あくまで
    # 「事前の注意喚起」として続行する(誤検出(例: 外部CDNのファイル名
    # への言及)の可能性もゼロではないため)。
    undeclared_references = _find_undeclared_file_references(tasks)
    for source_file, missing_name in undeclared_references:
        print(
            f"[⚠️ {source_file} の説明文が、計画に無いファイル「{missing_name}」に"
            f"言及しています。設計担当の記述ミスの可能性があります"
            f"(実装予定のファイル一覧: {', '.join(f for f, _ in tasks)})]"
        )

    # 仮の判断: 依頼の項目1(言語判定の結果をPROGRESS.mdに明記する)への
    # 対応。実際に設計担当が提案したファイルの拡張子から言語を逆算する
    # (_infer_language_from_tasks、依頼文からの推測ではなく実際のファイルを
    # 正とする理由は同関数のコメントを参照)。
    language = _infer_language_from_tasks(tasks)
    print(f"[🗂️ 使用言語と判断しました: {language}]")

    # 仮の判断: 合意フェーズがまとめた計画は、組織自身が生み出したもので
    # あっても、確定した瞬間に「必ず完遂すべきタスクリスト」として固定する。
    # 依頼で明示された前提(「組織側が4つ必要だと判断した以上、その4つを
    # 完遂する責任は組織側にある」)に基づく。
    checklist = _build_task_checklist(tasks)
    print(_format_task_checklist(checklist))

    # 仮の判断: 生成物はYoriai本体と混ざらないよう、projects/<プロジェクト名>/
    # というサブディレクトリにまとめる(プロジェクト名からのディレクトリ
    # 解決自体は、対話プロトコル用に呼び出し元で既に済んでいる)。
    print(f"[📁 生成物の保存先: {project_dir}]")

    _run_collaborative_project(
        request, tasks, checklist, candidates, org_fingerprint, project_dir, tasks, language=language,
    )

    # 仮の判断: 自動再開(有限の上限付き)は、この「新規の協業モード実行」
    # 直後にのみトリガーする。手動`//resume-all`(_run_resume_all)からは
    # 呼ばない(依頼の要件5: 手動実行はこの試行回数カウンタと無関係に
    # 何度でもできるようにする、という区別を素直に実装するため)。
    _maybe_auto_resume(project_dir, port, org_fingerprint)


def _ask_organization_collaborate(
    port: int, org_fingerprint: str, request: str, out_dir: str, enable_dialogue: bool = False,
    pending_box: "_PendingDesignDialogueBox" = None,
) -> None:
    """「〇〇を作って」のような制作依頼用: まず優先順位が最も高い1台に
    モジュール分割案とインターフェース設計を相談し(合意フェーズ)、
    その結果をタスクキュー方式(`_run_collaborative_task_queue`)で異なる
    メンバーに実装・相互レビューさせる。タスク数がメンバー数を超えても、
    メンバーの手が空くたびに次のタスクが割り当てられるため、最終的に
    全タスクが実装される。

    仮の判断:
    - 「設計担当」の選び方は他の候補選びと同じ優先順位ロジック
      (コーディング系タスクとして最優先の候補)をそのまま流用する。
      設計専用のモデルを分けて選ぶ仕組みは今回のスコープ外。
    - 設計担当の回答が期待した形式(「ファイル名: 内容」の行)で
      1件も得られなかった場合は、その旨と回答全文を表示して中断する
      (フォーマットの自動修正・再質問までは今回のスコープ外)。

    `enable_dialogue=True`の場合、単一の設計担当への1回きりの相談では
    なく、対話プロトコル(`_run_dialogue`)による複数ノード・複数
    ラウンドの合意形成を経て計画を決める(依頼の「計画フェーズでの
    利用」)。既定は`False`のまま(既存の呼び出し元・テストとの後方
    互換性のため)で、対話モードのREPLから`//agree`・自動判定経由で
    呼ばれる際にのみ`True`を渡す。

    `pending_box`(既定`None`)を渡すと、対話プロトコルがセーフティ
    リミット・人間の確認待ちで一時停止した場合に、再開に必要な情報
    (`_PendingDesignDialogue`)をそこへ格納する。`_run_repl_client`は
    これを使って、次の人間の発言を「一時停止への回答」として合意フェーズ
    に合流させる(`_resume_organization_collaborate`)。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return

    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    architect = candidates[0]

    # 仮の判断: プロジェクトディレクトリは、対話プロトコルによる議事録・
    # 要約の保存先としても使うため、設計フェーズより前にここで確定させる
    # (後段で改めて`_resolve_project_dir`を呼び直すと、対話ログの保存で
    # 既にディレクトリが作られてしまっているために連番がずれて、生成物と
    # 対話ログが別のディレクトリに散らばってしまう)。
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    project_name = _project_name_with_date_prefix(request)
    project_dir = _resolve_project_dir(projects_root, project_name)

    if enable_dialogue:
        answer, aborted, dialogue_result = _run_design_dialogue(org_fingerprint, request, candidates, project_dir)
        if aborted:
            if pending_box is not None and dialogue_result["status"] in (
                DIALOGUE_STATUS_NEEDS_HUMAN, DIALOGUE_STATUS_SAFETY_LIMIT,
            ):
                pending_box.set(_PendingDesignDialogue(
                    port, org_fingerprint, request, candidates, project_dir, dialogue_result,
                ))
            return
    else:
        print(
            f"[🧭 合意フェーズ開始: まず {architect['label']} (モデル: {architect['model']}) に"
            f"モジュール分割案とインターフェース設計を相談しています...]"
        )
        breakdown_prompt = _build_module_breakdown_prompt(request)
        answer, error, truncated = _collect_answer_from_candidate(
            architect, org_fingerprint, [{"role": "user", "content": breakdown_prompt}],
        )
        if error:
            print(f"設計担当への問い合わせに失敗しました: {error}")
            return
        if not answer:
            print("設計担当から応答が得られませんでした。")
            return
        if truncated:
            print(f"[⚠️ 設計担当の応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました。以下は不完全な内容の可能性があります]")

        # 仮の判断: 解析後の内容だけでなく、設計担当の回答そのもの(生テキスト)も
        # 表示する。パース処理(_parse_module_breakdown)の解釈が意図と異なって
        # いないか、実機での動作確認時に見比べられるようにするため。
        print(f"[🧭 {architect['label']} の回答(合意フェーズの結果、そのまま表示)]")
        print(answer)

    _run_collaborate_implementation_phase(request, answer, candidates, org_fingerprint, project_dir, port)


def _run_collaborative_project(
    request: str, tasks: list, checklist: list, candidates: list, org_fingerprint: str,
    project_dir: str, tasks_to_queue: list, auto_resume_count: int = 0, changelog: list = None,
    language: str = "",
) -> None:
    """タスクキュー方式による実装・レビューを実行し、その間PROGRESS.mdを
    更新し続ける。新規プロジェクト(`_ask_organization_collaborate`)・
    再開(`_resume_project`)の両方から共通で呼ばれる。

    `tasks`は計画全体(PROGRESS.mdの「モジュール分割案」に書き出す対象)、
    `tasks_to_queue`は実際にタスクキューへ投入する対象(新規作成時は
    `tasks`と同じ、再開時は未完了だったファイルだけの部分集合)。

    `auto_resume_count`(自動再開の試行回数)・`changelog`(既存プロジェクト
    への修正依頼の更新履歴)・`language`(使用言語)は、この関数自身は
    変更せず、呼び出し元から渡された値をそのままPROGRESS.mdに書き戻すだけの、
    素通しの値として扱う。新規プロジェクトでは既定値(`0`・空・空文字列)の
    まま、手動`//resume-all`(`_resume_project`)経由ではディスク上の既存の
    値がそのまま渡ってくる。これが無いと、修正依頼で追記した更新履歴や、
    合意フェーズで決まった使用言語が、その後の(無関係な)`//resume-all`に
    よるPROGRESS.mdの書き換えで消えてしまう。
    """
    changelog = list(changelog) if changelog else []
    review_feedback = {}
    progress_lock = threading.Lock()

    def write_progress():
        with progress_lock:
            _write_progress_md(
                project_dir, request, tasks, checklist, review_feedback, auto_resume_count, changelog, language,
            )

    write_progress()
    if tasks_to_queue:
        # 仮の判断: タスク数がメンバー数を超える場合に「超過分は切り捨てる」
        # 従来の実装(_dispatch_and_save_parallel_tasksの1:1固定割り当て)を
        # やめ、タスクキュー方式(_run_collaborative_task_queue)で全タスクが
        # 最終的に実装されるようにした。詳しくは同関数のコメントを参照。
        _run_collaborative_task_queue(
            tasks_to_queue, candidates, org_fingerprint, project_dir, checklist,
            on_update=write_progress, review_feedback=review_feedback,
        )

    # 【最重要】組織自身が立てた計画のうち、1つでも未完了のタスクが残って
    # いる場合は「✅ レビュー完了」を名乗らず、何が終わっていないかを
    # 明示して報告する。実装フェーズ・レビューフェーズがそれぞれ「自分の
    # 担当範囲では完了した」と表示していても、候補不足等で計画の一部が
    # そもそも実装フェーズに到達しなかった場合(config.pyスキップの不具合)
    # を、このタスクリストによる最終チェックが最後の砦として検出する。
    print()
    print(_format_task_checklist(checklist))
    incomplete_labels = _incomplete_task_labels(checklist)
    if incomplete_labels:
        print(f"[⚠️ 未完了のタスクが残っています: {', '.join(incomplete_labels)}]")
    else:
        print(f"[✅ 全タスク完了 (保存先: {project_dir})]")
    write_progress()


# ---------------------------------------------------------------------------
# 相互レビューフェーズ(実験3)
# ---------------------------------------------------------------------------
#
# 実装フェーズが完了した後、各メンバーに「自分以外が担当した、もう一方の
# ファイルのコード」を見せてレビューさせ、問題があれば担当メンバーに
# 修正を依頼する。暴走防止のため、1ファイルにつき初回レビュー+修正後の
# 再レビューの最大2回までとし、ループ処理ではなく2回分を明示的に順番に
# 呼び出す構造にすることで、実装上も「それ以上繰り返せない」ようにしてある。

_REVIEW_PROMPT_TEMPLATE = """あなたはコードレビュー担当です。以下は、組織内の別のメンバーが実装した{filename}のコードです。

【実装計画全体(事前に合意した内容)】
{full_plan}

【あなたが担当した{reviewer_own_filename}(参考: 正しく連携できそうか確認する際に使ってください)】
```python
{reviewer_own_code}
```

【レビュー対象: {filename}】
```python
{code}
```
{self_explanation_section}
以下の観点でレビューしてください:
1. 実装計画で合意した内容(関数名・引数・戻り値の型・データ形式)と、実際のコードが一致しているか
2. あなたが担当した{reviewer_own_filename}と正しく連携できそうか
3. 実装計画に登場する他のファイル(あなたが担当したファイル以外)との連携が必要な場合、read_fileツールで実際のファイルの中身を確認したうえで判断すること。推測だけで判断してはいけません。まだ実装されていないファイルを指定した場合は、その旨が結果として返されます。{search_then_read_guidance}
4. 例外処理の欠如・タイポなど、コードとして明らかな不備がないか

出力は次の形式のみとし、他の説明文は含めないでください。
- 問題が無ければ、1行目に「問題なし」とだけ書いてください。
- 問題があれば、1行目に「問題あり」と書き、2行目以降に具体的な指摘内容を書いてください。
"""

_FIX_PROMPT_TEMPLATE = """以下はあなたが実装した{filename}のコードですが、レビュー担当から指摘がありました。指摘内容を踏まえて修正したコードを出力してください。

【実装計画全体(事前に合意した内容)】
{full_plan}

【現在のコード: {filename}】
```python
{code}
```

【レビュー担当からの指摘】
{feedback}

修正後の{filename}の完全なコードを、コードブロックで出力してください。説明文は不要です。
"""


def _build_review_prompt(
    filename: str, code: str, reviewer_own_filename: str, reviewer_own_code: str, full_plan: str,
    self_explanation: str = "",
) -> str:
    # 仮の判断(対話プロトコルの「レビュー用途での再利用」): レビュー本番の
    # 前に実装担当・レビュー担当の間で行った軽量な自己説明対話
    # (`_run_review_self_explanation`)の申し送り事項があれば、参考情報
    # として追記する。無い場合(既定)は既存のプロンプトと一字一句同じ
    # になるよう、空文字列のまま何も追記しない。
    self_explanation_section = (
        f"\n【実装担当の設計判断についての申し送り事項(事前の自己説明・質疑より)】\n{self_explanation}\n"
        if self_explanation else ""
    )
    return _REVIEW_PROMPT_TEMPLATE.format(
        filename=filename, code=code,
        reviewer_own_filename=reviewer_own_filename, reviewer_own_code=reviewer_own_code,
        full_plan=full_plan, search_then_read_guidance=f" {_SEARCH_THEN_READ_GUIDANCE}",
        self_explanation_section=self_explanation_section,
    )


def _build_fix_prompt(filename: str, code: str, feedback: str, full_plan: str) -> str:
    return _FIX_PROMPT_TEMPLATE.format(filename=filename, code=code, feedback=feedback, full_plan=full_plan)


def _parse_review_verdict(answer: str) -> tuple:
    """レビュー担当の回答(1行目に「問題なし」または「問題あり」)を
    (問題なしかどうか, 指摘内容)に変換する。1行目にどちらの語も
    含まれない場合は、暴走防止のため「問題なし」側に倒す(仮の判断:
    レビュー担当の回答が期待した形式でなかった場合にまで自動修正
    ループへ進めてしまうより、安全側に倒して手動確認に委ねる方が
    無難と判断した)。
    """
    lines = [line.strip() for line in answer.strip().splitlines() if line.strip()]
    if not lines:
        return True, ""
    first_line = lines[0]
    if "問題あり" in first_line:
        feedback = "\n".join(lines[1:]).strip()
        return False, feedback if feedback else answer.strip()
    return True, ""


# 仮の判断: 実機で、メソッドチェーンを繋げる際の括弧漏れによる
# IndentationErrorが、LLMによる内容レビューで「問題なし」と誤判定され、
# そのまま確定してしまう不具合が見つかった。関数名・データ形式の一致
# といった「内容」の妥当性はLLMでなければ判断できないが、「そもそも
# 構文として正しいPythonコードか」は`ast.parse`で機械的かつ確実に
# 判定できる。機械的に検出できるものをわざわざLLMに判断させると、
# 誤判定のリスクと問い合わせのコストの両方を無駄に払うことになるため、
# 内容レビューより前に必ず構文チェックを通す関門を設けた。
def _check_python_syntax(code: str) -> tuple:
    """Pythonコードの構文を`ast.parse`で機械的にチェックする。
    (構文が正しいかどうか, エラー内容の文字列(正しい場合は空文字列))
    を返す。`IndentationError`は`SyntaxError`のサブクラスなので、
    まとめて捕捉できる。
    """
    try:
        ast.parse(code)
    except SyntaxError as exc:
        error_type = type(exc).__name__
        if exc.lineno is not None:
            detail = f"{error_type} ({exc.lineno}行目: {exc.msg})"
        else:
            detail = f"{error_type}: {exc.msg}"
        return False, detail
    return True, ""


# 仮の判断: 依頼の項目2(言語非依存の構文チェック)への対応。プロジェクト
# 全体の「使用言語」設定ではなく、ファイル単体の拡張子で振り分ける
# (1つのプロジェクトにHTML/CSS/JavaScriptのように複数の拡張子が混在する
# ことが普通にあり得るため、ファイル単位の判定の方が実態に即している)。
_EXTERNAL_SYNTAX_CHECK_TIMEOUT_SEC = 15

_INLINE_SCRIPT_JS_TYPES = {"", "text/javascript", "application/javascript", "module"}


class _InlineScriptExtractor(_HTMLParser):
    """HTML内の`<script>...</script>`から、インラインJavaScriptの中身
    だけを抜き出す(`src`属性を持つ外部ファイル参照や、
    `type="application/json"`のようなJavaScript以外の`<script>`は除く)。

    仮の判断: 「HTML/CSSと組み合わせたJavaScript(`<script>`タグ内に直接
    書かれたコード)は構文チェックされない」という依頼への対応。HTML
    ファイル全体をJavaScriptとして構文チェックすることはできないため、
    `<script>`タグの中身だけをブロック単位で取り出し、`node --check`に
    個別にかける(壊れた1ブロックが他のブロックの検査を妨げないよう、
    ブロックごとに独立して実行する)。`type="module"`かどうかも記録し、
    ESモジュール構文(`import`/`export`)を通常のスクリプトと誤判定して
    構文エラー扱いにしないようにする。
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []  # [(開始行, コード, is_module), ...]
        self._collecting = False
        self._buffer = []
        self._start_line = 0
        self._is_module = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "script":
            return
        attrs = dict(attrs)
        if "src" in attrs:
            return
        script_type = (attrs.get("type") or "").strip().lower()
        if script_type not in _INLINE_SCRIPT_JS_TYPES:
            return
        self._collecting = True
        self._buffer = []
        self._start_line = self.getpos()[0]
        self._is_module = script_type == "module"

    def handle_endtag(self, tag):
        if tag.lower() != "script" or not self._collecting:
            return
        self.scripts.append((self._start_line, "".join(self._buffer), self._is_module))
        self._collecting = False

    def handle_data(self, data):
        if self._collecting:
            self._buffer.append(data)


def _extract_inline_scripts(html_code: str) -> list:
    """`(開始行, コード, is_module)`のリストを返す。壊れたHTMLでも
    `html.parser`は寛容にパースを続けるため、抽出自体が例外で失敗する
    ことは無い(念のため`feed`は例外を握りつぶし、その時点までに集まった
    結果を返す)。
    """
    parser = _InlineScriptExtractor()
    try:
        parser.feed(html_code)
    except Exception:
        pass
    return parser.scripts


def _check_file_syntax(filename: str, code: str, file_path: str = None) -> tuple:
    """`(status, detail)`を返す。`status`は次のいずれか:
    - `"ok"`: 構文チェックを行い、問題が無かった
    - `"error"`: 構文チェックを行い、問題が見つかった(`detail`にエラー内容)
    - `"skipped"`: この拡張子には対応していない、またはチェックに必要な
      ツール(gcc等)が実機に無いためスキップした(`detail`にその理由)

    仮の判断: C言語の構文チェック(`gcc -fsyntax-only`)は実際のファイルを
    読ませる必要があるため`file_path`(ディスク上の実パス)を使う。
    呼び出し元がまだファイルを保存していない場合(`file_path`が`None`)は
    チェックできないため、エラーにはせずスキップとして扱う。
    """
    if filename.endswith(".py"):
        ok, detail = _check_python_syntax(code)
        return ("ok" if ok else "error"), detail
    if filename.endswith(".html"):
        # 仮の判断: HTMLマークアップ自体の構文チェックは、標準ライブラリの
        # html.parserが壊れたマークアップにも寛容で構文エラーを検出できない
        # ため、引き続き行わない。一方、HTML内に直接書かれたインライン
        # JavaScript(`<script>`タグの中身)はnode --checkでチェックできる
        # ため、実機にnodeがあれば行う(依頼: 「JSをHTML/CSSと組み合わせた
        # 場合もチェックしてほしい」への対応)。
        node_path = shutil.which("node")
        if not node_path:
            return "skipped", "node(Node.js)が見つからないため、インラインの<script>の構文チェックをスキップしました。"
        scripts = _extract_inline_scripts(code)
        if not scripts:
            return "skipped", "この種類のファイル(HTML)のマークアップ自体の構文チェックには対応していません(インラインの<script>も見つかりませんでした)。"
        errors = []
        for start_line, script_code, is_module in scripts:
            node_args = [node_path]
            if is_module:
                node_args.append("--input-type=module")
            node_args += ["--check", "-"]
            try:
                result = subprocess.run(
                    node_args, input=script_code, capture_output=True, text=True,
                    timeout=_EXTERNAL_SYNTAX_CHECK_TIMEOUT_SEC,
                )
            except Exception as exc:
                errors.append(f"{start_line}行目付近の<script>: 構文チェックの実行に失敗しました: {exc}")
                continue
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "構文エラーが発生しました").strip()
                errors.append(f"{start_line}行目付近の<script>: {detail}")
        if errors:
            return "error", "\n".join(errors)
        return "ok", ""
    if filename.endswith(".css"):
        # 仮の判断: CSS向けの信頼できる構文パーサーは標準ライブラリに
        # 存在しないため、新たな依存を追加せずスキップを選んだ。
        return "skipped", "この種類のファイル(CSS)の構文チェックには対応していません。"
    if filename.endswith(".js"):
        # C言語のgcc確認と同じ方式: 実機にnodeが無い場合はエラー扱いに
        # せずスキップする(依頼の項目3)。
        node_path = shutil.which("node")
        if not node_path or not file_path:
            return "skipped", "node(Node.js)が見つからないため、構文チェックをスキップしました。"
        try:
            result = subprocess.run(
                [node_path, "--check", file_path],
                capture_output=True, text=True, timeout=_EXTERNAL_SYNTAX_CHECK_TIMEOUT_SEC,
            )
        except Exception as exc:
            return "skipped", f"構文チェックの実行に失敗したためスキップしました: {exc}"
        if result.returncode == 0:
            return "ok", ""
        return "error", (result.stderr or result.stdout or "構文エラーが発生しました").strip()
    if filename.endswith(".c"):
        gcc_path = shutil.which("gcc") or shutil.which("cc")
        if not gcc_path or not file_path:
            return "skipped", "gcc(またはcc)が見つからないため、構文チェックをスキップしました。"
        try:
            result = subprocess.run(
                [gcc_path, "-fsyntax-only", file_path],
                capture_output=True, text=True, timeout=_EXTERNAL_SYNTAX_CHECK_TIMEOUT_SEC,
            )
        except Exception as exc:
            return "skipped", f"構文チェックの実行に失敗したためスキップしました: {exc}"
        if result.returncode == 0:
            return "ok", ""
        return "error", (result.stderr or result.stdout or "コンパイルエラーが発生しました").strip()
    return "skipped", "この言語の構文チェックには対応していません。"


# ---------------------------------------------------------------------------
# 並行実行中のスレッドの出力が入り乱れないようにするための共通処理
# ---------------------------------------------------------------------------
#
# タスクキュー方式では複数のワーカースレッドが同時に実装・レビューを
# 進めるため、素朴にprint()を個別に呼ぶと、あるタスクの出力の途中に
# 別のタスクの出力が挟まり、画面上でどちらの結果か判別できなくなる
# 不具合が実機で報告された(例: cli.pyのレビュー開始の直後に、別ファイル・
# 別担当者のutils.pyのレビュー結果が出てきてしまう)。

def _print_tagged(print_lock: threading.Lock, tag: str, text: str) -> None:
    """`text`の各行の先頭に`[tag]`を付けたうえで、`print_lock`を保持した
    まま1回のprint()呼び出しでまとめて出力する。

    仮の判断: (1)関連する複数行をこの関数の呼び出し1回にまとめ、
    print_lockを保持している間に1回のprint()で書き出すことで、
    「ひとまとまりの出力」(例: 複数行にわたるコードのプレビュー)が
    他スレッドの出力によって分断されないようにする。(2)すべての行の
    先頭に対象ファイル名(通常はタスクを一意に識別できるファイル名を
    そのまま使う)を付けることで、たとえ別タスクのブロックが直後に
    続いても、どちらのタスクの行かを1行単位でも判別できるようにする。
    ネットワーク応答待ちなどブロッキングするI/Oの間はロックを保持せず、
    実際にprintする直前だけロックを取る(呼び出し元がI/Oを待っている間は
    ロックの外)ため、ワーカースレッド同士の並行性は損なわれない。

    仮の判断: `print_lock`は`None`でもよい(この関数を含む一連のレビュー
    関数は、タスクキュー方式(複数スレッドが並行実行)だけでなく、
    既存のテストのように単体で直接呼び出されることもあるため)。`None`の
    場合はロックを取らずにタグ付きテキストだけ出力する。
    """
    tagged = "\n".join(f"[{tag}] {line}" for line in text.split("\n"))
    if print_lock is not None:
        with print_lock:
            print(tagged)
    else:
        print(tagged)


def _check_and_review_one_round(
    filename: str, code: str, reviewer: dict, reviewer_own_filename: str, reviewer_own_code: str,
    full_plan: str, org_fingerprint: str, round_label: str, out_dir: str, print_lock: threading.Lock = None,
    self_explanation: str = "",
) -> tuple:
    """1回分の「構文チェック→(通れば)内容レビュー」を実行し、
    (問題なしかどうか, 指摘内容)を返す。

    仮の判断: 構文チェックはファイルの拡張子に応じて`_check_file_syntax`
    が振り分ける(対応していない拡張子・ツールが実機に無い場合は
    スキップされる)。この時点で`code`は既にディスク(`out_dir/filename`)に
    保存済みのため、C言語のコンパイルチェックのように実ファイルを必要と
    する言語にも対応できる。
    """
    file_path = os.path.join(out_dir, filename)
    status, detail = _check_file_syntax(filename, code, file_path=file_path)
    if status == "ok":
        _print_tagged(print_lock, filename, "[🔍 構文チェック: OK]")
    elif status == "error":
        _print_tagged(print_lock, filename, f"[❌ 構文チェック: {detail}]")
        # 仮の判断: 構文エラーがある場合は、LLMによる内容レビューを
        # 待たずに即座に「問題あり」として扱う。構文が壊れている
        # コードをLLMに読ませて内容の妥当性を判断させても意味が無く、
        # 機械的に検出できるエラーは機械的に弾く方が速く確実。
        return False, f"構文エラーがあります: {detail}"
    else:  # skipped
        _print_tagged(print_lock, filename, f"[⏭️ 構文チェック: スキップ({detail})]")

    return _review_one_file(
        filename, code, reviewer, reviewer_own_filename, reviewer_own_code, full_plan, org_fingerprint, round_label, out_dir, print_lock,
        self_explanation,
    )


def _review_one_file(
    filename: str, code: str, reviewer: dict, reviewer_own_filename: str, reviewer_own_code: str,
    full_plan: str, org_fingerprint: str, round_label: str, out_dir: str, print_lock: threading.Lock = None,
    self_explanation: str = "",
) -> tuple:
    """1回分のレビューを実行し、(問題なしかどうか, 指摘内容)を返す。
    問い合わせ自体が失敗した場合も「問題あり」として扱う(暴走防止のため
    無条件に成功扱いにはしない)。

    レビュー担当には、自分の担当ファイル(reviewer_own_*)をプロンプトに
    直接埋め込むことに加えて、read_fileツールでプロジェクト内の任意の
    ファイル(その時点で実際に保存済みのもの)を確認できるようにする。
    read_fileが実際に呼ばれた瞬間にout_dirを読み直すため、このレビューの
    開始後に他のワーカースレッドが完成させたファイルも正しく反映される
    (_collect_review_answer_with_read_file参照)。

    仮の判断: `reviewer_own_code`(参考として見せるだけの、レビュー対象
    ではないファイル)は、read_file経由の他ファイルと同じ理由
    (トークン使用量対策)で`_truncate_file_content`により上限を設ける。
    レビュー対象そのものである`code`は、トークン量に関わらず常に全体を
    見せる(切り詰めた不完全な内容で合否を判断させるのは本末転倒なため)。
    """
    truncated_reviewer_own_code, reviewer_own_truncated = _truncate_file_content(reviewer_own_code)
    if reviewer_own_truncated:
        truncated_reviewer_own_code += (
            f"\n... (以下省略。トークン使用量を抑えるため先頭{_FILE_CONTENT_TRUNCATE_CHARS}文字のみ表示)"
        )
    _print_tagged(print_lock, filename, f"[🔎 {round_label}] {reviewer['label']} が {filename} をレビューしています...")
    prompt = _build_review_prompt(
        filename, code, reviewer_own_filename, truncated_reviewer_own_code, full_plan, self_explanation,
    )
    answer, error, answer_truncated = _collect_review_answer_with_read_file(
        reviewer, org_fingerprint, [{"role": "user", "content": prompt}], out_dir,
        print_lock=print_lock, tag=filename,
    )
    if answer_truncated:
        # 仮の判断: 複雑な指摘を書こうとした応答がCHAT_MAX_OUTPUT_TOKENS上限で
        # 途中で打ち切られた場合、1行目の判定(「問題あり」/「問題なし」)は
        # 残っていても、その後に続くはずだった指摘内容が不完全な可能性がある。
        # 中途半端な指摘内容に基づいて修正を進めるより、この回のレビューを
        # 問い合わせ失敗と同様に扱い安全側(=最大2回の枠内で次に進む)に倒す。
        _print_tagged(
            print_lock, filename,
            f"[{reviewer['label']}が{filename}をレビュー] 応答が長すぎたため打ち切りました({CHAT_MAX_OUTPUT_TOKENS}トークン上限)。",
        )
        return False, f"レビュー応答が{CHAT_MAX_OUTPUT_TOKENS}トークン上限で打ち切られました"
    if error or not answer:
        message = error or "応答がありませんでした"
        _print_tagged(print_lock, filename, f"[{reviewer['label']}が{filename}をレビュー] 問い合わせに失敗しました: {message}")
        return False, f"レビューへの問い合わせに失敗しました: {message}"

    ok, feedback = _parse_review_verdict(answer)
    if ok:
        _print_tagged(print_lock, filename, f"[{reviewer['label']}が{filename}をレビュー] 問題なし")
    else:
        _print_tagged(print_lock, filename, f"[{reviewer['label']}が{filename}をレビュー] 問題あり: {feedback}")
    return ok, feedback


def _request_fix(
    filename: str, code: str, feedback: str, owner: dict, full_plan: str, org_fingerprint: str, out_dir: str,
    print_lock: threading.Lock = None,
):
    """レビューで指摘された内容を担当メンバーに伝え、修正版のコードを
    取得して保存する。修正後のコード文字列を返し、失敗時はNoneを返す。

    仮の判断: 複雑な指摘(複数の問題点を同時に含む等)を受けた修正依頼で、
    モデルの応答がCHAT_MAX_OUTPUT_TOKENS上限に達して途中で打ち切られる
    ことが実機で報告された(生成が終わらず手動での強制終了が必要になった
    事例)。打ち切られた応答は、モデルが「修正し終えた」と判断した結果
    ではなく不完全なコードである可能性が高いため、通常の問い合わせ失敗と
    同様にNoneを返して不採用にする。これにより、この回の修正は
    (既存の「最大2回のレビュー」構造における)1回分として消費され、
    呼び出し元の`_review_and_fix_one_file`は元のレビュー指摘内容を
    未解決として報告する(新たな再試行ループは追加しない)。
    """
    _print_tagged(print_lock, filename, f"[🔧 {owner['label']} に {filename} の修正を依頼しています...]")
    prompt = _build_fix_prompt(filename, code, feedback, full_plan)
    answer, error, truncated = _collect_answer_from_candidate(owner, org_fingerprint, [{"role": "user", "content": prompt}])
    if truncated:
        _print_tagged(
            print_lock, filename,
            f"  → 応答が長すぎたため打ち切りました({CHAT_MAX_OUTPUT_TOKENS}トークン上限)。"
            "この回の修正は不採用とし、未完了として扱います。",
        )
        return None
    if error or not answer:
        _print_tagged(print_lock, filename, f"  → 修正依頼への問い合わせに失敗しました: {error or '応答がありませんでした'}")
        return None

    fixed_code = _extract_code_from_answer(answer)
    dest_path = os.path.join(out_dir, filename)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(fixed_code)
    _print_tagged(print_lock, filename, f"[💾 修正版の {dest_path} を保存しました (担当: {owner['label']})]")
    return fixed_code


def _run_review_self_explanation(
    filename: str, owner: dict, reviewer: dict, code: str, full_plan: str, org_fingerprint: str,
    print_lock: threading.Lock = None,
) -> str:
    """レビュー本番の前に、実装担当(提案役)に設計判断を短く説明させ、
    レビュー担当(反論役・統合役を兼務)がその場で疑問点をぶつける、
    軽量な自己説明対話を行う(対話プロトコルの「レビュー用途での
    再利用」)。説明に詰まる・要領を得ない場合、そのことが申し送り事項
    として本番のレビューに引き継がれ、レビュー担当の最終判断の材料になる。

    仮の判断: 本番のレビュー(`_review_one_file`)とは異なり、ここでの
    対話は「問題あり/問題なし」の最終判定そのものではなく、あくまで
    本番レビューへの参考情報を作ることが目的のため、`min_rounds`/
    `max_rounds`を小さくして往復を短く抑える。議事録・要約のディスクへの
    保存も行わない(ファイルごとに議事録ファイルが量産されるのを避ける
    ため。永続化が必須なのは計画フェーズ・修正フェーズのような、
    プロジェクト全体に関わる議題に対してのみ)。
    """
    background = f"【実装計画全体】\n{full_plan}\n\n【{filename}のコード】\n{_truncate_file_content(code)[0]}"
    result = _run_dialogue(
        org_fingerprint=org_fingerprint,
        topic=f"{filename} の設計判断についての自己説明",
        background=background,
        candidates=[owner, reviewer],
        output_instruction=(
            "実装者の説明とレビュー担当の疑問点を踏まえ、レビュー本番で確認すべき懸念点があれば"
            "2〜3文で書いてください(特に無ければ「特になし」とだけ書いてください)。"
        ),
        print_lock=print_lock, tag=filename, min_rounds=1, max_rounds=2,
    )
    return (result.get("final_content") or result.get("summary") or "").strip()


def _review_and_fix_one_file(
    filename: str, owner: dict, code: str, reviewer: dict, reviewer_own_filename: str, reviewer_own_code: str,
    full_plan: str, org_fingerprint: str, out_dir: str, print_lock: threading.Lock = None,
    enable_self_explanation: bool = False,
) -> tuple:
    """1ファイル分のレビュー→(必要なら)修正→再レビューを行い、
    (最終的に「問題なし」になったかどうか, 未解決の場合の指摘内容)を返す
    (問題なしの場合、指摘内容は`None`)。

    仮の判断: ループではなく「1回目のレビュー」→「(問題があれば)修正」→
    「修正後の再レビュー」という3ステップを直列に明示的に呼び出す構造に
    した。ループにして回数を変数でカウントする実装だと、条件分岐を
    間違えた場合に暴走するリスクが残るが、この構造なら最大2回のレビュー
    (+その間の1回の修正)しか物理的に発生し得ない。各「レビュー」は
    実際には「構文チェック→(通れば)内容レビュー」の2段階
    (`_check_and_review_one_round`)になっており、構文エラーが
    残ったまま2回目の構文チェックも通らなければ、そこで打ち切られる
    (=構文チェックの再試行も、この最大2回の枠内に収まる)。

    `print_lock`(タスクキュー方式専用)を渡すと、このファイルに関する
    レビュー・修正の全出力に`filename`のタグが付き、他のワーカー
    スレッドの出力と混ざらないよう1ブロックずつまとめて出力される。

    `enable_self_explanation=True`の場合、本番のレビューの前に
    `_run_review_self_explanation`による軽量な自己説明対話を1回だけ
    行い、その申し送り事項を1回目・再レビューの両方のレビュープロンプトに
    渡す(修正のたびに説明をやり直す必要は無いため、対話は1ファイル
    あたり1回に留める)。既定は`False`のまま(既存の呼び出し元・
    テストとの後方互換性のため)で、タスクキュー方式
    (`_run_collaborative_task_queue`)からのみ`True`が渡る。

    仮の判断(PROGRESS.md用): 最終的に未解決だった指摘内容も呼び出し元に
    返すようにした(以前は合否のみ`bool`で返していた)。PROGRESS.mdの
    「直近のレビュー指摘」セクションに、未完了のファイルがなぜ未完了
    なのかを記録するために必要になったための変更。
    """
    self_explanation = ""
    if enable_self_explanation:
        self_explanation = _run_review_self_explanation(
            filename, owner, reviewer, code, full_plan, org_fingerprint, print_lock,
        )

    ok, feedback = _check_and_review_one_round(
        filename, code, reviewer, reviewer_own_filename, reviewer_own_code, full_plan, org_fingerprint, "1回目のレビュー", out_dir, print_lock,
        self_explanation,
    )
    if ok:
        return True, None

    fixed_code = _request_fix(filename, code, feedback, owner, full_plan, org_fingerprint, out_dir, print_lock)
    if fixed_code is None:
        return False, feedback

    ok, feedback = _check_and_review_one_round(
        filename, fixed_code, reviewer, reviewer_own_filename, reviewer_own_code, full_plan, org_fingerprint, "修正後の再レビュー", out_dir, print_lock,
        self_explanation,
    )
    return ok, (None if ok else feedback)


# ---------------------------------------------------------------------------
# タスクキュー方式による実装・レビュー
# ---------------------------------------------------------------------------
#
# 以前は「メンバー数ぶんだけタスクを実行し、超過分は切り捨てる」実装
# だった(_dispatch_and_save_parallel_tasksの1:1固定割り当てと、静的な
# 円環割り当てで1回だけレビューする旧_run_review_phase)。合意フェーズが
# 提案したファイル数がメンバー数を超えると、超過分は永遠に実装されない
# (タスクチェックリストの追加調査で見つかった不具合の温床でもあった)。
# タスクを待ち行列(キュー)として扱い、メンバーの実装+レビューが完了
# するたびに次のタスクを割り当てる方式に変更し、メンバー数に関わらず
# 最終的に全タスクが実装されるようにした。

def _estimate_task_weight(content: str) -> int:
    """依頼文(合意フェーズがそのファイルについて書いた説明)の長さを、
    タスクの「重さ(実装の複雑さ)」の簡易的な代理指標として使う。

    仮の判断: 依頼文の複雑さを厳密に見積もるには本来LLMへの追加の
    問い合わせが必要になるが、それではタスクの割り当て順序を決める
    ためだけに追加のネットワーク往復が発生してしまう。説明文が長い
    ファイルほど、実装すべき関数シグネチャやデータ形式の記述が多く
    実装により時間がかかるだろう、という単純な仮定に基づく。依頼に
    明記された「簡易的に推定」という方針に沿った実装。
    """
    return len(content)


def _run_collaborative_task_queue(
    tasks: list, candidates: list, org_fingerprint: str, project_dir: str, checklist: list,
    on_update: callable = None, review_feedback: dict = None,
) -> None:
    """合意フェーズで確定した全タスクを待ち行列(キュー)として扱い、
    メンバーの実装+レビューが完了するたびに次のタスクを割り当てながら
    処理する。メンバー数がタスク数より少なくても、最終的に全タスクが
    実装される。

    仮の判断:
    - タスクは`_estimate_task_weight`による「重さ」の降順に並べたものを
      キューとする。`candidates`は`_select_chat_candidates`が既に空き
      メモリの多い順に並べているため、キューの先頭(最も重いタスク)から
      順に、空きメモリの多いメンバーへ初期割り当てされる形になる
      (「空きメモリが多いメンバーには重いタスクを優先的に割り当てる」
      という依頼の要件に対応)。2巡目以降は、手が空いたメンバーがその
      時点でキューの先頭(残りの中で最も重いタスク)を取る。
    - メンバーは1人1スレッドの「ワーカー」として動作し、キューが空に
      なるまで「タスクを取る→実装→(担当外の別メンバーに)レビューを
      依頼→(必要なら)修正→再レビュー」を繰り返す。実装+レビューの
      両方が(合否に関わらず)完了して初めて、そのメンバーは次のタスクを
      取れる(依頼の「実装が完了し、レビューも通ったら、次のタスクを
      割り当てる」という要件)。
    - レビュー担当は「自分以外の最初のメンバー」という単純な固定選択に
      した。レビュー依頼(問い合わせ1回)は、レビュー担当が自分自身の
      キュー処理で別のタスクを実装中であってもブロックせずに送られる。
      レビュー担当が参考にする「自分の担当ファイル」は、その時点までに
      そのメンバーが最後に完了させたファイルを使う(まだ何も完了させて
      いない場合はその旨をプロンプトに書く)。
    - メンバーが1台しかいない場合、担当外のレビュー担当を選べないため、
      そのファイルのレビューはスキップする(実装だけは行う)。この場合
      「レビュー」タスクはタスクチェックリスト上「未完了」のまま残り、
      最終チェックで検出される。

    `on_update`(PROGRESS.md永続化用)を渡すと、チェックリストの状態が
    変わるたび(実装・レビューの開始/完了)に呼び出される。`review_feedback`
    (dict、`{filename: 指摘内容}`)を渡すと、レビューが未解決のまま
    終わったファイルの最新の指摘内容がそこに記録され、解決すれば
    (再レビューが「問題なし」になれば)そのファイルのエントリは削除される。
    どちらも`None`の場合(既存の呼び出し元・テストとの互換性のため)は
    何もしない。
    """
    full_plan = "\n".join(f"{fn}: {content}" for fn, content in tasks)
    remaining = sorted(tasks, key=lambda t: _estimate_task_weight(t[1]), reverse=True)

    queue_lock = threading.Lock()
    print_lock = threading.Lock()
    latest_completed = {}  # candidate_label -> (filename, code)

    def pop_next():
        with queue_lock:
            if not remaining:
                return None
            task = remaining.pop(0)
            return task, list(remaining)

    def pick_reviewer(implementer):
        for c in candidates:
            if c["label"] != implementer["label"]:
                return c
        return None

    def worker(candidate):
        is_first = True
        while True:
            popped = pop_next()
            if popped is None:
                return
            (filename, content), remaining_after = popped

            remaining_desc = ", ".join(fn for fn, _ in remaining_after) if remaining_after else "なし"
            if is_first:
                _print_tagged(
                    print_lock, filename,
                    f"[📋 タスクキュー: {candidate['label']} に {filename} を割り当てました "
                    f"(残り{len(remaining_after)}件: {remaining_desc})]",
                )
            else:
                _print_tagged(
                    print_lock, filename,
                    f"[🙌 {candidate['label']} の手が空いたため、次のタスク {filename} を割り当てます "
                    f"(残り{len(remaining_after)}件: {remaining_desc})]",
                )
            is_first = False

            _set_task_status(checklist, filename, "impl", _TASK_STATUS_IN_PROGRESS)
            if on_update:
                on_update()
            request_text = _build_collaborative_implementation_request(filename, content, tasks)
            answer, error, truncated = _collect_answer_from_candidate(
                candidate, org_fingerprint, [{"role": "user", "content": request_text}],
            )
            # 仮の判断: ヘッダー行・回答本文(複数行になりうる)をまとめて
            # 1つの文字列として組み立ててから_print_taggedに渡すことで、
            # このタスクの実装結果プレビュー全体が1回のロック取得・1回の
            # print()で出力され、他のワーカースレッドの出力によって
            # 分断されないようにする。
            preview_lines = [f"--- {filename} ← {candidate['label']} (モデル: {candidate['model']}) ---"]
            if error or not answer:
                preview_lines.append(f"(問い合わせに失敗しました: {error or '応答なし'})")
            else:
                preview_lines.append(answer)
                if truncated:
                    preview_lines.append(f"(⚠️ 応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました。以降の構文チェック・レビューで検出されます)")
            _print_tagged(print_lock, filename, "\n".join(preview_lines))

            if error or not answer:
                logger.warning(
                    "%s の実装に失敗しました(担当: %s): %s", filename, candidate["label"], error or "応答なし",
                )
                # 実装できなかったファイルはタスクリスト上「未完了」のまま残り、
                # 最終チェックで検出される。このメンバーは次のタスクへ進む。
                continue

            code = _extract_code_from_answer(answer)
            os.makedirs(project_dir, exist_ok=True)
            dest_path = os.path.join(project_dir, filename)
            with open(dest_path, "w", encoding="utf-8") as f:
                f.write(code)
            _print_tagged(print_lock, filename, f"[💾 {dest_path} に保存しました (担当: {candidate['label']})]")
            _set_task_status(checklist, filename, "impl", _TASK_STATUS_COMPLETED)
            if on_update:
                on_update()

            reviewer = pick_reviewer(candidate)
            if reviewer is None:
                _print_tagged(
                    print_lock, filename,
                    f"[⚠️ {filename} のレビュー担当者がいません(メンバーが1台のみのため、レビューはスキップされます)]",
                )
                continue

            with queue_lock:
                reviewer_own = latest_completed.get(reviewer["label"])
            if reviewer_own:
                reviewer_own_filename, reviewer_own_code = reviewer_own
            else:
                reviewer_own_filename = "(まだ担当ファイルがありません)"
                reviewer_own_code = "(まだ実装したファイルがありません)"

            _set_task_status(checklist, filename, "review", _TASK_STATUS_IN_PROGRESS)
            if on_update:
                on_update()
            ok, feedback = _review_and_fix_one_file(
                filename=filename, owner=candidate, code=code,
                reviewer=reviewer, reviewer_own_filename=reviewer_own_filename, reviewer_own_code=reviewer_own_code,
                full_plan=full_plan, org_fingerprint=org_fingerprint, out_dir=project_dir, print_lock=print_lock,
                # 対話プロトコルの「レビュー用途での再利用」(依頼2.3): タスク
                # キュー方式による実装・レビューでは、実装担当に設計判断を
                # 短く説明させ、レビュー担当が疑問点をぶつける自己説明対話を
                # 常に行う。
                enable_self_explanation=True,
            )
            if ok:
                _set_task_status(checklist, filename, "review", _TASK_STATUS_COMPLETED)
            if review_feedback is not None:
                with queue_lock:
                    if ok:
                        review_feedback.pop(filename, None)
                    else:
                        review_feedback[filename] = feedback
            if on_update:
                on_update()

            with queue_lock:
                latest_completed[candidate["label"]] = (filename, code)

    print(f"[📋 タスクキュー方式で実装フェーズを開始します: {len(tasks)}件のタスクを{len(candidates)}台のメンバーで処理します]")
    threads = [threading.Thread(target=worker, args=(c,), daemon=True) for c in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------------------
# 未完了プロジェクトの再開(「巡回モード」・`//resume-all`)
# ---------------------------------------------------------------------------
#
# PROGRESS.mdにより、途中で終了したプロジェクトの状態がディスク上に
# 残るようになった。この状態を使って、未完了のタスクだけを対象に
# タスクキュー方式・レビューフェーズを再開する。

RESUME_ALL_COMMAND = "//resume-all"


def _resume_fix_project(project_dir: str, port: int, org_fingerprint: str, parsed: dict) -> bool:
    """//fixのタスク分割(`_run_fix_task_queue`)が、ツール呼び出しの
    往復回数の上限等で一部のサブタスクを完了させられなかった場合に、
    `//resume-all`から再開する経路(バグ報告への対応)。

    仮の判断: 合意フェーズ由来のモジュールタスクの再開
    (`_resume_project`本体、`_run_collaborative_project`を使う)とは
    全く別の仕組みが必要になる。fixのサブタスクは「ファイルを1から
    生成する」タスクではなく「既存のプロジェクトにプロジェクトツール
    (read_file/write_file等)で修正を加える」タスクのため、
    `_run_collaborative_project`にそのまま乗せると「サブタスクN」という
    仮の識別子をファイル名として扱ってしまい、そのファイル名で新規
    ファイルを作ろうとする誤動作になる。そのため`_resume_project`の
    入口でこちらに分岐し、//fix本来の実行経路(`_run_fix_task_queue`)を
    そのまま再利用する。
    """
    tasks = parsed["tasks"]
    language = parsed["language"] or _infer_language_from_tasks(tasks)
    full_plan = "\n".join(f"{fn}: {content}" for fn, content in tasks)
    file_list = _list_project_files(project_dir)
    pending_subtasks = parsed["pending_fix_subtasks"]
    pending_request = parsed["pending_fix_request"] or "(元の修正依頼の記録がありません。各サブタスクの説明を手がかりに判断してください)"

    if not file_list:
        print(f"[⚠️ {project_dir} にファイルが見つからないため、修正の再開をスキップします]")
        return False

    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        print(f"[⚠️ {project_dir}] 組織への問い合わせに失敗したため、修正の再開をスキップします。")
        return False
    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print(f"[⚠️ {project_dir}] 組織内にロード済みモデルを持つメンバーがいないため、修正の再開をスキップします。")
        return False

    print(
        f"[🔁 {project_dir} の未完了の修正サブタスクを再開します: {len(pending_subtasks)}件"
        f" ({', '.join(pending_subtasks)})]"
    )
    _run_fix_task_queue(
        pending_subtasks, candidates, org_fingerprint, project_dir, pending_request, parsed, full_plan, file_list, language,
    )
    return True


def _resume_project(project_dir: str, port: int, org_fingerprint: str, auto_resume_count: int = None) -> bool:
    """1つのプロジェクトディレクトリのPROGRESS.mdを読み込み、未完了の
    ファイルだけを対象にタスクキュー方式を再開する。PROGRESS.mdが
    読み取れない、組織に問い合わせできない等で再開自体を開始できな
    かった場合は`False`を返す(タスクの一部が未完了のまま処理自体は
    最後まで行えた場合は`True`を返す。「全タスクが完了したか」は
    呼び出し元がディスク上の状態を`_find_incomplete_projects`で
    再確認して判断する)。

    `auto_resume_count`は既定で`None`(=PROGRESS.mdに記録されている
    値をそのまま使う、つまり変更しない)。手動`//resume-all`
    (`_run_resume_all`)はこの既定のまま呼び出すため、自動再開の
    試行回数カウンタには一切触れない(依頼の要件5)。自動再開
    (`_maybe_auto_resume`)だけが、増分後の値を明示的に渡す。

    仮の判断(バグ報告への対応): PROGRESS.mdに未完了の修正サブタスク
    (`pending_fix_subtasks`、//fixのタスク分割が一部を完了させられ
    なかった場合に記録される)が残っている場合は、モジュールタスクの
    再開ロジックより先にそちらを`_resume_fix_project`へ委ねる。両者は
    独立した状態(前者はモジュールタスクのチェックリスト、後者は//fixの
    タスク分割の結果)のため、両方が同時に残っていることは通常無いが、
    仮に両方残っていた場合も、まず修正サブタスクを再開し、モジュール
    タスクの再開は次回の`//resume-all`に譲る(1回の再開で欲張らず、
    確実に完了させる)。
    """
    progress_path = os.path.join(project_dir, PROGRESS_FILENAME)
    parsed = _parse_progress_markdown(progress_path)
    if parsed is None:
        print(f"[⚠️ {progress_path} を読み取れなかったため、このプロジェクトの再開をスキップします]")
        return False

    if parsed.get("pending_fix_subtasks"):
        return _resume_fix_project(project_dir, port, org_fingerprint, parsed)

    request, tasks, checklist = parsed["request"], parsed["tasks"], parsed["checklist"]
    if auto_resume_count is None:
        auto_resume_count = parsed["auto_resume_count"]
    changelog = parsed["changelog"]
    # 仮の判断: この機能追加より前に作られたPROGRESS.md(「使用言語」の
    # 節が無い)を再開する場合、記録されている値は空文字列になる。せっかく
    # tasksの拡張子から言語を逆算できるので、その場で補完して以降の
    # PROGRESS.mdに書き戻す(既存プロジェクトを再開するたびに、言語設定が
    # 後から自然に付与される)。
    language = parsed["language"] or _infer_language_from_tasks(tasks)
    tasks_to_queue = _pending_tasks_from_checklist(tasks, checklist)
    if not tasks_to_queue:
        print(f"[{project_dir}] 未完了のタスクは見つかりませんでした。")
        return True

    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        print(f"[⚠️ {project_dir}] 組織への問い合わせに失敗したため、再開をスキップします。")
        return False
    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print(f"[⚠️ {project_dir}] 組織内にロード済みモデルを持つメンバーがいないため、再開をスキップします。")
        return False

    print(
        f"[🔁 {project_dir} を再開します: 未完了{len(tasks_to_queue)}件"
        f" ({', '.join(fn for fn, _ in tasks_to_queue)})]"
    )
    _run_collaborative_project(
        request, tasks, checklist, candidates, org_fingerprint, project_dir, tasks_to_queue,
        auto_resume_count, changelog, language,
    )
    return True


# 仮の判断: 未完了で終わった協業モードを、ユーザーの確認を挟まずに
# 自動的に再開できる上限回数。既存の「1ファイルあたりレビュー往復は
# 最大2回まで」という暴走防止の仕組み(内側の歯止め)とは独立した、
# プロジェクト単位での外側の歯止め。依頼で明示された「3回」を定数として
# 固定する(無制限に自動で回り続けることは絶対に避けたいという要件)。
_AUTO_RESUME_MAX_ATTEMPTS = 3


def _maybe_auto_resume(project_dir: str, port: int, org_fingerprint: str) -> None:
    """協業モードの1回分の実行(新規実行、またはこの関数自身による
    自動再開)が終わった直後に呼ばれる。プロジェクトがまだ未完了で、
    自動再開の試行回数(PROGRESS.mdに記録済み)が上限未満であれば、
    ユーザーの確認を挟まず自動的にもう一度そのプロジェクトを再開する
    (既存の`_resume_project`をそのまま使う)。上限に達している場合は、
    その旨をはっきりと報告して停止する。

    仮の判断: 試行回数はPROGRESS.md自身から呼び出しのたびに読み直す
    (メモリ上の引数として引き継がない)。read_fileツール・PROGRESS.md
    全体で踏襲している「ディスク上の実際の状態を正として扱う」方針に
    沿ったもので、対話モードのプロセスを再起動しても試行回数の記録が
    失われない。`_resume_project`に渡す増分後の試行回数は、
    `_run_collaborative_project`の最初の`write_progress()`呼び出しで
    即座にディスクへ書き込まれるため、この関数自身が別途書き込む
    必要はない。

    再帰呼び出しで次の試行を行う設計にしているが、上限(3回)により
    再帰の深さは物理的に3を超えない。
    """
    parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
    if parsed is None or not _progress_checklist_is_incomplete(parsed["checklist"]):
        return

    attempt_count = parsed["auto_resume_count"]
    if attempt_count >= _AUTO_RESUME_MAX_ATTEMPTS:
        print(
            f"[⛔ {project_dir}: 自動再開の上限({_AUTO_RESUME_MAX_ATTEMPTS}回)に達しました。"
            "これ以上の自動再開は行いません。人間の確認が必要です]"
        )
        return

    next_attempt = attempt_count + 1
    print(
        f"[🔁 {project_dir}: 未完了のタスクが残っているため、自動的に再開します"
        f"(試行 {next_attempt}/{_AUTO_RESUME_MAX_ATTEMPTS})]"
    )
    if _resume_project(project_dir, port, org_fingerprint, auto_resume_count=next_attempt):
        _maybe_auto_resume(project_dir, port, org_fingerprint)


def _run_resume_all(port: int, org_fingerprint: str, out_dir: str) -> None:
    """`./projects/`配下で未完了のPROGRESS.mdを持つプロジェクトを全て
    検出し、順番に(1つずつ)再開する「巡回モード」。

    仮の判断: 複数プロジェクトを並行に処理するのではなく、1つずつ順番に
    処理する。プロジェクトをまたいで並行実行すると、同じ組織のメンバーを
    複数のプロジェクトが同時に取り合うことになり、`_run_collaborative_
    task_queue`が前提とする「1プロジェクト内でメンバーを取り合う」設計を
    プロジェクトをまたいで多重に組み合わせることになって複雑さが増す
    (依頼でも「1つずつ、または可能な範囲で並行して」と、順次処理は
    明示的に許容されている)。この巡回モード自体は、対話モードの
    バックグラウンドジョブとして実行される(`_run_repl_client`参照)ため、
    処理中もユーザーは他の依頼を入力できる。
    """
    incomplete_projects = _find_incomplete_projects(out_dir)
    if not incomplete_projects:
        print("[未完了のプロジェクトは見つかりませんでした]")
        return

    print(f"[🔁 巡回モード開始: 未完了のプロジェクトが{len(incomplete_projects)}件見つかりました]")
    for project_dir in incomplete_projects:
        print(f"  - {project_dir}")

    for project_dir in incomplete_projects:
        _resume_project(project_dir, port, org_fingerprint)

    # 仮の判断: 依頼の「全て完了したら[✅ ...]とまとめて報告する」という
    # 文言はそのまま使うが、実際に全プロジェクトが完了したかどうかは
    # ディスク上の状態を再確認したうえで判定する。既存のタスクチェック
    # リストの最終チェックと同じ方針(「✅ 完了」を安易に名乗らず、正直に
    # 未完了を報告する)を、複数プロジェクトをまたいだこのモードでも
    # 踏襲する。
    print()
    still_incomplete = _find_incomplete_projects(out_dir)
    if still_incomplete:
        print(f"[⚠️ 巡回モード終了: {len(still_incomplete)}件のプロジェクトが未完了のまま残っています]")
        for project_dir in still_incomplete:
            print(f"  - {project_dir}")
    else:
        print("[✅ 全プロジェクトの未完了タスクが完了しました]")


# ---------------------------------------------------------------------------
# 既存プロジェクトへの修正依頼(`//fix`)
# ---------------------------------------------------------------------------
#
# これまでの協業モードは「新規のプロジェクトをゼロから作る」ことしか
# できず、既に完成したプロジェクトに対して「ここを直して」という
# 修正依頼を送る手段が無かった。Claude CodeにおけるRead/Glob/Grep/Edit
# のような、既存ファイルに対する検索・部分修正の仕組みを導入する。
#
# 対象は「完成済み」(タスクチェックリストが全項目✅)のプロジェクトに
# 限定する。未完了のプロジェクトは、既存の`//resume-all`(未完了タスクの
# 続きを実装する仕組み)の役割であり、両者を混同すると「未完了タスクの
# 続きを実装する」のか「完成済みの実装を書き換える」のかが曖昧になる。
# この制約により、修正依頼を処理する時点でそのプロジェクトの
# `review_feedback`(直近のレビュー指摘)は必ず空である(タスクキュー
# 方式は、あるファイルのレビューが完了した瞬間にそのファイルの指摘を
# 消すため、全項目✅の時点で指摘は残っていない)ことも保証され、
# 修正依頼のPROGRESS.md更新が既存の指摘を誤って消してしまうことも無い。

FIX_PROJECT_COMMAND = "//fix"


def _char_bigrams(text: str) -> set:
    normalized = text.lower()
    if len(normalized) < 2:
        return set()
    return {normalized[i:i + 2] for i in range(len(normalized) - 1)}


def _text_similarity_score(a: str, b: str) -> int:
    """2つの文字列の類似度を、文字2-gram(バイグラム)の共通数で簡易的に
    スコア化する。

    仮の判断: 日本語は分かち書き(単語の区切り)が無いため、形態素解析
    ライブラリを新たに追加せずに大まかな重なりを見る手法として、文字
    単位のn-gramを使う。英数字の識別子(例: "generate_id")の部分一致も
    自然に拾える(バイグラムは文字種を問わず作れるため)という利点もある。
    """
    return len(_char_bigrams(a) & _char_bigrams(b))


# 仮の判断: この点数未満の重なりしか無い場合は「一致する候補が無い」と
# みなす。文字バイグラムの共通数という単純な指標のため、依頼文と
# プロジェクトの要約が何らかの形で具体的に関連していることを示すための
# 最低限のしきい値として設定した(厳密な根拠のある値ではなく、依頼の
# 「判定に自信が持てない場合は確認する」という要件を安全側に倒すための
# 目安)。
_PROJECT_MATCH_MIN_SCORE = 2


def _project_summary_text(parsed: dict) -> str:
    return parsed["request"] + "\n" + "\n".join(f"{fn}: {content}" for fn, content in parsed["tasks"])


def _identify_target_project(request_text: str, out_dir: str) -> tuple:
    """依頼文と、`./projects/`以下の完成済み(全タスク✅かつ//fixの
    未完了サブタスクも無い)プロジェクトのPROGRESS.md(元の依頼・モジュール
    分割案)を照合し、どのプロジェクトについての依頼かを判定する。

    戻り値は`(status, project_dirs)`のタプル。
    - `status == "matched"`: `project_dirs == [project_dir]`
      (1件に絞り込めた)
    - `status == "ambiguous"`: `project_dirs == [project_dir, ...]`
      (複数の候補が同点で並んだ)
    - `status == "pending_fix"`: `project_dirs == [project_dir]`
      (依頼文に最も近いプロジェクトは見つかったが、未完了の作業
      (モジュールタスク、または//fixのタスク分割の残り)が残っており、
      新規の//fixの対象にはできない)
    - `status == "not_found"`: `project_dirs == []`
      (完成済み・未完了を問わず、どのプロジェクトとも十分に一致しなかった)

    仮の判断(バグ報告への対応): 以前は未完了の作業が残るプロジェクトを
    候補から単純に除外していたため、依頼文に最も近いのがまさにその
    プロジェクトだった場合、「見つかりませんでした。新規に//agreeで
    作ってください」という誤った案内になってしまっていた
    (実機で、//fixのタスク分割が一部未完了のまま残っていたプロジェクトに
    対して次の//fixを送ったところ、この誤案内が発生し、ユーザーが
    誤って重複した新規プロジェクトを作ってしまいかねない状況になった)。
    完成済みの候補が見つからない場合でも、未完了の候補の中に十分近い
    ものがあれば、それを「未完了」として区別して報告し、
    `//resume-all`へ誘導する。
    """
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    if not os.path.isdir(projects_root):
        return "not_found", []

    completed_scored = []
    pending_scored = []
    for name in sorted(os.listdir(projects_root)):
        project_dir = os.path.join(projects_root, name)
        parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
        if parsed is None:
            continue
        score = _text_similarity_score(request_text, _project_summary_text(parsed))
        if _project_has_pending_work(parsed):
            pending_scored.append((score, project_dir))
        else:
            completed_scored.append((score, project_dir))

    if completed_scored:
        completed_scored.sort(key=lambda item: item[0], reverse=True)
        top_score = completed_scored[0][0]
        if top_score >= _PROJECT_MATCH_MIN_SCORE:
            top_candidates = [project_dir for score, project_dir in completed_scored if score == top_score]
            if len(top_candidates) == 1:
                return "matched", top_candidates
            return "ambiguous", top_candidates

    if pending_scored:
        pending_scored.sort(key=lambda item: item[0], reverse=True)
        top_score, top_project_dir = pending_scored[0]
        if top_score >= _PROJECT_MATCH_MIN_SCORE:
            return "pending_fix", [top_project_dir]

    return "not_found", []


def _resolve_explicit_fix_target(request_text: str, out_dir: str) -> tuple:
    """`//fix <プロジェクト名>: <修正依頼>`という明示指定の構文を試みる。
    コロンより前の部分が既存プロジェクトのディレクトリ名と一致すれば
    `(project_dir, 残りの依頼文)`を返す。一致しなければ`(None,
    request_text)`を返し、呼び出し元に自動判定(`_identify_target_
    project`)させる。
    """
    if ":" not in request_text:
        return None, request_text
    prefix, _sep, rest = request_text.partition(":")
    prefix = prefix.strip()
    if not prefix:
        return None, request_text
    candidate_dir = os.path.join(out_dir, PROJECTS_SUBDIR_NAME, prefix)
    if os.path.isfile(os.path.join(candidate_dir, PROGRESS_FILENAME)):
        return candidate_dir, rest.strip()
    return None, request_text


def _list_project_files(project_dir: str) -> list:
    """プロジェクトディレクトリ内(サブディレクトリを含む)の、
    PROGRESS.md以外のファイルの相対パス一覧をソート済みで返す。
    相対パスの区切りは常にスラッシュ('/')にする(read_file・write_file
    等の引数としてそのまま使える、OS非依存の表記にするため)。

    仮の判断(バグ報告への対応): `_resolve_safe_project_path`が
    サブディレクトリ内のパスを許可するようになったことに合わせ、
    直下だけでなく再帰的に走査する。これが無いと、サブディレクトリ内に
    実際に書き込まれたファイルが、修正担当への「現在保存されている
    ファイル一覧」やlist_dir・構文チェック(`_syntax_check_all_files`)
    から見えなくなってしまう。
    """
    if not os.path.isdir(project_dir):
        return []
    files = []
    for root, _dirs, filenames in os.walk(project_dir):
        for name in filenames:
            if name == PROGRESS_FILENAME:
                continue
            rel = os.path.relpath(os.path.join(root, name), project_dir).replace(os.sep, "/")
            files.append(rel)
    return sorted(files)


# 仮の判断: 依頼の項目2(既存のread_fileツールの仕組みを流用し、
# プロジェクト内の全ファイルを確認できるようにする)への対応として、
# 「プロジェクト内でのファイル操作・コマンド実行ツール」の追加に伴い、
# 修正担当はread_fileだけでなくlist_dir・write_file・move_file・
# delete_file・make_directory・run_commandも自由に使い、複数ファイル・
# 複数手順にまたがる修正(リネーム→import修正→動作確認、等)を1回の
# 依頼の中で完結できるようにした(_collect_answer_with_project_tools)。
# 以前の「FILE: <ファイル名> + コードブロック1つだけを解析して1ファイルを
# 上書きする」設計は、複数ファイルにまたがる修正を表現できないため廃止した。
#
# 仮の判断(run_commandへの刷新に伴うプロンプト更新): 依頼の項目5
# 「ファイルの検証にはrun_commandを使い、そのファイルに適したコマンドを
# 自分で判断すること。ただしネットワークアクセスや削除操作は別のツールで
# 行う必要がある」という指示を明示的に加えた。run_commandは以前のような
# 拡張子ごとの決め打ちコマンドを提供しないため、この指示が無いとモデルが
# 検証を試みずに済ませてしまう・誤って禁止されたコマンドを繰り返し試す、
# のどちらかに陥りやすいと判断したため。
_FIX_REQUEST_TOOL_PROMPT_TEMPLATE = """あなたはこのプロジェクトの改修担当です。以下はこのプロジェクトの実装計画全体と、現在保存されているファイルの一覧です。

【このプロジェクトの使用言語】
{language}

【実装計画全体】
{full_plan}

【現在保存されているファイル一覧】
{file_list}

【ユーザーからの修正依頼】
{request}

以下のツールを使って、このプロジェクトディレクトリ内のファイルに直接修正を加えてください。
- search_in_file: 既存ファイル内でキーワードや関数名を検索し、該当行番号とその前後の抜粋だけを確認する。ファイルが大きい場合、read_fileでいきなり全体を読む前にまずこれで見当をつけると効率的。
- read_file: 既存ファイルの中身を確認する(filenameのみ指定するとファイル全体、start_line/end_line(またはline+context_lines)を指定するとその範囲だけを読める)
- list_dir: ファイル一覧を確認する
- write_file: ファイルを新規作成・上書きする(部分修正の場合も、まずread_fileで現在の中身を確認したうえでファイル全体を書き直すこと)
- move_file: ファイル名を変更(移動)する
- make_directory: サブディレクトリを作成する
- delete_file: 不要になったファイルを削除する(取り消せないため、本当に不要な場合のみ使うこと)
- run_command: ファイルの検証(動作確認)に使う。そのファイルの種類に適したコマンドを自分で判断して実行すること(例: .pyファイルなら'python3 <ファイル名>'、.jsファイルなら'node --check <ファイル名>'、.cファイルなら'gcc -fsyntax-only <ファイル名>'、.html/.cssファイルなら'check_html <HTMLファイル名>')。ただし、ネットワークアクセス(curl・wget・ssh等)・ファイルの削除や移動(rm・mv・cp等)・権限昇格(sudo等)を行うコマンドは実行できない(拒否される)。ファイルの削除・移動・作成・上書きは、run_commandではなく必ずdelete_file・move_file・write_fileを使うこと。

{search_then_read_guidance}

関連するファイル(import元・import先など)がある場合は、read_fileで確認したうえで、必要なファイルすべてに修正を反映してください。修正が完了したら、最後にツールを呼び出さずに、何をどう変更したかを簡潔な日本語の文章で報告してください。
"""


def _resolve_and_validate_fix_target(request_text: str, out_dir: str) -> tuple:
    """`//fix`の依頼文から対象プロジェクトを解決する。

    仮の判断:
    - プロジェクトの特定は、まず`//fix <プロジェクト名>: <依頼>`という
      明示指定を試み、それが無ければ依頼文とPROGRESS.mdの内容を照合する
      自動判定(`_identify_target_project`)に委ねる。複数の候補が拮抗
      した場合・十分な一致が無い場合・対象が未完了の場合は、ここで
      ユーザーに確認を求める案内をprintしたうえで`(None, None)`を返し、
      その場で推測して処理を進めることはしない。
    - 対話的な修正セッション(`_FixSession`)が2回目以降の発言では
      この特定処理をやり直さずに済むよう、特定処理そのものを実際の
      修正の実行(`_run_fix_on_project`)から分離した。`_run_repl_client`は
      この関数を(バックグラウンドジョブに載せず)同期的に呼んでセッションを
      開始・継続するかどうかを判断し、成功した場合だけ
      `_run_fix_on_project`をバックグラウンドジョブとして投入する。
    """
    explicit_project_dir, request = _resolve_explicit_fix_target(request_text, out_dir)
    if explicit_project_dir:
        project_dir = explicit_project_dir
    else:
        status, candidate_dirs = _identify_target_project(request_text, out_dir)
        if status == "not_found":
            print(
                "[⚠️ 修正依頼の対象となるプロジェクトが見つかりませんでした。"
                "新規の依頼として作成したい場合は、通常の会話や"
                f"{AGREE_COMMAND}をお使いください]"
            )
            return None, None
        if status == "pending_fix":
            print(
                f"[⚠️ {candidate_dirs[0]} が対象と思われますが、まだ未完了のタスクが残っています。"
                f"先に{RESUME_ALL_COMMAND}で完了させてから修正を依頼してください]"
            )
            return None, None
        if status == "ambiguous":
            names = ", ".join(os.path.basename(d) for d in candidate_dirs)
            example = os.path.basename(candidate_dirs[0])
            print(f"[❓ 複数のプロジェクトが候補に挙がりました: {names}]")
            print(f"どのプロジェクトのことでしょうか? {FIX_PROJECT_COMMAND} {example}: {request_text} のように、プロジェクト名を付けて教えてください。")
            return None, None
        project_dir = candidate_dirs[0]
        request = request_text

    parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
    if parsed is None:
        print(f"[⚠️ {project_dir} のPROGRESS.mdを読み取れなかったため、修正を中断します]")
        return None, None
    if _project_has_pending_work(parsed):
        print(
            f"[⚠️ {project_dir} はまだ未完了のタスクが残っています。"
            f"先に{RESUME_ALL_COMMAND}で完了させてから修正を依頼してください]"
        )
        return None, None

    return project_dir, request


def _build_fix_dialogue_output_instruction() -> str:
    return (
        "合意した修正方針を、担当メンバーへの依頼文にそのまま使える形で2〜5文程度にまとめてください。"
        "変更すべき箇所・手順・(あれば)検証方法を具体的に書いてください。他の説明文や前置きは不要です。"
    )


def _run_fix_approach_dialogue(
    org_fingerprint: str, request: str, candidates: list, full_plan: str, file_list: list, language: str,
    project_dir: str,
) -> tuple:
    """//fixの修正方針についても、対話プロトコルによる事前合意を挟む
    (依頼の「//fix修正フェーズでの利用」)。戻り値は
    `(修正方針を反映した依頼文, 中断すべきかどうか)`。

    仮の判断: バックエンドへの疎通自体ができていない場合にまで対話の
    不調を理由に修正を止めてしまうと、既存の`_decide_fix_task_split`
    (問い合わせに失敗した場合は安全側にフォールバックする)と一貫しない
    体験になる。そのため、提案役から一度も実のある応答が得られなかった
    場合(`DIALOGUE_STATUS_NO_ENGAGEMENT`)は、対話を行わなかったことに
    して元の依頼文のまま処理を続行する。実際に議論が行われたが合意に
    至らなかった場合(セーフティリミット・人間の確認が必要)は、依頼の
    要件通り議事録・要約を保存したうえで中断する。
    """
    architect = candidates[0]
    print(f"[🧭 対話プロトコルによる修正方針の事前合意: {architect['label']}さんを中心に議論します...]")
    background = (
        f"【このプロジェクトの使用言語】\n{language}\n\n【実装計画全体】\n{full_plan}\n\n"
        f"【現在保存されているファイル一覧】\n{chr(10).join(file_list)}\n\n【ユーザーからの修正依頼】\n{request}"
    )
    result = _run_dialogue(
        org_fingerprint=org_fingerprint,
        topic=f"修正依頼「{request}」の方針",
        background=background,
        candidates=candidates,
        output_instruction=_build_fix_dialogue_output_instruction(),
        tag="修正方針",
    )
    if result["status"] == DIALOGUE_STATUS_NO_ENGAGEMENT:
        return request, False

    _report_dialogue_result(result, project_dir, "fix", f"修正依頼「{request}」の方針")

    if result["status"] != DIALOGUE_STATUS_CONSENSUS:
        return request, True

    agreed_approach = (result["final_content"] or "").strip()
    if not agreed_approach:
        return request, False
    print("[🧭 対話プロトコルで合意した修正方針]")
    print(agreed_approach)
    return f"{request}\n\n[対話プロトコルで事前合意した修正方針]\n{agreed_approach}", False


def _ask_organization_fix_project(
    port: int, org_fingerprint: str, request_text: str, out_dir: str, enable_dialogue: bool = False,
) -> None:
    """既存の完成済みプロジェクトへの修正依頼(//fixの単発呼び出し)を
    処理する。対象プロジェクトの解決は`_resolve_and_validate_fix_target`、
    実際の修正の実行は`_run_fix_on_project`に委ねる(分割の理由は
    `_resolve_and_validate_fix_target`のdocstringを参照)。

    `enable_dialogue`は`_run_fix_on_project`にそのまま引き継ぐ(既定は
    `False`のまま、既存の呼び出し元・テストとの後方互換性のため)。
    """
    project_dir, request = _resolve_and_validate_fix_target(request_text, out_dir)
    if project_dir is None:
        return
    _run_fix_on_project(port, org_fingerprint, project_dir, request, out_dir, enable_dialogue=enable_dialogue)


# ---------------------------------------------------------------------------
# //fixの規模判定・タスク分割(//agreeのタスクキュー方式の//fixへの再利用)
# ---------------------------------------------------------------------------
#
# これまでの//fixは、依頼の大小に関わらず常に1人のメンバーに丸ごと修正を
# 依頼していた。実機で、教材コンテンツの創作を含む複数ファイルにまたがる
# 大規模な修正依頼を送ったところ、1台のメンバーがそれを丸ごと処理しようと
# して応答時間の増大・タイムアウトにつながる不具合が報告された。
#
# //agree(新規プロジェクト作成)は、合意フェーズで複数ファイルに分割し、
# タスクキュー方式(`_run_collaborative_task_queue`)で複数メンバーが分担する
# 仕組みを既に持っている。この考え方を//fixにも導入する: 依頼を受けた直後に
# 「1人で完結できる規模か、複数のサブタスクに分けるべき規模か」を判定し、
# 分割すべきと判断された場合のみ、同じ「ワーカーがキューから次々にタスクを
# 取る」設計(`_run_fix_task_queue`)に乗せて複数メンバーで分担する。小さな
# 修正まで毎回この判定を挟むとオーバーヘッドになるため、分割不要と判断
# された場合はこれまで通り1人のメンバーに直接依頼する(既存の単一実装
# 経路は一切変更しない)。

_FIX_SPLIT_DECISION_PROMPT_TEMPLATE = """あなたはこのプロジェクトの改修判断を担当します。以下のプロジェクト情報と修正依頼を確認し、この修正を1人のメンバーが一度に実行できる規模か、それとも複数の独立したサブタスクに分けて複数メンバーで分担すべき規模かを判断してください。

【このプロジェクトの使用言語】
{language}

【実装計画全体】
{full_plan}

【現在保存されているファイル一覧】
{file_list}

【ユーザーからの修正依頼】
{request}

判断基準:
- 修正内容が単純(見た目の微調整・1箇所だけの挙動修正など)で、1人が一度に実行しても長時間化しなさそうであれば「分割不要」と判断してください。
- 複数ファイルにまたがる大規模な変更、まとまった分量のコンテンツ作成(例: 複数レッスン分の教材追加)、独立して並行実装できる複数の変更点を含む場合は、分割してください。

出力は次のいずれかの形式のみとし、他の説明文は含めないでください。

分割不要と判断した場合、次の1行だけを出力してください:
分割不要

分割すべきと判断した場合は、2〜5個程度の、互いに独立して実装できるサブタスクに分け、1行に1つずつ「- <サブタスクの内容>」の形式で列挙してください(このサブタスクの一覧以外の説明文は書かないでください):
- <サブタスク1の内容>
- <サブタスク2の内容>
"""


def _decide_fix_task_split(
    candidate: dict, org_fingerprint: str, request: str, full_plan: str, file_list: list, language: str,
) -> list:
    """//fixの依頼規模を、依頼の実装担当予定のメンバー(`candidate`)に
    問い合わせて判定する。分割すべきと判断された場合はサブタスクの説明
    (2件以上)のリストを、分割不要と判断された場合は空リストを返す。

    仮の判断: 問い合わせ自体に失敗した場合・応答が期待した形式で解釈
    できなかった場合も、安全側(=分割しない、これまで通り1人に依頼する)
    にフォールバックする。判定のための余計な往復でユーザーの依頼が
    宙に浮くことを避けるため。
    """
    print("[🔍 修正の規模を判定しています...]")
    prompt = _FIX_SPLIT_DECISION_PROMPT_TEMPLATE.format(
        full_plan=full_plan, file_list="\n".join(file_list), request=request, language=language,
    )
    answer, error, _truncated = _collect_answer_from_candidate(
        candidate, org_fingerprint, [{"role": "user", "content": prompt}],
    )
    if error or not answer:
        return []
    return _parse_fix_split_subtasks(answer)


def _parse_fix_split_subtasks(text: str) -> list:
    """判定担当の回答から、分割後の各サブタスクの説明を取り出す
    (`_parse_bullet_lines`で行頭が箇条書き記号(-・*)の行だけを抽出)。
    2件未満しか取り出せない場合は「分割不要」の判断とみなし空リストを
    返す(「分割不要」という見出し文言そのものではなく、実際に列挙
    されたサブタスクの件数を見ることで、モデルの言い回しの揺れに対しても
    頑健に判定する)。PROGRESS.mdに書き出した「未完了の修正サブタスク」を
    読み戻す用途では、この2件未満の足切りは不適切(1件だけ残っている
    正当なケースを消してしまう)なため、そちらは`_parse_bullet_lines`を
    直接使う。
    """
    subtasks = _parse_bullet_lines(text)
    return subtasks if len(subtasks) >= 2 else []


_FIX_SUBTASK_REVIEW_PROMPT_TEMPLATE = """あなたはこのプロジェクトの改修レビュー担当です。以下のサブタスクは、組織内の別のメンバーが実装済みです。実際に変更されたファイルの中身をread_file・search_in_file等で確認し、問題があれば自分でwrite_file等のツールを使って直してください。

【このプロジェクトの使用言語】
{language}

【実装計画全体】
{full_plan}

【担当メンバーが取り組んだサブタスク】
{subtask}

【実際に変更されたファイル】
{modified_files}

確認した結果、問題が無ければツールを何も呼び出さずに「問題なし」とだけ回答してください。問題があれば、write_file等のツールで実際に修正したうえで、何をどう直したかを簡潔に報告してください。
"""


def _build_fix_subtask_review_prompt(subtask: str, full_plan: str, language: str, modified_files: list) -> str:
    return _FIX_SUBTASK_REVIEW_PROMPT_TEMPLATE.format(
        subtask=subtask, full_plan=full_plan, language=language,
        modified_files=", ".join(modified_files) if modified_files else "(不明)",
    )


def _finalize_fix_changes(
    project_dir: str, request: str, parsed: dict, language: str, modified_files: list, error: str,
    pending_fix_request: str = "", pending_fix_subtasks: list = None,
) -> None:
    """//fixで実際に確認された変更を受けて、プロジェクト全体の構文チェック・
    PROGRESS.mdへの更新履歴の追記・最終報告までを行う。通常の単一実装
    //fix・分割タスクキュー(`_run_fix_task_queue`)のどちらから呼ばれても
    同じ記録・報告の仕組みを使う(依頼の項目3: 「PROGRESS.mdへの記録も、
    通常の//fixと同様に適用する」への対応。元々`_run_fix_on_project`に
    直接書かれていた処理をそのまま切り出したもので、動作は変えていない)。

    呼び出し前に`modified_files`が空でないことは呼び出し元が保証すること。
    1件も変更が無い場合の「実行されませんでした」という報告は、単一実装・
    分割それぞれの文脈で言い回しが異なるため、呼び出し元側で行う。

    `pending_fix_subtasks`(バグ報告への対応: `_run_fix_task_queue`が
    往復回数の上限等で一部のサブタスクを完了させられなかった場合に、
    その説明のリストを渡す)を渡すと、`pending_fix_request`とあわせて
    PROGRESS.mdに「未完了の修正サブタスク」として記録し、次回の
    `//resume-all`で再開できるようにする。空リスト・省略時は、この節を
    書かない(=既に記録されていた未完了状態も、今回すべて完了したなら
    正しくクリアされる)。
    """
    broken_files = _syntax_check_all_files(project_dir)

    # 仮の判断: delete_fileツールが修正セッション中にPROGRESS.mdを既に
    # 更新している可能性がある(依頼の項目4)ため、ここでは最初に読み込んだ
    # parsedを使い回さず、ディスク上の最新の状態を読み直してから
    # changelogに1件追記する(その更新履歴を上書きで消してしまわないため)。
    latest_parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME)) or parsed
    today = datetime.date.today().isoformat()
    # 仮の判断: 「PROGRESS.mdの更新履歴が、実際の変更内容と一致している
    # ことを確認したい」という依頼の動作確認要件に応えるため、実際に変更が
    # 確認できたファイル名を更新履歴にも明記する(重複は除いて表示する)。
    unique_modified_files = list(dict.fromkeys(modified_files))
    file_note = f" [変更ファイル: {', '.join(unique_modified_files)}]"
    error_note = f" (セッションが最後まで完了しませんでした: {error})" if error else ""
    syntax_note = f" (構文エラーが残っています: {', '.join(broken_files)})" if broken_files else ""
    changelog = list(latest_parsed["changelog"])
    changelog.append(f"- {today}: {request.strip()}{file_note}{error_note}{syntax_note}")
    _write_progress_md(
        project_dir, latest_parsed["request"], latest_parsed["tasks"], latest_parsed["checklist"],
        review_feedback={}, auto_resume_count=latest_parsed["auto_resume_count"], changelog=changelog,
        language=latest_parsed["language"] or language,
        pending_fix_request=pending_fix_request if pending_fix_subtasks else "",
        pending_fix_subtasks=pending_fix_subtasks or [],
    )

    if error:
        print(
            f"[⚠️ 修正セッションが最後まで正常に完了しませんでした({error})。"
            f"ただし一部のファイルは実際に変更されています: {', '.join(unique_modified_files)}]"
        )
    elif not broken_files:
        print(f"[✅ 修正が完了しました (プロジェクト: {project_dir}, 変更したファイル: {', '.join(unique_modified_files)})]")
    else:
        print(f"[⚠️ 修正は保存されましたが、構文エラーが残っているファイルがあります: {', '.join(broken_files)}]")

    if pending_fix_subtasks:
        print(
            f"[🔁 {len(pending_fix_subtasks)}件のサブタスクが未完了のため、"
            f"{RESUME_ALL_COMMAND}で再開できます]"
        )


def _persist_pending_fix_subtasks(
    project_dir: str, parsed: dict, request: str, pending_fix_subtasks: list, language: str,
) -> None:
    """`_run_fix_task_queue`が1件もファイルを変更できないまま終わった
    場合(`_finalize_fix_changes`は呼ばれない)でも、未完了のサブタスクは
    見失わずPROGRESS.mdに記録して`//resume-all`から再開できるようにする。

    仮の判断: ここでは構文チェック・changelogへの追記は行わない。
    「未完了のサブタスクが残っている」という事実と「実際に何らかの変更が
    行われた」という事実は別物であり、後者の記録・報告は
    (呼ばれる場合の)`_finalize_fix_changes`の責務のままにする
    (changelogのエントリが実際の変更を伴わずに記録されることを防ぐ、
    既存の設計方針を崩さないため)。
    """
    latest_parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME)) or parsed
    _write_progress_md(
        project_dir, latest_parsed["request"], latest_parsed["tasks"], latest_parsed["checklist"],
        review_feedback={}, auto_resume_count=latest_parsed["auto_resume_count"],
        changelog=latest_parsed["changelog"], language=latest_parsed["language"] or language,
        pending_fix_request=request if pending_fix_subtasks else "",
        pending_fix_subtasks=pending_fix_subtasks,
    )
    if pending_fix_subtasks:
        print(
            f"[🔁 {len(pending_fix_subtasks)}件のサブタスクが未完了のため、"
            f"{RESUME_ALL_COMMAND}で再開できます]"
        )


def _pending_subtask_descriptions(checklist: list, numbered_subtasks: list) -> list:
    """タスクキュー実行後のチェックリストから、まだ完了していない
    (実装・レビューのいずれかが未完了の)サブタスクの説明文を返す。
    チェックリスト上の仮の識別子(「サブタスクN」)を、`numbered_subtasks`
    (`[(N, サブタスクの説明文), ...]`)を使って元の説明文に逆引きする。
    """
    pending = []
    for index, subtask in numbered_subtasks:
        task_key = f"サブタスク{index}"
        statuses = [task["status"] for task in checklist if task["filename"] == task_key]
        if any(status != _TASK_STATUS_COMPLETED for status in statuses):
            pending.append(subtask)
    return pending


def _run_fix_task_queue(
    subtasks: list, candidates: list, org_fingerprint: str, project_dir: str,
    request: str, parsed: dict, full_plan: str, file_list: list, language: str,
) -> None:
    """//fixが大規模と判定した修正依頼を、`_run_collaborative_task_queue`と
    同じ「ワーカーがキューから次々にサブタスクを取る」設計に乗せ、複数
    メンバーで分担して処理する(依頼の項目3)。

    仮の判断:
    - 各サブタスクの「実装」は、通常の単一実装//fixと全く同じ
      `_collect_answer_with_project_tools`(read_file/write_file等の
      プロジェクトツール一式)を使う。担当メンバーは依頼文(サブタスクの
      説明)だけを受け取り、修正すべきファイルの特定も含めて自分で判断する
      (通常の//fixと同じ設計思想)。
    - 「レビュー」は、//agreeの相互レビュー(単一ファイルの内容全体を
      突き合わせるテンプレート、`_REVIEW_PROMPT_TEMPLATE`)をそのまま
      流用するのではなく、別のメンバーに同じプロジェクトツール一式を
      与えて「実装担当が変更したファイルを確認し、問題があれば自分で
      直してよい」という形にした。//fixはモデル自身がread_file/write_file
      で複数ファイルを自由に操作する設計のため、単一ファイルの内容を
      丸ごと比較する既存のレビュー用テンプレートはこの設計とはかみ合わない。
    - タスクの重さの見積もり(`_estimate_task_weight`、重い順にキューへ
      並べる)・チェックリストの表現(`_build_task_checklist`/
      `_format_task_checklist`/`_incomplete_task_labels`)は、//agreeの
      タスクキューと全く同じものをそのまま再利用する(依頼の「車輪の
      再発明はせず、既存の仕組みをできる限り流用してほしい」への対応)。
      サブタスクには実ファイル名が無いため、チェックリスト上は
      「サブタスクN」という仮の識別子を「ファイル名」として扱う。
    - メンバーが1台しかない場合、担当外のレビュー担当を選べないため、
      そのサブタスクのレビューはスキップする(実装だけは行う)。
    """
    numbered_subtasks = list(enumerate(subtasks, start=1))
    numbered_subtasks.sort(key=lambda t: _estimate_task_weight(t[1]), reverse=True)
    checklist = _build_task_checklist([(f"サブタスク{i}", st) for i, st in numbered_subtasks])
    print(_format_task_checklist(checklist))

    queue_lock = threading.Lock()
    print_lock = threading.Lock()
    remaining = list(numbered_subtasks)
    all_modified_files = []
    session_errors = []

    def pop_next():
        with queue_lock:
            if not remaining:
                return None
            return remaining.pop(0)

    def record_modified(files):
        if not files:
            return
        with queue_lock:
            all_modified_files.extend(files)

    def record_error(note):
        with queue_lock:
            session_errors.append(note)

    def worker(candidate):
        while True:
            popped = pop_next()
            if popped is None:
                return
            index, subtask = popped
            task_key = f"サブタスク{index}"

            _print_tagged(
                print_lock, task_key,
                f"[📋 タスクキュー: {candidate['label']} に {task_key}「{subtask}」を割り当てました]",
            )
            _set_task_status(checklist, task_key, "impl", _TASK_STATUS_IN_PROGRESS)

            impl_prompt = _FIX_REQUEST_TOOL_PROMPT_TEMPLATE.format(
                full_plan=full_plan, file_list="\n".join(file_list), request=subtask, language=language,
                search_then_read_guidance=_SEARCH_THEN_READ_GUIDANCE,
            )
            summary, error, truncated, modified = _collect_answer_with_project_tools(
                candidate, org_fingerprint, [{"role": "user", "content": impl_prompt}], project_dir,
                print_lock=print_lock, tag=task_key,
            )
            record_modified(modified)
            preview_lines = [f"--- {task_key} ← {candidate['label']} (モデル: {candidate['model']}) ---"]
            if error:
                preview_lines.append(f"(問い合わせに失敗しました: {error})")
            elif summary:
                preview_lines.append(summary)
                if truncated:
                    preview_lines.append(f"(⚠️ 応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました)")
            _print_tagged(print_lock, task_key, "\n".join(preview_lines))

            if error:
                record_error(f"{task_key}: {error}")
                # 実装が完了しなかったサブタスクはチェックリスト上「未完了」の
                # まま残り、最終チェックで検出される。このメンバーは次の
                # サブタスクへ進む。
                continue

            _set_task_status(checklist, task_key, "impl", _TASK_STATUS_COMPLETED)

            if not modified:
                # このサブタスクでは実際のファイル変更が無かった(=判断の
                # 結果、何もする必要が無かった)ため、レビューする対象も
                # 無く、そのままレビュー済み扱いにして次へ進む。
                _set_task_status(checklist, task_key, "review", _TASK_STATUS_COMPLETED)
                continue

            reviewer = next((c for c in candidates if c["label"] != candidate["label"]), None)
            if reviewer is None:
                _print_tagged(
                    print_lock, task_key,
                    f"[⚠️ {task_key} のレビュー担当者がいません(メンバーが1台のみのため、レビューはスキップされます)]",
                )
                continue

            _set_task_status(checklist, task_key, "review", _TASK_STATUS_IN_PROGRESS)
            review_prompt = _build_fix_subtask_review_prompt(
                subtask, full_plan, language, list(dict.fromkeys(modified)),
            )
            review_summary, review_error, review_truncated, review_modified = _collect_answer_with_project_tools(
                reviewer, org_fingerprint, [{"role": "user", "content": review_prompt}], project_dir,
                print_lock=print_lock, tag=task_key,
            )
            record_modified(review_modified)
            review_preview = [f"--- {task_key} のレビュー ← {reviewer['label']} (モデル: {reviewer['model']}) ---"]
            if review_error:
                review_preview.append(f"(レビューの問い合わせに失敗しました: {review_error})")
            elif review_summary:
                review_preview.append(review_summary)
            _print_tagged(print_lock, task_key, "\n".join(review_preview))

            if review_error:
                record_error(f"{task_key}のレビュー: {review_error}")
                continue

            _set_task_status(checklist, task_key, "review", _TASK_STATUS_COMPLETED)

    threads = [threading.Thread(target=worker, args=(c,)) for c in candidates]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print()
    print(_format_task_checklist(checklist))
    incomplete_labels = _incomplete_task_labels(checklist)
    if incomplete_labels:
        print(f"[⚠️ 未完了のサブタスクが残っています: {', '.join(incomplete_labels)}]")

    # 仮の判断(バグ報告への対応): 完了しなかったサブタスク(実装・レビュー
    # のいずれかが未完了)の説明文を、チェックリスト上の仮の識別子
    # (「サブタスクN」)から逆引きしておく。このリストをPROGRESS.mdに
    # 記録しない場合、往復回数の上限等で一部だけ未完了のまま終わっても
    # `//resume-all`からはその状態が一切見えず、再開する手段が無くなって
    # しまう(実機で報告された不具合)。
    still_pending_subtasks = _pending_subtask_descriptions(checklist, numbered_subtasks)

    combined_error = "; ".join(session_errors) if session_errors else None
    if not all_modified_files:
        if combined_error:
            print(f"修正の問い合わせに失敗しました: {combined_error}")
        else:
            print(
                "[❓ どのサブタスクでも実際のファイル変更が行われませんでした"
                "(修正は実行されませんでした)。"
                "もう少し具体的に(ファイル名や、直したい場所を)教えてください]"
            )
        _persist_pending_fix_subtasks(project_dir, parsed, request, still_pending_subtasks, language)
        return

    _finalize_fix_changes(
        project_dir, request, parsed, language, all_modified_files, combined_error,
        pending_fix_request=request, pending_fix_subtasks=still_pending_subtasks,
    )


def _run_fix_on_project(
    port: int, org_fingerprint: str, project_dir: str, request: str, out_dir: str, enable_dialogue: bool = False,
) -> None:
    """既に対象が確定した`project_dir`に対して、実際の修正を実行する。

    仮の判断:
    - 修正の実行は、実装担当のモデル自身にプロジェクトツール一式
      (`_collect_answer_with_project_tools`)を使わせて行う。Yoriai側で
      「修正が必要なファイル」を先読みして決め打ちする実装は避けた
      (担当外のファイルとの連携まで踏まえた判断は、モデル自身が実際の
      コードを見てから行う方が確実なため、既存のレビューフェーズと
      同じ設計思想を踏襲した)。
    - 修正完了後、プロジェクト内の全ファイルに、拡張子に応じた機械的な
      構文チェックを行う(`_syntax_check_all_files`、言語非依存)。個々の
      `write_file`呼び出し直後にも同じ構文チェックの結果がその場で
      モデルへの返り値として返るため、モデル自身が気づいて直せる機会も
      ある。1台構成でLLMによる相互レビューができない状況でも、この
      機械的なチェックだけは必ず行われる(依頼の「既存の構文チェック...
      フェーズを通してから反映する」という要件への対応)。
    - 依頼の項目4: PROGRESS.mdに記録されている、このプロジェクトの
      使用言語をプロンプトに埋め込み、担当メンバーがどの言語・拡張子で
      作業すべきかを常に意識できるようにする(旧バージョンのPROGRESS.md
      で言語の記録が無い場合は、モジュール分割案のファイル拡張子から
      その場で逆算する。`_resume_project`と同じ「読み込み時に補完する」
      考え方)。
    - 呼び出し元(`_ask_organization_fix_project`・対話的な修正セッション
      `_FixSession`のいずれか)が事前に`_resolve_and_validate_fix_target`で
      対象を検証済みであっても、ここでも同じ検証(PROGRESS.mdが読める
      か・タスクが完了済みか)をやり直す。修正セッションの2回目以降の
      発言はこの検証を経ずに直接呼ばれるため、この関数自身が自己完結
      した安全網であることを保証するため。

    仮の判断(重大なバグ報告への対応): 実機で、「修正が完了しました」と
    表示されPROGRESS.mdにも更新履歴が記録されたにもかかわらず、実際には
    対象ファイルが一切変更されていない不具合が報告された。原因は、
    `_collect_answer_with_project_tools`の問い合わせ自体が成功したか
    どうか(`error`)だけを見ており、モデルが実際にwrite_file等を呼んで
    ファイルを変更したかどうかを一切確認していなかったこと(文章だけの
    "修正しました"という回答や、write_fileが呼ばれたがパストラバーサル
    対策等で実際には失敗したケースの両方で、無条件に完了扱いになって
    いた)。`_collect_answer_with_project_tools`が返す`modified_files`
    (write_file/move_file/delete_fileが実際に成功したファイル名の
    リスト)が空の場合は、問い合わせ自体の成否に関わらず「修正が実行
    されませんでした」と正直に報告し、PROGRESS.mdへの更新履歴の記録も
    行わない。これにより、「完了しました」という報告とPROGRESS.mdの
    更新履歴は、常に実際のファイル変更と対応することが保証される。

    仮の判断(追加のユーザー報告への対応): 上記の対応だけでは「正直に
    失敗を報告する」に留まり、依頼そのものは達成されないままだった。
    実機で、モデルが「〇〇を修正しました」という説明文だけを返し、
    write_file等を一度も呼ばない現象が繰り返し報告されたため、
    `_collect_answer_with_project_tools`側で、最終回答がツール呼び出しを
    伴わずmodified_filesが空のまま終わろうとした場合に、1回だけ「実際に
    ツールを呼んでください」と促して再試行する仕組みを追加した
    (`_NO_TOOL_CALL_NUDGE_MESSAGE`)。これにより、モデルが行動を忘れた
    だけのケースでは実際に修正が成功するようになる。無制限に促し続けると
    暴走につながるため、促しは1回のみで、それでも改善しなければ上記の
    正直な失敗報告に落ち着く。

    仮の判断(修正対象ファイルの特定失敗に関する追加のバグ報告への
    対応): 依頼文にファイル名や具体的な手がかりが乏しい場合
    (「もう少し大きくして」等)、モデルがどのファイルを直せば良いか
    自信を持てないまま何も書き込まずに終わる(=`modified_files`が
    空になる)ケースが実機で確認された。この状況は上記の対応で既に
    「実行されませんでした」という正直な報告にはなっているが、単に
    再試行を促すだけでなく、ユーザーに次の一手(ファイル名や直したい
    箇所を教える)を明確に案内した方が親切なため、メッセージを
    「どのファイルを直せば良いか判断できませんでした」という確認を
    求める文面に変更した。
    """
    print(f"[🔧 対象プロジェクトを特定しました: {project_dir}]")

    parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
    if parsed is None:
        print(f"[⚠️ {project_dir} のPROGRESS.mdを読み取れなかったため、修正を中断します]")
        return
    if _project_has_pending_work(parsed):
        print(
            f"[⚠️ {project_dir} はまだ未完了のタスクが残っています。"
            f"先に{RESUME_ALL_COMMAND}で完了させてから修正を依頼してください]"
        )
        return

    tasks = parsed["tasks"]
    language = parsed["language"] or _infer_language_from_tasks(tasks)
    full_plan = "\n".join(f"{fn}: {content}" for fn, content in tasks)
    file_list = _list_project_files(project_dir)
    if not file_list:
        print(f"[⚠️ {project_dir} にファイルが見つからないため、修正を中断します]")
        return

    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return
    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    implementer = candidates[0]

    # 仮の判断(依頼の「//fix修正フェーズでの利用」への対応): 対話プロト
    # コルによる修正方針の事前合意を、規模判定(_decide_fix_task_split)
    # より前に挟む。合意できた場合はその方針を依頼文に追記し、以降の
    # 規模判定・実装依頼にそのまま引き継がれる。既定は`enable_dialogue=False`
    # (`_begin_fix_session`・`_continue_fix_session`経由の実際のREPL操作
    # からのみ`True`が渡る)。
    if enable_dialogue:
        request, aborted = _run_fix_approach_dialogue(
            org_fingerprint, request, candidates, full_plan, file_list, language, project_dir,
        )
        if aborted:
            return

    # 仮の判断(依頼への対応: //fixにも規模に応じたタスク分割を導入する):
    # 実際に修正を依頼する前に、まず依頼の規模を判定する。分割すべきと
    # 判断された場合は、既存のタスクキュー方式(`_run_fix_task_queue`)に
    # 委ねて複数メンバーで分担し、ここでは戻ってその後の処理を続けない。
    # 分割不要と判断された場合(既定の安全側フォールバックを含む)は、
    # 以下の既存の単一実装の経路をこれまで通り実行する。
    subtasks = _decide_fix_task_split(implementer, org_fingerprint, request, full_plan, file_list, language)
    if subtasks:
        print(f"[📐 大規模な修正のため、{len(subtasks)}件のサブタスクに分割します]")
        for i, subtask in enumerate(subtasks, start=1):
            print(f"  {i}. {subtask}")
        _run_fix_task_queue(subtasks, candidates, org_fingerprint, project_dir, request, parsed, full_plan, file_list, language)
        return

    print(f"[🔧 {implementer['label']} (モデル: {implementer['model']}) に修正を依頼しています...]")
    prompt = _FIX_REQUEST_TOOL_PROMPT_TEMPLATE.format(
        full_plan=full_plan, file_list="\n".join(file_list), request=request, language=language,
        search_then_read_guidance=_SEARCH_THEN_READ_GUIDANCE,
    )
    summary, error, truncated, modified_files = _collect_answer_with_project_tools(
        implementer, org_fingerprint, [{"role": "user", "content": prompt}], project_dir,
    )

    # 仮の判断: 実機で、「修正が完了しました」と表示されPROGRESS.mdにも
    # 更新履歴が記録されたにもかかわらず、実際にはファイルが一切変更
    # されていない(モデルが文章だけを返してwrite_file等を一度も呼ばな
    # かった、または呼んだが失敗した)という不具合が報告された。
    # write_file/move_file/delete_fileが実際に成功したことが確認できた
    # 場合(modified_filesが空でない場合)のみ、"完了"の報告とPROGRESS.md
    # への更新履歴の記録を行う。1件も変更が確認できなければ、問い合わせ
    # 自体は成功していても(=モデルが応答は返したが行動しなかった場合も
    # 含めて)正直に「実行されませんでした」と報告し、PROGRESS.mdは
    # 一切書き換えない(事実と異なる完了報告をしないため)。
    if not modified_files:
        if error:
            print(f"修正の問い合わせに失敗しました: {error}")
        else:
            if summary:
                print(summary)
            print(
                "[❓ どのファイルを直せば良いか判断できませんでした"
                "(修正は実行されませんでした)。"
                "もう少し具体的に(ファイル名や、直したい場所を)教えてください]"
            )
        return

    # ここに到達した時点で、少なくとも1件のファイル変更が実際に確認できている。
    # （エラー・往復回数の上限到達により途中で終わった場合でも、それまでに
    # 成功した変更はmodified_filesに残っているため、報告・記録の対象にする。）
    if summary:
        print(summary)
    if truncated:
        print(f"[⚠️ 応答が長すぎたため、{CHAT_MAX_OUTPUT_TOKENS}トークンで打ち切られました。この後の構文チェックで問題が残っていないか確認してください]")

    _finalize_fix_changes(project_dir, request, parsed, language, modified_files, error)


# ---------------------------------------------------------------------------
# 実行モードの自動判定(コマンド不要化)
# ---------------------------------------------------------------------------
#
# これまでは単発質問・//multi・//parallel・//agree と、人間がコマンドを
# 使い分けて実行モードを指定する設計だった。これを「ただ話しかけるだけ」で
# Yoriai自身が適切なモードを選ぶ方式に変更する。明示コマンドは、自動判定を
# 上書きしたい場合の手動指定として引き続き使える。

EXECUTION_MODE_SINGLE = "single"
EXECUTION_MODE_COMPARE = "compare"
EXECUTION_MODE_COLLABORATE = "collaborate"
EXECUTION_MODE_FIX_PROJECT = "fix_project"

# 仮の判断: 「作って」等、まとまった制作物を要求するキーワードを協業モードの
# 判定に使う。「書いて」は1つの関数・スニペットの依頼でも使われがちなため、
# 誤判定を避けるためあえて含めていない。
_COLLABORATE_MODE_KEYWORDS = ("作って", "作成して", "つくって", "開発して", "構築して")

# 仮の判断: 「複数の答えを見比べたい」という意図が読み取れる表現を比較
# モードの判定に使う。
_COMPARE_MODE_KEYWORDS = (
    "意見を聞", "意見がほし", "意見を教えて", "みんなの意見", "比較して", "比べて",
    "複数の案", "複数の意見", "見比べ", "どう思う", "案を出して", "案がほしい",
)

# 仮の判断: 既存プロジェクトへの修正依頼と読み取れる表現を判定に使う。
# 「作って」と異なりほぼ確実に既存の何かを対象にした表現であるため、
# 誤判定のリスクは低いと判断した。ただし、既存プロジェクトが1件も
# 無い場合にまでこのモードへ倒すと空振りになるため、
# `_classify_execution_mode`側で`out_dir`にプロジェクトが実在する
# 場合にのみこの判定を有効にする。
_FIX_PROJECT_MODE_KEYWORDS = ("直して", "修正して", "直したい", "修正したい", "なおして")


def _has_any_completed_projects(out_dir: str) -> bool:
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    if not os.path.isdir(projects_root):
        return False
    for name in os.listdir(projects_root):
        progress_path = os.path.join(projects_root, name, PROGRESS_FILENAME)
        parsed = _parse_progress_markdown(progress_path)
        if parsed is not None and not _project_has_pending_work(parsed):
            return True
    return False


def _classify_execution_mode(text: str, out_dir: str = None) -> str:
    """依頼文から実行モード(単発/比較/協業/既存プロジェクトの修正)を
    判定する。

    仮の判断: キーワードベースの単純な判定にとどめる。既存プロジェクトへの
    修正依頼キーワード(「直して」等)を最優先で確認するが、これは
    `out_dir`が渡され、かつ完成済みのプロジェクトが実際に1件以上
    存在する場合のみ有効にする(`out_dir`省略時はこの判定を行わない。
    既存の呼び出し元・テストとの後方互換のための既定値)。次に協業系
    キーワード(「作って」等)、その次に比較系キーワード(「意見を聞かせて」等)を
    確認する。どれにも該当しなければ単発モードとする。
    """
    if out_dir is not None:
        for keyword in _FIX_PROJECT_MODE_KEYWORDS:
            if keyword in text and _has_any_completed_projects(out_dir):
                return EXECUTION_MODE_FIX_PROJECT
    for keyword in _COLLABORATE_MODE_KEYWORDS:
        if keyword in text:
            return EXECUTION_MODE_COLLABORATE
    for keyword in _COMPARE_MODE_KEYWORDS:
        if keyword in text:
            return EXECUTION_MODE_COMPARE
    return EXECUTION_MODE_SINGLE


def _last_turn_is_dialogue_pause(messages: list) -> bool:
    """会話履歴(`messages`/`_ChatLog`)の直前のエントリが、対話プロトコルの
    一時停止(セーフティリミット・人間の確認が必要)を報告するものだった
    かどうかを判定する。

    仮の判断(平文からの自動ツール選択の復活、依頼への対応): 以前
    (637cce6)は、対話プロトコルが人間の確認を求めて一時停止した直後の
    続きの発言(感想・追加指示等)まで`_classify_execution_mode`でゼロから
    判定し直され、「[判断: 単発の質問と判断しました]」と表示されて無関係な
    新規のやり取りとして扱われてしまう不具合があり、平文からの自動判定
    そのものを廃止していた。その後、会話履歴が一本化され(`_ChatLog`)、
    バックグラウンドジョブの標準出力も逐次`messages`へ`role: "assistant"`の
    エントリとして記録されるようになったため、一時停止の報告
    (`_DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER`)がその直前のエントリに
    含まれているかどうかを見れば、「一時停止の直後かどうか」を会話状態
    から判別できる。この場合だけ自動判定をスキップして常に会話の続き
    (単発質問)として扱い、それ以外の(新規の話しかけらしい)場面では
    自動判定を有効にする。
    """
    if not messages:
        return False
    last_content = messages[-1].get("content", "")
    return _DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER in last_content


def _execution_mode_reason_label(mode: str) -> str:
    if mode == EXECUTION_MODE_COLLABORATE:
        return "制作依頼と判断し、事前すり合わせを行います"
    if mode == EXECUTION_MODE_COMPARE:
        return "複数の意見を求めていると判断し、複数メンバーに問い合わせます"
    if mode == EXECUTION_MODE_FIX_PROJECT:
        return "既存プロジェクトへの修正依頼と判断し、対象プロジェクトを特定します"
    return "単発の質問と判断しました"


# ---------------------------------------------------------------------------
# 対話的な修正セッション(//fixの継続利用)
# ---------------------------------------------------------------------------
#
# これまでの//fixは常に単発の処理で、1回の依頼ごとに対象プロジェクトを
# 独立して特定し直していた。実際の運用では、「まだ狭い」「今度は逆に」の
# ように、コマンドを付けない自然な発言で少しずつ調整しながら何度も
# //fixを打ち直す使い方が多く発生していたため、一度//fixで対象プロジェクトを
# 指定したら、その後はコマンド無しの発言も継続してそのプロジェクトへの
# 修正依頼として扱う「修正セッション」を導入する。

# 仮の判断: セッションを保持したまま一定時間操作が無かった場合に自動終了
# させるための目安値。厳密な根拠のある値ではなく、対話モードが1回の
# セッションで数十分~数時間開きっぱなしになることを踏まえ、「明らかに
# 別の作業に移った」とみなせる程度の間隔として設定した。
_FIX_SESSION_IDLE_TIMEOUT_SEC = 30 * 60

# 仮の判断: セッション中に「そのプロジェクトに関係ない話題」(このコード
# ベースの簡易な判定では、比較モード=`EXECUTION_MODE_COMPARE`と判定された
# 発言)が何ターン連続したら自動終了とみなすかの目安値。1回程度の脱線では
# 終了せず、複数ターン続けて別の話題に移った場合にのみ終了する。
_FIX_SESSION_UNRELATED_STREAK_LIMIT = 3

# 仮の判断: ユーザーが明示的にセッションを終えたいときの言い回し。短い
# 相槌的な発言(この単語だけの発言)に限定してマッチさせることで、
# 「(修正が)まだ終了していません」のような、たまたま"終了"という語を含む
# 通常の修正依頼を誤って終了コマンドとして扱ってしまう事故を防ぐ。
_FIX_SESSION_END_PHRASES = (
    "終了", "セッション終了", "修正セッション終了", "修正セッションを終了",
    "セッションを終了", "やめる", "おわり", "終わり",
)


class _FixSession:
    """`//fix`で対象プロジェクトを指定した後、コマンド無しの発言を継続的に
    そのプロジェクトへの修正依頼として扱うための状態。`_run_repl_client`が
    1つだけ保持し、セッションが有効な間は対象プロジェクトの再特定を行わない。
    """

    def __init__(self, project_dir: str):
        self.project_dir = project_dir
        self.unrelated_streak = 0
        self.last_active = time.monotonic()

    @property
    def project_name(self) -> str:
        return os.path.basename(self.project_dir.rstrip(os.sep))

    def touch(self) -> None:
        """このセッションへの継続的な操作があったことを記録し、無関係な
        話題の連続カウントをリセットする。
        """
        self.unrelated_streak = 0
        self.last_active = time.monotonic()

    def is_idle_expired(self) -> bool:
        return time.monotonic() - self.last_active >= _FIX_SESSION_IDLE_TIMEOUT_SEC


def _format_fix_session_marker(fix_session: "_FixSession") -> str:
    return f"[🔧 修正セッション中: {fix_session.project_name}]"


def _is_fix_session_end_phrase(text: str) -> bool:
    stripped = text.strip().rstrip("。.!!")
    return stripped in _FIX_SESSION_END_PHRASES


def _continue_fix_session(port: int, org_fingerprint: str, out_dir: str, job_runner: "_BackgroundJobRunner",
                           fix_session: "_FixSession", request_text: str, chat_log: "_ChatLog" = None) -> None:
    """既に有効な`fix_session`に対して、対象の再特定を行わずそのまま
    対象プロジェクトへの継続的な修正依頼を投入する。コマンド無しの発言
    (`_run_repl_client`)と、プロジェクト名を省略した`//fix <依頼>`
    (`_begin_fix_session`)の両方から、同じ「今のセッション対象へ続ける」
    処理として共有される。

    `chat_log`(既定`None`)を渡すと、この依頼と結果が対話モード全体の
    会話履歴にも記録される(`_run_job_with_conversation_log`参照)。
    """
    fix_session.touch()
    print(_format_fix_session_marker(fix_session))
    project_dir = fix_session.project_dir
    job_runner.submit(
        lambda project_dir=project_dir, request=request_text: _run_job_with_conversation_log(
            chat_log, request,
            lambda: _run_fix_on_project(port, org_fingerprint, project_dir, request, out_dir, enable_dialogue=True),
        ),
        queued_notice=_BACKGROUND_QUEUED_NOTICE,
    )


def _begin_fix_session(port: int, org_fingerprint: str, out_dir: str, job_runner: "_BackgroundJobRunner",
                        fix_session, request_text: str, chat_log: "_ChatLog" = None):
    """`//fix`(明示コマンド・コマンド無しの自然文どちらの経由でも)による
    修正依頼を処理する。

    仮の判断(気づきへの対応): セッションが有効な間に、プロジェクト名を
    省略した`//fix <依頼>`(`<プロジェクト名>: `構文を伴わない)が送られた
    場合、コマンド無しの発言と全く同じ扱いにする。つまり依頼文と
    PROGRESS.mdを照合する自動判定(`_identify_target_project`、文字
    バイグラムによる簡易な照合のため曖昧な依頼文では失敗しやすい)へは
    回さず、`_continue_fix_session`で現在のセッション対象へそのまま
    続ける。以前は明示コマンド経由かどうかに関わらず常に対象の解決を
    やり直していたため、セッション中に"//fix "を付けて発言しただけで
    「対象となるプロジェクトが見つかりませんでした」と誤って報告される
    (コマンドを付けない発言なら問題なく継続できるのに、という一貫性の
    無さがあった)。プロジェクト名を明示した`//fix <プロジェクト名>:
    <依頼>`は、別プロジェクトへの明示的な切り替え手段として引き続き
    そのまま解決処理に委ねる。

    それ以外の場合(明示指定あり、またはセッションが無い状態)は、対象
    プロジェクトをその場で(同期的に)解決し、成功すればセッションを
    開始・更新したうえで実際の修正をバックグラウンドジョブとして投入
    する。解決できなかった場合(未検出・複数候補・未完了)は、
    `_resolve_and_validate_fix_target`が理由をprint済みのため、ここでは
    何もせず`fix_session`をそのまま返す(依頼の項目3: 別プロジェクトへの
    //fixが実際に特定できた場合にのみ、前のセッションを終了する)。

    戻り値は、更新後の(または変化が無ければそのままの)`fix_session`。
    """
    explicit_project_dir, _rest = _resolve_explicit_fix_target(request_text, out_dir)
    if explicit_project_dir is None and fix_session is not None:
        _continue_fix_session(port, org_fingerprint, out_dir, job_runner, fix_session, request_text, chat_log=chat_log)
        return fix_session

    project_dir, request = _resolve_and_validate_fix_target(request_text, out_dir)
    if project_dir is None:
        return fix_session

    if fix_session is not None and fix_session.project_dir != project_dir:
        print(
            f"[🔧 修正セッションを終了しました({fix_session.project_name} → "
            f"{os.path.basename(project_dir)}への切り替えのため)]"
        )
        fix_session = None
    if fix_session is None:
        fix_session = _FixSession(project_dir)
    else:
        fix_session.touch()

    print(_format_fix_session_marker(fix_session))
    job_runner.submit(
        lambda project_dir=project_dir, request=request: _run_job_with_conversation_log(
            chat_log, request,
            lambda: _run_fix_on_project(port, org_fingerprint, project_dir, request, out_dir, enable_dialogue=True),
        ),
        queued_notice=_BACKGROUND_QUEUED_NOTICE,
    )
    return fix_session


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def run_agent(token: str, port: int) -> None:
    """常駐(キッチン)実行のエントリーポイント。`python3 yoriai.py` (対話モード関連の
    引数なし)で常に呼ばれる。systemd/launchdからでも、ターミナルからのフォアグラウンド
    実行でも同じ挙動(自己紹介カードサーバー・mDNS/Tailscale探索・/chatの提供)になる。
    対話モードのUI(フロント)は同じプロセスには含まれない
    (別プロセスの`python3 yoriai.py --chat`から`/status`・`/chat`経由で問い合わせる)。
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

    logger.info("同じネットワーク上のYoriaiエージェントを探索しています... (Ctrl+Cで終了)")
    try:
        last_heartbeat = time.monotonic()
        last_tailscale_scan = time.monotonic()
        while True:
            time.sleep(1)
            # 仮の判断: 発見がない間も動作中であることが外から分かるよう、
            # 一定間隔でハートビートログを出す。mDNSが機能しないネットワーク
            # (例: クライアント間マルチキャストが遮断された環境)でも、
            # Tailscale経由の発見数を合わせて見せることで「本当に0台なのか」を
            # 判断しやすくする。
            if time.monotonic() - last_tailscale_scan >= TAILSCALE_RESCAN_INTERVAL_SEC:
                tailscale_found_count = discover_via_tailscale(agent_id, org_fingerprint, port, registry)
                last_tailscale_scan = time.monotonic()
            if time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                logger.info(
                    "探索中... (現在発見数: mDNS %d件 / Tailscale %d件)",
                    len(listener.known_peers), tailscale_found_count,
                )
                last_heartbeat = time.monotonic()
    except KeyboardInterrupt:
        logger.info("終了します...")
    finally:
        zeroconf.unregister_service(service_info)
        zeroconf.close()
        server.shutdown()


# 仮の判断(prompt_toolkit採用への設計変更): 以前は`readline`による
# 1行単位の編集(input()を1行ずつ呼び、空行が来たら連結して送信)を
# 使っていたが、これには「上下矢印で前の行に戻って編集できない」
# (readlineは1行内のカーソル移動にしか対応していない)という原理的な
# 制約があった。実機からも、複数行にまたがる入力の改行部分に制御文字と
# 思われる記号が表示されることがあるという報告があった。
#
# 過去に(フェーズ5.2の判断メモを参照)、外部依存を最小限(zeroconf・
# requestsのみ)に保つ方針から`prompt_toolkit`の導入を一度見送っていたが、
# 今回の要望(複数行入力全体を1つの編集可能な領域として扱い、上下矢印で
# 自由に行き来する)はreadlineでは原理的に実現できない機能のため、改めて
# 導入を検討し、採用することにした。判断の詳細は本ファイル末尾の
# 「複数行入力のテキストエディタ化(prompt_toolkit採用)」の判断メモを
# 参照。ARM環境(Raspberry Pi 4)向けの事前確認(ネイティブ依存の有無)も
# そちらに記録している。
_REPL_PROMPT = "Yoriai> "

# 仮の判断: 実機(Windows機からraspi4へのSSH接続)で、送信キー(Alt+Enter)が
# SSHクライアント側のショートカットに奪われ、Ctrl+Enterも(SSH経由では
# ネイティブコンソールのControl-Enter→Meta-Enter自動変換が働かないため)
# 機能せず、exit/quit・Ctrl+D・Ctrl+Cのいずれも反応せず対話モードから
# 一切抜け出せなくなる不具合が報告された。この「非常口」機能は、送信キーが
# 何らかの理由で一切機能しない状況でも確実に対話モードを終了できることを
# 唯一の目的とするため、新しい送信キーの選定は行わず、Ctrl+Cという既存の
# 割り込みキー(SSH経由でも確実に届く、TCP接続を通じて転送される標準的な
# シグナル)を2回連続で押すことを検出する方式にした。
_REPL_DOUBLE_CTRL_C_WINDOW_SEC = 2.0


class _DoubleInterruptGuard:
    """対話モードのどのタイミング(入力編集中・応答待ちのどちらでも)で
    Ctrl+C(`KeyboardInterrupt`)が発生しても、直前のCtrl+Cから
    `_REPL_DOUBLE_CTRL_C_WINDOW_SEC`秒以内に連続して発生したかどうかを
    判定できるようにする、対話モードのセッションを通じて1つだけ使い回す
    小さな状態オブジェクト。

    仮の判断: `_read_multiline_input`(入力編集中のCtrl+C)と
    `_run_repl_client`本体(組織への問い合わせ中、応答待ちの間のCtrl+C)の
    両方でCtrl+Cを検出する必要があるため、片方の関数内のローカル変数では
    状態を共有できない。`_run_repl_client`が対話モードの開始時に1つだけ
    作り、両方の箇所に渡して使い回すことで、「入力中に1回目、応答待ち中に
    2回目」のように状況をまたいで連続したCtrl+Cも正しく検出できる。
    """

    def __init__(self, window_sec: float = _REPL_DOUBLE_CTRL_C_WINDOW_SEC):
        self._window_sec = window_sec
        self._last_interrupt_at = None

    def note_interrupt(self) -> bool:
        """Ctrl+Cが発生するたびに呼ぶ。直前のCtrl+Cから`window_sec`秒以内
        なら(=2回連続とみなし)`True`を返す。それ以外は`False`を返し、
        「今回のCtrl+C」を新たな基準時刻として記録する。
        """
        now = time.monotonic()
        if self._last_interrupt_at is not None and (now - self._last_interrupt_at) <= self._window_sec:
            return True
        self._last_interrupt_at = now
        return False


def _handle_repl_command_interrupt(interrupt_guard: "_DoubleInterruptGuard") -> bool:
    """組織への問い合わせ中(応答待ち)にCtrl+Cが発生した際の共通処理。
    2回連続のCtrl+Cであれば、対話モードを終了すべきことを示す`True`を返す。
    """
    print()
    if interrupt_guard.note_interrupt():
        print("[⚠️ Ctrl+Cが2回連続で押されたため、対話モードを終了します]")
        return True
    print("(処理を中断しました。2秒以内にもう一度Ctrl+Cを押すと、対話モードを終了できます)")
    return False


def _repl_prompt_continuation(width, line_number, is_soft_wrap) -> str:
    """prompt_toolkitの`prompt_continuation`コールバック。

    仮の判断: 2行目以降にも`...`のようなプレフィックスを付けていた時期が
    あったが、これは以前(readlineベースの実装時代)、まだ入力途中である
    ことを示すための工夫だった。`prompt_toolkit`のmultiline編集に切り替え
    た後は、複数行の入力欄自体が視覚的に自明であり(Claude Code等の
    一般的な複数行入力欄と同様)、かえって見た目のノイズになるという
    フィードバックを受けて空文字列(プレフィックス無し)にした。1行目の
    プロンプト(`_REPL_PROMPT`)はそのまま維持する。
    """
    return ""


# 仮の判断(送信キーをEnter、改行をShift+Enterに変更): 実機で、Alt+Enter
# (旧送信キー)がSSHクライアント側のショートカットに奪われる不具合が
# 報告されたことを受け、Claude Code等と同様の配置(Enterで送信、
# Shift+Enterで改行)に変更した。
#
# 技術的な制約: prompt_toolkit(3.0.53時点)には「Shift+Enter」を表す
# 固有のキー名が存在しない。多くの端末がShift+Enterに対して送るxterm
# modifyOtherKeys形式のエスケープシーケンス(`\x1b[27;2;13~`)は、
# prompt_toolkitの入力パーサー(`prompt_toolkit.input.ansi_escape_sequences.
# ANSI_SEQUENCES`)自体が、キー割り当ての仕組みに渡す前の時点で素の
# Enterと同じ`Keys.ControlM`に変換してしまうため、`@bindings.add("s-enter")`
# のような割り当ては存在しない(実際に試すと`ValueError: Invalid key:
# s-enter`になる)。
#
# そこで、Enterキーの共通ハンドラ内で`KeyPress.data`(変換前の生の
# エスケープシーケンス文字列。`.key`とは別に保持されている)を調べ、
# Shift+Enterの生シーケンスと一致する場合だけ改行を挿入し、それ以外は
# 送信として扱う、という方法で区別する。この生シーケンスをそのまま送って
# くる端末(および、この形式のシーケンス送出が有効化された設定)では
# Shift+Enterが改行として機能するが、これはterminfoやSSHクライアントの
# 実装に依存するため、全ての環境で保証されるわけではない。依頼で明示的に
# 許容された通り、区別できない端末ではShift+Enterも単にEnterと同様に
# 送信として扱われる(改行を挿入したい場合に単に送信されてしまう)。
# それでも、送信キー自体が完全に効かなくなるわけではなく、Ctrl+Cを2回
# 連続で押す「非常口」(`_DoubleInterruptGuard`)は影響を受けず引き続き
# 確実に機能する。
_REPL_SHIFT_ENTER_RAW_DATA = "\x1b[27;2;13~"


def _make_repl_key_bindings() -> KeyBindings:
    """対話モードの入力セッション用のキー割り当てを作る。

    仮の判断: `multiline=True`セッションの既定のキー割り当てでは、Enterは
    常に改行を挿入する(送信にはAlt+Enterが必要)。ここでは独自の"enter"
    ハンドラを追加し、既定の挙動を上書きする(`PromptSession(key_bindings=..)`
    で渡したキー割り当ては、既定のキー割り当てより後にマージされ、
    同じキーに複数の割り当てが一致する場合は最後に一致したものが優先される
    ため、既定の「Enterで改行」を上書きできる)。
    """
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit_or_insert_newline(event) -> None:
        raw_data = event.key_sequence[-1].data if event.key_sequence else ""
        if raw_data == _REPL_SHIFT_ENTER_RAW_DATA:
            event.current_buffer.insert_text("\n")
        else:
            event.current_buffer.validate_and_handle()

    return bindings


def _create_repl_prompt_session() -> PromptSession:
    """対話モードのメッセージ入力用に、複数行編集対応のセッションを1つ
    作る。`_run_repl_client`が起動時に1回だけ作り、以後の全メッセージ
    入力(`_read_multiline_input`の呼び出しごと)で使い回す。

    仮の判断: セッションを使い回すことで、入力履歴(`InMemoryHistory`)が
    メッセージをまたいで蓄積される。バッファの先頭行で上矢印を押すと、
    (今回新たに書いていない行であれば)過去に送信したメッセージを呼び戻せる
    (`Buffer.auto_up`の標準動作)。会話をまたいだ履歴の永続化(次回起動時に
    前回の入力を引き継ぐ機能)は今回のスコープ外で、プロセス終了とともに
    履歴も破棄される。
    """
    return PromptSession(history=InMemoryHistory(), key_bindings=_make_repl_key_bindings())


def _read_multiline_input(session: PromptSession, interrupt_guard: "_DoubleInterruptGuard") -> tuple:
    """対話モードの1メッセージ分の入力を読み取る。

    仮の判断: `prompt_toolkit`の`multiline=True`セッションを使う。
    `_make_repl_key_bindings`で追加した独自のキー割り当てにより、
    Enter単体で入力を確定し(送信)、Shift+Enterで改行を挿入する
    (区別できない端末での既知の制約は`_make_repl_key_bindings`の
    コメントを参照)。上下矢印キーは、複数行のバッファ内でまだ移動できる
    行がある間はカーソルをその行へ移動し、バッファの端(最初/最後の行)に
    達すると入力履歴の呼び出しに切り替わる(`Buffer.auto_up`/`auto_down`の
    標準動作)。これにより、依頼の「上下矢印で自由に行き来しながら編集
    できる」という要望に、prompt_toolkit標準の挙動だけで対応できる。

    仮の判断(送信キーの変遷): 「空行で送信」→「Alt+Enterで送信」→
    「Enterで送信、Shift+Enterで改行」の順に変更してきた。複数行バッファ
    全体を自由に編集できる以上「空行=送信」は成立しない(空行=段落区切り
    を入れられなくなる)ため早期に廃止した。続くAlt+Enterは、実機
    (Windows機からraspi4へのSSH接続)でSSHクライアント側のショートカット
    に奪われ、対話モードから抜け出せなくなる不具合が報告された。この
    対策として、Claude Code等と同様の配置(Enterで送信、Shift+Enterで
    改行)に変更した。

    戻り値は`(text, terminate)`のタプル。`terminate`が`True`の場合、
    対話モード自体を終了すべきことを示す(EOF/Ctrl+D、送信内容が
    "exit"/"quit"単体、またはCtrl+Cが2回連続で押された場合)。
    `terminate`が`False`の場合、`text`が確定したメッセージ本文
    (空にはならない)。

    仮の判断: Ctrl+Cを1回だけ押した場合は、バッファに何か入力済みかどうかに
    関わらず、常にそのメッセージ全体を破棄して新しい入力待ちに戻る
    (対話モード自体は終了しない)。以前は「何も入力していない状態での
    Ctrl+Cは対話モード自体を終了する」という、入力済みかどうかで挙動が
    変わる仕様だったが、prompt_toolkitの`session.prompt()`はCtrl+Cが
    押された時点のバッファの状態を呼び出し元に伝えずに一様に
    `KeyboardInterrupt`を送出するため、この区別を維持しようとすると
    別途バッファの状態を監視する仕組みが必要になり、複雑さに見合わない
    と判断した。

    仮の判断(実機で報告された「非常口」不具合への対応): 送信キーが
    SSH経由の接続などで一切機能しない状況では、"exit"/"quit"すら送信
    できずEOF(Ctrl+D)も反応しないことがあり、
    対話モードから抜け出す手段が無くなる不具合が実機で報告された。この
    ため、`interrupt_guard`(`_DoubleInterruptGuard`)で直前のCtrl+Cから
    `_REPL_DOUBLE_CTRL_C_WINDOW_SEC`秒以内に連続してCtrl+Cが押された
    ことを検出した場合は、送信キーの状態に一切関係なく対話モード自体を
    終了する(`terminate=True`を返す)。Ctrl+C(SIGINT)はSSH接続を含む
    どのような端末経由でも標準的に転送される割り込みであり、送信キーが
    ターミナルアプリ側に奪われるような状況でも確実に届くため、「非常口」
    として機能する。
    """
    while True:
        try:
            raw = session.prompt(_REPL_PROMPT, multiline=True, prompt_continuation=_repl_prompt_continuation)
        except EOFError:
            print()
            return "", True
        except KeyboardInterrupt:
            print()
            if interrupt_guard.note_interrupt():
                print("[⚠️ Ctrl+Cが2回連続で押されたため、対話モードを終了します]")
                return "", True
            print("(入力を破棄しました。2秒以内にもう一度Ctrl+Cを押すと、対話モードを終了できます)")
            continue

        text = raw.strip()
        if not text:
            # 何も入力していない状態(または空白のみ)での確定は、
            # 何もせず次の入力を待つ(従来通りの挙動)。
            continue
        if text.lower() in ("exit", "quit"):
            return "", True
        return text, False


# 仮の判断(バックグラウンド実行): 以前は`//agree`・自動判定された協業
# モード・`//parallel`・`//resume-all`のいずれも、対話モードのメイン
# スレッドで同期的に(=完了するまで次の入力を受け付けずに)実行して
# いた。協業モードは合意フェーズ→タスクキュー方式による実装・レビューと
# 複数のフェーズにまたがり、実機では数分単位の時間がかかることがある。
# その間、次の依頼を入力できないのはClaude Code等のツールの体験と比べて
# 不便という指摘を受け、これらの時間のかかる処理を専用のバックグラウンド
# スレッドに投げ、メインスレッドの入力ループを一切ブロックしないように
# 変更した。
class _BackgroundJobRunner:
    """対話モードの時間のかかる処理(協業モード・`//parallel`・
    `//resume-all`)を、入力ループをブロックしない1本のバックグラウンド
    スレッドで順番に(FIFOで)実行する。

    仮の判断: 複数のジョブを真に並列実行するのではなく、あくまで
    「メインスレッド(入力受付)をブロックしない」ことだけを目的とした
    設計にした。複数の協業モードのジョブを同時に走らせても、同じ組織の
    メンバー(LLMバックエンド)を取り合うだけで実質的な高速化にはならない
    上、タスクキューやチェックリストの状態を複数のジョブが同時に触る
    ことによる競合のリスクが増える。ジョブを1本のキューで順番に処理する
    方式なら、この手の競合を作り込む余地が無い。

    仮の判断: 単発質問(`_ask_organization`)・比較質問(`//multi`、
    `_ask_organization_multi`)はこのジョブキューに乗せず、これまで通り
    メインスレッドで同期的に実行する。これらは対話モードの会話履歴
    (`messages`)を共有・追記する仕組みになっており、もし非同期化すると
    「1件目の処理が終わる前に2件目のユーザー発言が`messages`に追記され、
    1件目の応答がその2件目宛の質問であるかのように扱われてしまう」
    という会話の順序が壊れる不具合を生みかねない。これらのモードは
    協業モードほど長時間かからないことが多く、依頼が主な動機として
    挙げていたのも協業モードの長時間ブロックだったため、非同期化の
    対象を長時間かかる・会話履歴を共有しないモードに絞った。
    """

    def __init__(self):
        self._queue = queue.Queue()
        self._pending_lock = threading.Lock()
        self._pending = 0
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def submit(self, job: callable, queued_notice: str = None) -> None:
        with self._pending_lock:
            already_busy = self._pending > 0
            self._pending += 1
        if already_busy and queued_notice:
            print(queued_notice)
        self._queue.put(job)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception:
                logger.exception("バックグラウンドジョブの実行中にエラーが発生しました")
            finally:
                with self._pending_lock:
                    self._pending -= 1
                self._queue.task_done()

    def join(self) -> None:
        """キューに投入した全ジョブの完了を待つ(主にテスト用)。"""
        self._queue.join()


def _create_background_job_runner() -> "_BackgroundJobRunner":
    return _BackgroundJobRunner()


_BACKGROUND_QUEUED_NOTICE = "[⏳ 実行中の処理があるため、この依頼は完了後に順番にバックグラウンドで処理されます]"


# ---------------------------------------------------------------------------
# 対話モードの会話履歴の一本化と、起動時刻ベースのログファイルへの保存
# ---------------------------------------------------------------------------
#
# 以前は、単発質問・//multi(比較質問)だけが会話履歴(messages)を
# 共有・追記しており、協業モード(//agree)・//fix・//plan-only・
# //parallel・//resume-allはmessagesに一切触れず、それぞれ独立した
# 1回きりのやり取りとして処理されていた。そのため、対話プロトコルが
# 「人間の確認が必要です」と表示していったん立ち止まった直後に、
# ユーザーが感想や追加の指示を送っても、それは全く無関係な新規の単発
# 質問として扱われてしまい、直前まで何を話していたかをメンバー側が
# 一切踏まえられないという不具合が実機で報告された(依頼: 過去の
# 「修正セッション」のような限定的な継続機能ではなく、`--chat`の1回の
# 起動中は常に会話の記録を保持して引き継いでほしい)。
#
# `--chat`の1回の起動中は、どのコマンド経由のやり取りもすべて同じ会話
# 履歴に積み上げ、単発質問がこの履歴を会話コンテキストとして踏まえられる
# ようにする。あわせて、起動時刻をファイル名に持つログファイルへも
# 逐次書き出す(依頼: 別のセッションで見返せるように記録を残したい)。

_CHAT_LOG_SUBDIR_NAME = "chat_logs"
# 仮の判断: 会話履歴に積み上げる協業モード等の出力(対話プロトコルの
# 逐語ログを含みうる)は長大になりうる。無制限に積み上げるとトークン
# 使用量が際限なく増えてしまうため、既存のread_file等と同じ考え方
# (_FILE_CONTENT_TRUNCATE_CHARS)を踏襲し、会話履歴(=LLMへの問い合わせに
# 使われる部分)に載せる分だけ上限を設ける。ログファイル自体には
# 切り詰めずそのまま書き出す(あくまで「LLMに毎回送り直す会話履歴」の
# トークン量対策であり、人間が見返す記録を欠落させたくないため)。
_BACKGROUND_JOB_HISTORY_TRUNCATE_CHARS = 8000


class _ChatLog(list):
    """`--chat`の1回の起動中の会話履歴。

    仮の判断: 既存の`messages`(単なる`list`)と完全互換の`list`の
    サブクラスとして実装し、`append()`だけをフックしてログファイルへの
    書き出しを行う設計にした。これにより、既存の`_ask_organization`/
    `_ask_organization_multi`が行う素朴な`messages.append(...)`をはじめ、
    `messages`を単なる`list`として読み書きする既存コード・既存テストを
    一切変更せずに済む。
    """

    def __init__(self, log_path: str):
        super().__init__()
        self._log_path = log_path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(self._log_path, "w", encoding="utf-8") as f:
            f.write(f"# Yoriai 対話ログ ({time.strftime('%Y-%m-%d %H:%M:%S')} 開始)\n\n")

    def append(self, entry: dict) -> None:
        with self._lock:
            super().append(entry)
            self._append_to_file(entry)

    def _append_to_file(self, entry: dict) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        role = entry.get("role", "?")
        content = entry.get("content", "")
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(f"## [{timestamp}] {role}\n\n{content}\n\n")


def _create_chat_log(out_dir: str) -> "_ChatLog":
    """起動時刻(`YYYYMMDD_HHMMSS`)をファイル名に持つ会話ログを作る
    (依頼: 別のセッションで見返せるように、起動時刻をファイル名に
    入れて記録を残す)。
    """
    log_dir = os.path.join(out_dir, _CHAT_LOG_SUBDIR_NAME)
    log_path = os.path.join(log_dir, f"chat_{time.strftime('%Y%m%d_%H%M%S')}.md")
    return _ChatLog(log_path)


class _StdoutTee:
    """標準出力への書き込みを、実際の出力(端末やテストの
    `redirect_stdout`等、その時点の`sys.stdout`が何であっても)はそのまま
    行いつつ、文字列としても捕捉するためのラッパー。バックグラウンド
    ジョブの出力を会話履歴に取り込むために使う。
    """

    def __init__(self, original):
        self._original = original
        self.captured = []

    def write(self, text: str):
        self.captured.append(text)
        return self._original.write(text)

    def flush(self) -> None:
        self._original.flush()

    def __getattr__(self, name):
        return getattr(self._original, name)


def _truncate_for_history(text: str, limit: int = _BACKGROUND_JOB_HISTORY_TRUNCATE_CHARS) -> str:
    """会話履歴に積む前に、長すぎる出力を`limit`文字までに切り詰める。

    仮の判断(不具合修正: `_last_turn_is_dialogue_pause`が長い対話の後の
    一時停止を検出できない問題への対応): 単純に先頭`limit`文字だけを
    残すと、対話プロトコルの一時停止を示すマーカー
    (`_DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER`)は`_report_dialogue_result`
    により出力の末尾付近に印字されるため、議事録が長い(=このマーカーが
    `limit`文字より後ろに来る)場合に丸ごと切り捨てられてしまい、次の
    ターンで「一時停止直後の発言かどうか」を会話履歴から判別できなく
    なる。マーカーが含まれる場合は、先頭部分を短縮してでもマーカー以降を
    必ず残す。
    """
    if len(text) <= limit:
        return text
    marker_pos = text.find(_DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)
    if marker_pos != -1:
        tail = text[marker_pos:]
        head_limit = max(limit - len(tail), 0)
        return text[:head_limit] + "\n... (中略。会話履歴への記録の一部を省略しましたが、対話プロトコルの一時停止に関するメッセージは末尾に保持しています)\n" + tail
    return text[:limit] + f"\n... (以下省略。会話履歴への記録は先頭{limit}文字まで。詳細は実際の出力・議事録ファイルを参照してください)"


def _run_job_with_conversation_log(chat_log: "_ChatLog", user_label: str, job: callable) -> None:
    """`job`を実行しつつ、その依頼文と(標準出力から捕捉した)結果を
    会話履歴(`chat_log`)に積み上げる。`chat_log`が`None`の場合は、
    これまで通り会話履歴への記録を一切行わずに`job`を実行するだけ
    (既存の呼び出し元・テストとの後方互換性のため)。

    仮の判断: 標準出力を丸ごと差し替えるのではなく、実際の書き込みは
    そのまま転送しつつ文字列としても集める`_StdoutTee`を使うため、
    `patch_stdout()`(バックグラウンドジョブの出力を安全に差し込む
    既存の仕組み)やテストの`contextlib.redirect_stdout`など、呼び出し
    時点の`sys.stdout`が何であっても壊さずに済む。

    仮の判断(実機で顕在化しうる不具合への対応): `job`はバックグラウンド
    ジョブ(`_BackgroundJobRunner`の別スレッド)として実行されることが
    あり、その実行時間は呼び出し元のコンテキスト(`_run_repl_client`の
    `patch_stdout()`・テストの`contextlib.redirect_stdout`等)より長く
    続くことがある(`exit`で対話モードを終了した後もジョブは動き続ける
    設計)。そのため、`job`が終わった時点で無条件に`sys.stdout`を
    最初に捕まえた値へ戻すと、その間に外側のコンテキストが既に
    (別の値へ)復元し終えていた場合、その新しい正しい値を誤って
    自分が最初に見た古い値で上書きしてしまう(`sys.stdout`が壊れた
    状態のままグローバルに残り続ける)。`sys.stdout`が依然として
    自分の設置した`tee`のままである場合に限って復元することで、
    この上書き事故を防ぐ。
    """
    if chat_log is None:
        job()
        return

    chat_log.append({"role": "user", "content": user_label})
    original_stdout = sys.stdout
    tee = _StdoutTee(original_stdout)
    sys.stdout = tee
    try:
        job()
    finally:
        if sys.stdout is tee:
            sys.stdout = original_stdout

    captured = "".join(tee.captured).strip()
    chat_log.append({
        "role": "assistant",
        "content": _truncate_for_history(captured) if captured else "(出力はありませんでした)",
    })


# ---------------------------------------------------------------------------
# 起動時の案内文(見た目の改善)
# ---------------------------------------------------------------------------
#
# 対話モードの起動時の見た目を、Claude Code等のツールに近い洗練された
# 印象にしたいという依頼を受けた。(1)複数行にわたるASCIIアートのロゴ、
# (2)情報量の多かった説明文を階層立てて簡潔に整理、(3)検出済みの
# メンバー数の表示、(4)色付け(ただし色に対応しない環境でも崩れない)、
# を実装する。
#
# 仮の判断(実機で報告された文字化け不具合への対応): 当初、この案内文は
# `patch_stdout()`(バックグラウンドジョブの出力を安全に差し込むための
# 仕組み)のコンテキスト内でprint()していたが、実機(Raspberry Pi 4への
# SSH接続)で色付けのANSIエスケープシーケンスが`?[1m?[36m`のような
# 文字列としてそのまま表示される不具合が報告された。原因を
# `prompt_toolkit`のソースコードを読んで調査したところ、`patch_stdout()`
# は既定(`raw=False`)では`Output.write()`という、意図せず端末制御
# シーケンスを注入しないための安全な書き込み経路を使い、これは
# `Vt100_Output`(実際の対話的端末に接続されている場合に選ばれる
# 実装)ではESC文字(`\x1b`)を`?`に置換してしまうことが分かった
# (該当ソースのコメント: "Removes vt100 escape codes. -- used for
# safely writing text.")。この安全策自体は、LLMの応答テキストのような
# 信頼できない出力に対しては望ましい(応答に紛れ込んだ端末制御
# シーケンスで表示が乗っ取られることを防ぐ)ため、`patch_stdout()`
# 全体を`raw=True`にする対応は避けた。代わりに、案内文(Yoriai自身が
# 用意した、信頼できる固定テキスト)は`patch_stdout()`のコンテキストに
# 入る前に(素の`sys.stdout`へ直接)出力するようにし、LLMの応答等は
# 引き続き安全策の効いた経路のまま維持した。

_ANSI_RESET = "\x1b[0m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_DIM = "\x1b[2m"
_ANSI_CYAN = "\x1b[36m"


def _supports_ansi_color() -> bool:
    """色付け(ANSIエスケープシーケンス)を使ってよいかどうかを判定する。

    仮の判断: 以下のいずれかに該当する場合は色付けを無効にする。
    (1)`NO_COLOR`環境変数(色付けを無効化する事実上の標準、
    https://no-color.org/ )が設定されている。(2)`TERM`が未設定・空、
    または`dumb`(色を含む高度な端末制御シーケンスに対応しない、
    もしくは対応状況が不明な端末)。(3)標準出力が端末に直接つながって
    いない(ファイルやパイプにリダイレクトされている、`isatty()`が偽)。
    これにより、色に対応しない・対応するかどうか不明な環境でも、
    エスケープシーケンスの断片がそのまま表示されて崩れることがない
    (依頼の「誤検知をできるだけ避ける」「色が使えない環境でも崩れずに
    表示できるようにしてほしい」という要件への対応)。`TERM`未設定を
    追加で無効化対象にしたのは、対応状況が判定できない場合は安全側
    (色を使わない)に倒すべきという判断による。
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") in ("", "dumb"):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _ansi(text: str, *codes: str, use_color: bool) -> str:
    if not use_color or not codes:
        return text
    return "".join(codes) + text + _ANSI_RESET


def _display_width(text: str) -> int:
    """半角=1・全角(日本語の仮名・漢字・絵文字等)=2として、おおよその
    表示幅を計算する簡易的な基準(Unicode East Asian Widthの正式な
    判定ではないが、ロゴの箱組みや案内文の80文字幅チェックには十分な
    精度)。

    仮の判断: 単純に「コードポイントがU+00FFより大きいかどうか」で
    判定すると、罫線素片(╔╗╚╝═║等、U+2500-U+257F)まで全角(幅2)扱いに
    なってしまい、実際のターミナル描画幅(半角1文字分)より過大に見積もる
    (実機の80文字幅チェックで実際に検出した)。罫線素片は明示的に幅1
    として扱い、それ以外の日本語の仮名・漢字・絵文字等の代表的な範囲を
    幅2として扱う。
    """
    width = 0
    for ch in text:
        code = ord(ch)
        if 0x2500 <= code <= 0x257F:
            width += 1  # 罫線素片(╔╗╚╝═║等)
        elif (
            0x1100 <= code <= 0x115F      # ハングル字母
            or 0x2E80 <= code <= 0xA4CF   # CJK部首・仮名・統合漢字等
            or 0xAC00 <= code <= 0xD7A3   # ハングル音節
            or 0xF900 <= code <= 0xFAFF   # CJK互換漢字
            or 0xFF00 <= code <= 0xFF60   # 全角英数・全角記号
            or 0xFFE0 <= code <= 0xFFE6   # 全角記号
            or 0x1F000 <= code <= 0x1FFFF  # 絵文字を含む記号面
            or 0x2600 <= code <= 0x27BF   # その他の記号・絵文字的記号(★⚠️✅等)
            or 0x2B00 <= code <= 0x2BFF   # その他の記号・矢印
        ):
            width += 2
        else:
            width += 1
    return width


# 仮の判断: figlet風のブロック体ワードマーク。ユーザー自身が指定した
# 具体的な図案をそのまま採用した(1文字ずれるだけで罫線の噛み合わせが
# 崩れるため、値は一切加工せずリテラルとして固定する)。
_YORIAI_LOGO_ART_LINES = (
    "██╗   ██╗ ██████╗ ██████╗ ██╗ █████╗ ██╗",
    "╚██╗ ██╔╝██╔═══██╗██╔══██╗██║██╔══██╗██║",
    " ╚████╔╝ ██║   ██║██████╔╝██║███████║██║",
    "  ╚██╔╝  ██║   ██║██╔══██╗██║██╔══██║██║",
    "   ██║   ╚██████╔╝██║  ██║██║██║  ██║██║",
    "   ╚═╝    ╚═════╝ ╚═╝  ╚═╝╚═╝╚═╝  ╚═╝╚═╝",
)
_YORIAI_LOGO_RULE = "─" * 46
_YORIAI_LOGO_SUBTITLE = "             G A T H E R  ·  C R E A T E"


def _format_logo_lines(use_color: bool) -> list:
    """複数行にわたるASCIIアートのロゴ(figlet風のブロック体ワードマーク+
    区切り線+サブタイトル)を組み立てる。ブロック体部分をシアン、
    区切り線を控えめな色、サブタイトルを太字にする。
    """
    lines = [_ansi(line, _ANSI_BOLD, _ANSI_CYAN, use_color=use_color) for line in _YORIAI_LOGO_ART_LINES]
    lines.append(_ansi(_YORIAI_LOGO_RULE, _ANSI_DIM, use_color=use_color))
    lines.append(_ansi(_YORIAI_LOGO_SUBTITLE, _ANSI_BOLD, use_color=use_color))
    return lines


def _count_org_members(port: int, org_fingerprint: str):
    """起動時の案内文に表示するためだけの、ベストエフォートなメンバー数
    (自分を含む)取得。`handle_chat`が起動時に`fail_fast=True`で疎通
    確認済みのため、通常はここでも成功する。万が一失敗しても
    `_fetch_org_snapshot`が案内を表示するだけで例外は投げないため、
    対話モードの起動そのものは妨げない(`None`を返し、案内文側で
    「取得できませんでした」という表示に切り替える)。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return None
    return len(data.get("peers", [])) + 1


def _format_member_count_line(member_count) -> str:
    if member_count is None:
        return "(組織のメンバー数を取得できませんでした)"
    return f"🟢 {member_count}台のメンバーが接続中(自分を含む)"


def _format_startup_banner(out_dir: str, member_count, use_color: bool) -> str:
    """対話モード起動時に表示する案内文を組み立てる。

    仮の判断: ロゴ(`_format_logo_lines`)の下に、メンバー数・「基本操作」
    「コマンド」の2つの見出しをまとめて表示する。以前は1行1トピックの
    長い説明文が並んでいたが、見出しの下にまとめることで階層立てて
    簡潔に整理した。
    """
    lines = list(_format_logo_lines(use_color))
    lines.append(_format_member_count_line(member_count))
    lines.append("")
    lines += [
        _ansi("基本操作", _ANSI_BOLD, use_color=use_color),
        "  Enterで送信、Shift+Enterで改行",
        "  上下矢印キーで、入力済みの行を自由に編集できます",
        "  exit・quit の送信、または入力前のCtrl+Dで終了",
        "  " + _ansi("★", _ANSI_BOLD, use_color=use_color)
        + " Ctrl+Cを2秒以内に2回連続で押すと、状況に関わらずいつでも確実に終了できます",
        "    (送信キーやexit/quitが効かない場合の非常口です)",
        "",
        _ansi("コマンド", _ANSI_BOLD, use_color=use_color),
        "  (コマンドを付けずに話しかけると、単発/比較/協業のどのモードかを自動判断します)",
        "  (//agreeの一時停止直後の発言は合意フェーズの続きとして扱われ、合意でき",
        "   次第そのまま実装に進みます。それ以外の一時停止は会話の続きとして扱われます)",
        f"  {MULTI_QUERY_COMMAND} <質問文>: 空きリソース上位{MULTI_QUERY_TARGET_COUNT}台に同時に質問",
        f"  {AGREE_COMMAND} <制作依頼>: 対話プロトコルによる事前すり合わせを経て協業モードで実装",
        f"  {PLAN_ONLY_COMMAND} <依頼>: 対話プロトコルで計画のみ議論し、実装せず計画書を出力",
        f"  {FIX_PROJECT_COMMAND} <修正依頼>: 完成済みプロジェクトの一部を修正(プロジェクト名を",
        f"    先頭に付けて{FIX_PROJECT_COMMAND} <プロジェクト名>: <修正依頼>のように明示指定も可能)",
        f"    ({FIX_PROJECT_COMMAND}後は修正セッションが始まり、続けてコマンド無しで話しかけても",
        "     同じプロジェクトへの修正として扱われます。「終了」で終わります)",
        f"  {PARALLEL_QUERY_COMMAND} <ファイル名1>:<依頼1> | ...: ファイル・依頼を手動指定して並行実装",
        f"  {RESUME_ALL_COMMAND}: 未完了プロジェクトを検出して再開",
        "",
        f"生成物の保存先: {os.path.join(out_dir, PROJECTS_SUBDIR_NAME)}/<プロジェクト名>/",
        "協業モード・//plan-only・//fix・//parallel・//resume-allはバックグラウンドで",
        "処理され、処理中も次の依頼を入力できます。",
    ]
    return "\n".join(lines)


def _run_repl_client(port: int, org_fingerprint: str, out_dir: str) -> None:
    # 仮の判断(実機で報告された文字化け不具合への対応): 起動時の案内文
    # (ロゴ等)は、`patch_stdout()`のコンテキストに入る前に、素の
    # `sys.stdout`へ直接print()する。理由は`_format_logo_lines`の
    # コメントを参照。`patch_stdout()`(バックグラウンドジョブの出力を
    # 安全に差し込むための仕組み)は、この後のセッション本体
    # (入力ループ・LLMの応答・バックグラウンドジョブの出力)にのみ適用する。
    print()
    member_count = _count_org_members(port, org_fingerprint)
    print(_format_startup_banner(out_dir, member_count, _supports_ansi_color()))
    print()

    # 仮の判断: 対話モードのセッション本体を`patch_stdout()`で包む。
    # バックグラウンドジョブ(協業モード等)がメインスレッドの入力待ち中に
    # print()する際、このコンテキストが無いと出力が入力中の行を破壊して
    # しまう(prompt_toolkit公式が「別スレッドから安全にprintする」
    # ために提供する仕組み)。
    #
    # 仮の判断: `patch_stdout()`は、内部で`prompt_toolkit`の(プロセス
    # 全体で共有される)既定の`AppSession`が持つ`Output`オブジェクトへ
    # 書き込む。既定の`AppSession`はプロセス内で使い回される単一の
    # インスタンスであり、一度作られると(その時点の`sys.stdout`を
    # 元に)出力先を固定してしまうため、`create_app_session()`で
    # この呼び出し専用の新しい`AppSession`を作ってから`patch_stdout()`を
    # 使う。これが無いと、テストのように同一プロセス内で`_run_repl_client`
    # (や`sys.stdout`の差し替え)を複数回呼び出した場合、2回目以降の
    # 出力が最初の呼び出し時点の(既に閉じられた)出力先に書き込まれて
    # 消えてしまう不具合が実際に発生した。
    with create_app_session(), patch_stdout():
        # 仮の判断(依頼への対応): 会話履歴(messages)は、この起動中は
        # 単発質問・//multiだけでなく、//agree・//fix・//plan-only・
        # //parallel・//resume-allも含めたすべてのやり取りを共有・
        # 蓄積する(`_ChatLog`参照)。起動時刻をファイル名に持つログ
        # ファイルにも逐次書き出されるため、対話モードのプロセスを
        # 終了しても記録自体はディスク上に残る(次回起動時に自動で
        # 読み込んで引き継ぐ機能は今回のスコープ外。ログファイルを
        # 見返せば、別のセッションでも過去の会話内容を確認できる)。
        chat_log = _create_chat_log(out_dir)
        messages = chat_log
        session = _create_repl_prompt_session()
        # 仮の判断: 入力編集中・組織への問い合わせ中(応答待ち)のどちらで
        # Ctrl+Cが発生しても「2回連続」を正しく検出できるよう、対話モードの
        # セッションを通じて1つのインスタンスを使い回す(_DoubleInterruptGuard
        # の定義コメントを参照)。
        interrupt_guard = _DoubleInterruptGuard()
        job_runner = _create_background_job_runner()
        # 仮の判断: 対話的な修正セッション(`_FixSession`)の状態。//fixで
        # 対象プロジェクトが特定できるたびにここへセットし、コマンド無しの
        # 発言はこれが有効な間、優先的にそのプロジェクトへの継続的な修正
        # 依頼として扱う(詳細は`_FixSession`・`_begin_fix_session`を参照)。
        fix_session = None
        # 仮の判断(不具合修正: 対話プロトコル一時停止後、実装フェーズに
        # 繋がらない問題への対応): //agreeの合意フェーズ(対話プロトコル)が
        # セーフティリミット・人間の確認待ちで一時停止した場合、
        # `_ask_organization_collaborate`(バックグラウンドジョブとして実行
        # される)がこの箱へ再開に必要な情報を格納する。次の人間の発言が
        # 来た時点でこの箱を確認し、値があれば「一時停止への回答」として
        # 合意フェーズを続きから再開する(`_resume_organization_collaborate`)。
        pending_design_box = _PendingDesignDialogueBox()
        while True:
            text, terminate = _read_multiline_input(session, interrupt_guard)
            if terminate:
                break

            if fix_session is not None and fix_session.is_idle_expired():
                print(
                    f"[🔧 修正セッションを終了しました({fix_session.project_name}への"
                    "操作がしばらく無かったため)]"
                )
                fix_session = None

            # 仮の判断: 一時停止中の合意フェーズがある状態で、人間が明示的な
            # コマンド(//agree・//fix等)を新たに入力した場合は、「今の一時
            # 停止への回答」ではなく別の新しい指示だと判断できるため、再開待ち
            # 状態を破棄する(新規の//agreeが進行中の修正セッションを終了
            # させるのと同じ考え方)。
            if pending_design_box.peek() is not None and (
                text.startswith(RESUME_ALL_COMMAND) or text.startswith(AGREE_COMMAND)
                or text.startswith(PLAN_ONLY_COMMAND) or text.startswith(FIX_PROJECT_COMMAND)
                or text.startswith(PARALLEL_QUERY_COMMAND) or text.startswith(MULTI_QUERY_COMMAND)
            ):
                pending_design_box.take()
                print("[🧭 新しいコマンドが入力されたため、一時停止中だった合意フェーズの再開待ちを終了しました]")

            if text.startswith(RESUME_ALL_COMMAND):
                job_runner.submit(
                    lambda: _run_job_with_conversation_log(
                        messages, text, lambda: _run_resume_all(port, org_fingerprint, out_dir),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if text.startswith(AGREE_COMMAND):
                request = text[len(AGREE_COMMAND):].strip()
                if not request:
                    print(f"使い方: {AGREE_COMMAND} <制作依頼>  (例: {AGREE_COMMAND} ToDoリストのCLIツールを作って)")
                    continue
                # 仮の判断(依頼の項目3): 新規の//agreeが実行された場合、
                # 進行中の修正セッションは自動的に終了する。
                if fix_session is not None:
                    print(
                        f"[🔧 修正セッションを終了しました(新規の{AGREE_COMMAND}が"
                        "実行されたため)]"
                    )
                    fix_session = None
                job_runner.submit(
                    lambda request=request: _run_job_with_conversation_log(
                        messages, text,
                        lambda: _ask_organization_collaborate(
                            port, org_fingerprint, request, out_dir, enable_dialogue=True,
                            pending_box=pending_design_box,
                        ),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if text.startswith(PLAN_ONLY_COMMAND):
                plan_request = text[len(PLAN_ONLY_COMMAND):].strip()
                if not plan_request:
                    print(f"使い方: {PLAN_ONLY_COMMAND} <検討したい依頼>  (例: {PLAN_ONLY_COMMAND} ToDoリストのCLIツールを作って)")
                    continue
                job_runner.submit(
                    lambda plan_request=plan_request: _run_job_with_conversation_log(
                        messages, text,
                        lambda: _ask_organization_plan_only(port, org_fingerprint, plan_request, out_dir),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if text.startswith(FIX_PROJECT_COMMAND):
                fix_request = text[len(FIX_PROJECT_COMMAND):].strip()
                if not fix_request:
                    print(
                        f"使い方: {FIX_PROJECT_COMMAND} <修正依頼>  "
                        f"(例: {FIX_PROJECT_COMMAND} ID生成のロジックにバグがあるので直して)"
                    )
                    continue
                fix_session = _begin_fix_session(
                    port, org_fingerprint, out_dir, job_runner, fix_session, fix_request, chat_log=messages,
                )
                continue

            if text.startswith(PARALLEL_QUERY_COMMAND):
                command_text = text[len(PARALLEL_QUERY_COMMAND):].strip()
                job_runner.submit(
                    lambda command_text=command_text: _run_job_with_conversation_log(
                        messages, text,
                        lambda: _ask_organization_parallel(port, org_fingerprint, command_text, out_dir),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if text.startswith(MULTI_QUERY_COMMAND):
                question = text[len(MULTI_QUERY_COMMAND):].strip()
                if not question:
                    print(f"使い方: {MULTI_QUERY_COMMAND} <質問文>  (例: {MULTI_QUERY_COMMAND} こんにちは)")
                    continue
                messages.append({"role": "user", "content": question})
                try:
                    _ask_organization_multi(port, org_fingerprint, messages)
                except KeyboardInterrupt:
                    if _handle_repl_command_interrupt(interrupt_guard):
                        break
                continue

            # 仮の判断(平文からの自動ツール選択の復活、依頼への対応): 一度
            # (637cce6)は、対話プロトコルの一時停止直後の続きの発言まで
            # ゼロから判定し直される不具合のため、コマンド無しの発言を
            # 依頼文のキーワードから実行モードへ自動的に振り分ける仕組み
            # (`_classify_execution_mode`のディスパッチ呼び出し)を廃止し、
            # 常に「今の会話の続き」の単発質問として扱うようにしていた。
            # その後、会話履歴が一本化された(`_ChatLog`)ことで、
            # 「一時停止の直後かどうか」を直前の会話履歴のエントリから
            # 判別できるようになったため(`_last_turn_is_dialogue_pause`)、
            # その場合だけ自動判定をスキップして続きとして扱い、それ以外の
            # 新規の話しかけらしい場面では自動判定を復活させる。修正
            # セッション(`fix_session`)中は、それ自体が既に「今は継続中」
            # という状態を表しているため、これまで通り自動判定を経由せず
            # 常に対象プロジェクトへの継続的な修正依頼として扱う。
            if fix_session is not None and not _is_fix_session_end_phrase(text):
                _continue_fix_session(port, org_fingerprint, out_dir, job_runner, fix_session, text, chat_log=messages)
                continue
            elif fix_session is not None:
                # _is_fix_session_end_phrase(text) が真だった場合。
                print(f"[🔧 修正セッションを終了しました({fix_session.project_name})]")
                fix_session = None
                continue

            # 仮の判断(不具合修正: 対話プロトコル一時停止後、実装フェーズに
            # 繋がらない問題への対応): //agreeの合意フェーズが一時停止した
            # 直後であれば、`_last_turn_is_dialogue_pause`によるテキストの
            # マーカー判定を待たず、構造化された再開情報(`pending_design_box`)を
            # 優先的に使う。この発言を一時停止への回答として合意フェーズを
            # 続きから再開し、合意(または単独での計画確定)が得られ次第その
            # まま実装フェーズへ進む(`_resume_organization_collaborate`)。
            pending_design = pending_design_box.take()
            if pending_design is not None:
                print("[💬 対話プロトコルの一時停止直後の発言のため、合意フェーズを続きから再開します]")
                job_runner.submit(
                    lambda pending=pending_design, text=text: _run_job_with_conversation_log(
                        messages, text,
                        lambda: _resume_organization_collaborate(pending, text),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if _last_turn_is_dialogue_pause(messages):
                print("[💬 対話プロトコルの一時停止直後の発言のため、会話の続きとして扱います]")
                mode = EXECUTION_MODE_SINGLE
            else:
                mode = _classify_execution_mode(text, out_dir)
                print(f"[判断: {_execution_mode_reason_label(mode)}]")

            if mode == EXECUTION_MODE_COLLABORATE:
                job_runner.submit(
                    lambda text=text: _run_job_with_conversation_log(
                        messages, text,
                        lambda: _ask_organization_collaborate(
                            port, org_fingerprint, text, out_dir, enable_dialogue=True,
                            pending_box=pending_design_box,
                        ),
                    ),
                    queued_notice=_BACKGROUND_QUEUED_NOTICE,
                )
                continue

            if mode == EXECUTION_MODE_FIX_PROJECT:
                fix_session = _begin_fix_session(
                    port, org_fingerprint, out_dir, job_runner, fix_session, text, chat_log=messages,
                )
                continue

            messages.append({"role": "user", "content": text})
            try:
                if mode == EXECUTION_MODE_COMPARE:
                    _ask_organization_multi(port, org_fingerprint, messages)
                else:
                    _ask_organization(port, org_fingerprint, messages)
            except KeyboardInterrupt:
                if _handle_repl_command_interrupt(interrupt_guard):
                    break
                continue

        print("対話モードを終了します。")


def handle_chat(port: int, out_dir: str) -> None:
    """対話モード(フロント)のエントリーポイント。`python3 yoriai.py --chat`で
    起動される。このプロセス自身はポートのbindもmDNS/Tailscale探索も一切行わず、
    既に起動しているキッチン(常駐エージェント)の`/status`・`/chat`にHTTPで
    問い合わせるだけのクライアントとして動く。
    """
    token = config.load_token()
    if not token:
        print(NO_TOKEN_GUIDANCE)
        sys.exit(1)
    org_fingerprint = config.token_fingerprint(token)

    # 起動時に1回、キッチン(常駐エージェント)に到達できるか確認しておく
    # (--statusと同じ考え方。到達できない場合は案内を出して終了する)。
    _fetch_org_snapshot(port, org_fingerprint, fail_fast=True)

    _run_repl_client(port, org_fingerprint, out_dir)


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
    backends = card.get("models", {}).get("backends", [])

    if free_gb is not None and total_gb is not None:
        memory_line = f"    メモリ: 空き {free_gb}GB / 総 {total_gb}GB"
    else:
        memory_line = "    メモリ: 不明"

    lines = [
        f"[{index}] {device_name} {label}",
        f"    チップ: {chip}",
        memory_line,
        f"    利用可能なバックエンド: {', '.join(backends) if backends else 'なし'}",
        f"    ロード済みモデル: {', '.join(loaded) if loaded else 'なし'}",
        f"    インストール済みモデル({len(installed)}件): {', '.join(installed) if installed else 'なし'}",
    ]
    if last_seen is not None:
        lines.append(f"    最終確認: {_format_seconds_ago(last_seen)}")
    return "\n".join(lines)


def handle_status(port: int) -> None:
    # 仮の判断: --status は「今動いている常駐プロセスに問い合わせるだけ」の
    # 軽量なコマンドという要件のため、エージェントが起動していない場合は
    # 新たに起動したりはせず、案内を出して終了する(_fetch_org_snapshotが行う)。
    token = config.load_token()
    if not token:
        print(NO_TOKEN_GUIDANCE)
        sys.exit(1)
    org_fingerprint = config.token_fingerprint(token)

    data = _fetch_org_snapshot(port, org_fingerprint, fail_fast=True)

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
        # 仮の判断: 同一デバイスが複数経路(mDNS・Tailscale)で見えている場合、
        # 両方の経路を併記する(例: "(mDNS経由 / Tailscale経由)")。
        via_list = peer.get("via") or ["unknown"]
        label = f"({' / '.join(f'{v}経由' for v in via_list)})"
        print(_format_status_member(peer.get("card", {}), i, label, peer.get("last_seen")))
        print()

    print(f"合計: {len(peers) + 1}台のエージェントが組織に参加中")


# --init が値なしで指定された(合言葉を対話入力する)ことを表す印。
# 「--initそのものが指定されなかった」(default=None)と区別するために使う。
_INIT_INTERACTIVE = object()


def main():
    # 仮の判断: `readline`(macOSではlibedit)による行編集は、C言語レベルの
    # ロケール設定(`setlocale`)に従って多バイト文字(日本語の全角文字等)を
    # 1文字単位で正しく扱うかどうかが変わる。CPythonは起動時に自動でシステムの
    # ロケールを`setlocale(LC_ALL, "")`しないため、環境変数(LANG/LC_ALL)で
    # UTF-8系ロケールが指定されていても、明示的に呼ばないとCライブラリ側が
    # 反映しないことがある。呼び出しに失敗しても(ロケールが未生成の環境など)
    # 対話モード自体は行編集機能が多少不安定になるだけで動作は継続できるため、
    # 例外は握りつぶす。
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    # 仮の判断: input()が使う文字コードはOSのロケール設定(LANG/LC_ALL)に依存する。
    # ラズパイなどでシステムロケールがUTF-8になっていない(C/POSIXのままなど)環境では、
    # 端末側は自分のロケール(通常UTF-8)で正しく描画・送信しているにもかかわらず、
    # Python側がそれをロケールの指示通り別の文字コードとして解釈しようとして
    # UnicodeDecodeErrorになることがある(実機での報告により発覚)。「画面には正しく
    # 表示されているのにデコードに失敗する」のは、表示は端末側、デコードはPython側の
    # ロケール依存という別々の処理だから起こりうる。現代のほとんどの端末はUTF-8で
    # 送ってくる前提のため、システムロケールに関わらずstdinの文字コードを明示的に
    # UTF-8に固定する(万一それでも解釈できないバイト列があった場合は例外にせず
    # 置換文字に変換する)。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
    group.add_argument(
        "--chat", action="store_true",
        help="常駐中のYoriaiエージェント(キッチン)に接続し、対話的に組織のメンバーへ質問する"
             "(このプロセス自身はポートのbindやmDNS/Tailscale探索を行わないフロント専用)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_CARD_PORT,
        help=f"自己紹介カードを配信するHTTPポート番号(既定値: {DEFAULT_CARD_PORT}。0を指定するとOSに自動選択させる)",
    )
    parser.add_argument("--force", action="store_true", help="--init と併用し、既存トークンを確認の上で強制的に再発行する")
    parser.add_argument(
        "--dir", dest="out_dir", default=".", metavar="DIR",
        help=(
            f"--chat と併用し、{PARALLEL_QUERY_COMMAND}・{AGREE_COMMAND}・協業モードで保存するファイルの"
            f"保存先ディレクトリ(既定値: カレントディレクトリ)。{AGREE_COMMAND}・協業モードの生成物は"
            f"この配下の{PROJECTS_SUBDIR_NAME}/<プロジェクト名>/にまとめて保存される"
        ),
    )
    args = parser.parse_args()

    if args.init is not None:
        custom_passphrase = None if args.init is _INIT_INTERACTIVE else args.init
        handle_init(args.force, custom_passphrase)
        return

    if args.status:
        handle_status(args.port)
        return

    if args.chat:
        handle_chat(args.port, args.out_dir)
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

    run_agent(token, args.port)


if __name__ == "__main__":
    main()
