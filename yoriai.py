#!/usr/bin/env python3
"""Yoriai: 同一ネットワーク上のローカルLLMエージェントを mDNS で自動発見し、
自己紹介カード(JSON)を交換する最小構成プロトタイプ。

対象OS: macOS (Apple Silicon) / Linux (Raspberry Pi 4を含むDebian系など)。
"""

import argparse
import json
import locale
import logging
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
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
# ウェブ検索ツール(DuckDuckGo)
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
WEB_SEARCH_URL = "https://html.duckduckgo.com/html/"
# 仮の判断: DuckDuckGo側にブラウザ以外からのアクセスとして弾かれないよう、
# 一般的なブラウザのUser-Agentを名乗る。
WEB_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class _DuckDuckGoResultParser(_HTMLParser):
    """DuckDuckGoのHTML版(JS不要の検索結果ページ)から検索結果を抜き出す
    最小限のパーサー。`class="result__a"`のリンクをタイトル+URL、
    `class="result__snippet"`の要素を説明文として拾う。

    仮の判断: DuckDuckGo側のマークアップ変更に弱い非公式な方法だが、外部
    ライブラリ(ddgs等)が内部で使うRust製の`primp`のようなネイティブ拡張に
    依存すると、Raspberry Pi(aarch64)ではプリビルドのwheelが無く、
    ビルドにRustツールチェイン一式が必要になってビルド自体が失敗することが
    実機で確認された。Yoriaiはこれまでも(Linuxのネットワークインターフェース
    取得など)外部コマンド・ネイティブ拡張への依存を避けてstdlibで実装してきた
    方針のため、ここも`requests`とstdlibの`html.parser`だけで実装している。
    """

    def __init__(self):
        super().__init__()
        self.results = []
        self._section = None  # "title" | "snippet" | None
        self._current = None  # 収集中の {"title", "url", "snippet"}

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        attrs = dict(attrs)
        classes = attrs.get("class", "") or ""
        if "result__a" in classes.split():
            self._current = {"title": "", "url": self._extract_real_url(attrs.get("href", "")), "snippet": ""}
            self._section = "title"
        elif "result__snippet" in classes.split() and self._current is not None:
            self._section = "snippet"

    def handle_endtag(self, tag):
        if tag != "a" or self._section is None:
            return
        if self._section == "snippet" and self._current is not None:
            self.results.append(self._current)
            self._current = None
        self._section = None

    def handle_data(self, data):
        if self._section and self._current is not None:
            self._current[self._section] += data

    @staticmethod
    def _extract_real_url(href: str) -> str:
        # DuckDuckGoの検索結果は自前のリダイレクトリンク
        # (//duckduckgo.com/l/?uddg=<実URLをURLエンコードしたもの>&...)を
        # 経由するため、そこから元のURLを取り出す。
        if href.startswith("//"):
            href = "https:" + href
        query = urllib.parse.urlparse(href).query
        real_url = urllib.parse.parse_qs(query).get("uddg", [None])[0]
        return urllib.parse.unquote(real_url) if real_url else href


def web_search(query: str, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list:
    """DuckDuckGoのHTML版を直接スクレイピングしてウェブ検索し、結果のリストを返す。

    仮の判断: 検索バックエンドはAPIキー登録が不要ですぐ使えるDuckDuckGoを選んだ。
    非公式スクレイピングのため失敗することもあるが、失敗時は例外を投げずに
    空リストを返し、モデル側には「検索結果が得られなかった」ことだけ伝える。
    """
    try:
        resp = requests.post(
            WEB_SEARCH_URL,
            data={"q": query},
            headers={"User-Agent": WEB_SEARCH_USER_AGENT},
            timeout=(CHAT_CONNECT_TIMEOUT_SEC, WEB_SEARCH_TIMEOUT_SEC),
        )
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("ウェブ検索に失敗しました: %s", exc)
        return []

    parser = _DuckDuckGoResultParser()
    try:
        parser.feed(resp.text)
    except Exception as exc:
        logger.warning("検索結果の解析に失敗しました: %s", exc)
        return []

    results = []
    for r in parser.results[:max_results]:
        title = r["title"].strip()
        if not title:
            continue
        results.append({"title": title, "url": r["url"], "snippet": r["snippet"].strip()})
    return results


def _execute_tool_call(tool_call: dict) -> str:
    """モデルからのtool_call(OpenAI互換形式)を実行し、モデルに返す結果を
    JSON文字列として返す。
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
    順にyieldし、最後にそのターンで要求されたtool_calls一覧
    ({"tool_calls": [...]}、無ければ空リスト)をyieldする。
    """
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={"model": model, "messages": messages, "tools": tools, "stream": True},
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
                break
    except Exception as exc:
        yield {"error": str(exc)}
        return
    yield {"tool_calls": tool_calls}


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
            json={"model": model, "messages": messages, "tools": tools, "stream": True},
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
            delta = choices[0].get("delta", {})
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
    yield {"tool_calls": tool_calls}


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


def stream_chat_completion(model: str, messages: list):
    """モデル名からOllama/LM Studio/MLX-LMのどれにチャットを振るかを決め、
    正規化されたストリーミングイベント({"content": ...} / {"tool_call": <ツール名>} /
    {"done": True} / {"error": ...})を順にyieldする。

    モデルがツール(現状はweb_searchのみ)の呼び出しを要求した場合は、
    ここでツールを実行して結果を会話履歴に追加し、モデルに再度問い合わせる
    (最大MAX_TOOL_CALL_ROUNDSラウンドまで)。呼び出し元(REPL等)からは
    ツールの存在を意識せず、通常のチャットと同じように使える。

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
    tools = CHAT_TOOLS

    for round_num in range(MAX_TOOL_CALL_ROUNDS + 1):
        if model in get_ollama_loaded_models():
            turn = _stream_ollama_turn(model, messages, tools)
        elif model in get_mlx_lm_models():
            turn = _stream_mlx_lm_turn(model, messages, tools)
        else:
            turn = _stream_lmstudio_turn(model, messages, tools)

        tool_calls = []
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

        if (leaked or tools_rejected) and tools:
            tools = None  # このモデルにはツールを二度とオファーせず、素の会話として再試行する
            continue

        if not tool_calls or round_num == MAX_TOOL_CALL_ROUNDS:
            break

        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        for tool_call in tool_calls:
            tool_name = tool_call.get("function", {}).get("name", "")
            yield {"tool_call": tool_name}
            result = _execute_tool_call(tool_call)
            messages.append({
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.get("id"),
            })

    yield {"done": True}


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


def _stream_chat_from_candidate(candidate: dict, org_fingerprint: str, messages: list):
    """候補(自分自身を含む)のキッチンにHTTP経由(/chat)で問い合わせ、
    正規化されたストリーミングイベントを順にyieldする。
    """
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


def _collect_answer_from_candidate(candidate: dict, org_fingerprint: str, messages: list):
    """1候補に問い合わせ、回答をすべて集めて文字列として返す
    (content, error)のタプル。エラー時はcontentが空文字列になる。

    仮の判断: //multi では複数候補への問い合わせを並列に行うため、通常モード
    のように1文字ずつその場で画面に出す(ライブストリーミング)方式のままだと
    複数候補の出力が入り交じって読めなくなってしまう。そのため、この関数は
    ストリーミング自体はバックグラウンドで最後まで受信しきり、完成した
    回答をまとめて呼び出し元に返す(表示は呼び出し元が候補ごとに区切って行う)。
    """
    answer_parts = []
    error = None
    for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages):
        if "error" in event:
            error = event["error"]
            break
        content = event.get("content")
        if content:
            answer_parts.append(content)
        if event.get("done"):
            break
    return "".join(answer_parts), error


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
        answer, error = _collect_answer_from_candidate(candidate, org_fingerprint, list(messages))
        results[index] = (candidate, answer, error)
        with print_lock:
            print()
            print(f"--- {candidate['label']} (モデル: {candidate['model']}) ---")
            if error:
                print(f"(問い合わせに失敗しました: {error})")
            else:
                print(answer if answer else "(応答がありませんでした)")

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
    out_dir 配下にファイル名で保存する。`//parallel`(手動指定)と協業モード・
    `//agree`(合意フェーズ後の並行実装)の両方から共通して使われる下請け関数。

    保存に成功したタスクについて、`[{"filename":, "candidate":, "code":}, ...]`
    (タスクを書いた順)を返す。この戻り値は、協業モードのレビューフェーズ
    (`_run_review_phase`)が「どのメンバーがどのファイルを実装したか」を
    知るために使う。`//parallel`(手動指定)側は戻り値を使わない。

    仮の判断:
    - 割り当ては「タスクを書いた順」と「優先順位順に並べた候補」を先頭から
      1対1で対応させるだけの単純な方式にした。特定のメンバーを名指しで
      選ぶ構文は今回のスコープ外。
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
        answer, error = _collect_answer_from_candidate(candidate, org_fingerprint, task_messages)
        results[index] = (filename, candidate, answer, error)
        with print_lock:
            print()
            print(f"--- {filename} ← {candidate['label']} (モデル: {candidate['model']}) ---")
            if error:
                print(f"(問い合わせに失敗しました: {error})")
            else:
                print(answer if answer else "(応答がありませんでした)")

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

# 仮の判断: 設計担当への指示は、出力形式をできるだけ単純な1行1ファイル形式に
# 寄せるために日本語のプロンプトテンプレートとして持つ。ただし実際には
# モデルがこの形式を厳密に守るとは限らず、ファイル名を見出しとして詳細を
# 複数行にぶら下げる階層的な形式で返してくることもあるため、解析側
# (`_parse_module_breakdown`)はそれも扱えるようにしてある。
_MODULE_BREAKDOWN_PROMPT_TEMPLATE = """あなたはソフトウェア設計を担当します。以下の依頼を、複数人で分担して実装できるよう、複数のファイルに分割する実装計画を考えてください。

各ファイルが実装すべき内容には、他のファイルから呼び出される関数のシグネチャ(関数名・引数名・型・戻り値の型)や、やり取りするデータの形式(辞書のキー名など)を具体的に明記してください。担当が異なるファイル同士が正しく連携できるよう、インターフェースの記述は曖昧にせず、できるだけ厳密に書いてください。

重要: 複数のファイルの説明に同じ関数(例: あるファイルが呼び出す、別のファイルが定義する関数)が登場する場合、その関数名・引数名・戻り値の型は、すべてのファイルの説明文で一字一句まったく同じ表記に揃えてください。あるファイルの説明では`add_todo`、別のファイルの説明では`add_task`のように、同じ役割の関数に別々の名前を使うことは厳禁です。

出力は次の形式のみとし、他の説明文や前置き・後書きは一切含めないでください。ファイルごとに1行、「ファイル名: 実装すべき内容(インターフェースの詳細を含む)」という形式で出力してください(2〜4ファイル程度を想定します):

storage.py: <実装すべき内容をここに>
cli.py: <実装すべき内容をここに(storage.pyの行と完全に同じ関数名を使うこと)>

依頼内容: {request}
"""


def _build_module_breakdown_prompt(request: str) -> str:
    return _MODULE_BREAKDOWN_PROMPT_TEMPLATE.format(request=request)


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


def _ask_organization_collaborate(port: int, org_fingerprint: str, request: str, out_dir: str) -> None:
    """「〇〇を作って」のような制作依頼用: まず優先順位が最も高い1台に
    モジュール分割案とインターフェース設計を相談し(合意フェーズ)、
    その結果を`_dispatch_and_save_parallel_tasks`で異なるメンバーに
    並行実装させ(実装フェーズ)、最後にお互いが担当外のファイルを
    レビューし合う(レビューフェーズ、`_run_review_phase`)。

    仮の判断:
    - 「設計担当」の選び方は他の候補選びと同じ優先順位ロジック
      (コーディング系タスクとして最優先の候補)をそのまま流用する。
      設計専用のモデルを分けて選ぶ仕組みは今回のスコープ外。
    - 設計担当の回答が期待した形式(「ファイル名: 内容」の行)で
      1件も得られなかった場合は、その旨と回答全文を表示して中断する
      (フォーマットの自動修正・再質問までは今回のスコープ外)。
    """
    data = _fetch_org_snapshot(port, org_fingerprint)
    if data is None:
        return

    candidates = _select_chat_candidates(data.get("self", {}), data.get("peers", []), port, TASK_TYPE_CODING)
    if not candidates:
        print("組織内にロード済みモデルを持つメンバーがいません。")
        return

    architect = candidates[0]
    print(
        f"[🧭 合意フェーズ開始: まず {architect['label']} (モデル: {architect['model']}) に"
        f"モジュール分割案とインターフェース設計を相談しています...]"
    )

    breakdown_prompt = _build_module_breakdown_prompt(request)
    answer, error = _collect_answer_from_candidate(
        architect, org_fingerprint, [{"role": "user", "content": breakdown_prompt}],
    )
    if error:
        print(f"設計担当への問い合わせに失敗しました: {error}")
        return
    if not answer:
        print("設計担当から応答が得られませんでした。")
        return

    # 仮の判断: 解析後の内容だけでなく、設計担当の回答そのもの(生テキスト)も
    # 表示する。パース処理(_parse_module_breakdown)の解釈が意図と異なって
    # いないか、実機での動作確認時に見比べられるようにするため。
    print(f"[🧭 {architect['label']} の回答(合意フェーズの結果、そのまま表示)]")
    print(answer)

    tasks = _parse_module_breakdown(answer)
    if not tasks:
        print("設計担当の回答からファイル分割案を読み取れませんでした。上記の回答内容を確認してください。")
        return

    print(f"[📐 モジュール分割案がまとまりました({len(tasks)}ファイル): {', '.join(f for f, _ in tasks)}]")
    for filename, content in tasks:
        print(f"  - {filename}: {content}")

    # 仮の判断: 各担当への実装依頼には、自分のファイルの説明行だけでなく
    # 合意フェーズで決まった実装計画全体を埋め込む(理由は
    # _build_collaborative_implementation_request のコメントを参照)。
    enriched_tasks = [
        (filename, _build_collaborative_implementation_request(filename, content, tasks))
        for filename, content in tasks
    ]

    # 仮の判断: 生成物はYoriai本体と混ざらないよう、projects/<プロジェクト名>/
    # というサブディレクトリにまとめる。プロジェクト名は依頼文から簡易的に
    # 生成し、同名の既存プロジェクトがあれば連番で衝突を避ける。
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    project_name = _generate_project_name(request)
    project_dir = _resolve_project_dir(projects_root, project_name)
    print(f"[📁 生成物の保存先: {project_dir}]")

    print(f"[🔨 実装フェーズ開始: 合意した計画に基づき、{len(enriched_tasks)}件のファイルを異なるメンバーに並行実装させます]")
    implemented = _dispatch_and_save_parallel_tasks(enriched_tasks, candidates, org_fingerprint, project_dir)

    # 仮の判断: レビューフェーズは「お互いが担当外のファイルをレビューする」
    # 前提のため、実装に成功したファイルが2件以上無いと成立しない
    # (1件しか実装できなかった場合、担当外のレビュー担当が存在しない)。
    if len(implemented) < 2:
        print(f"[🔎 レビューフェーズはスキップします: 相互レビューを行うには実装済みファイルが2件以上必要です(保存先: {project_dir})]")
        return

    _run_review_phase(implemented, tasks, org_fingerprint, project_dir)


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

以下の観点でレビューしてください:
1. 実装計画で合意した内容(関数名・引数・戻り値の型・データ形式)と、実際のコードが一致しているか
2. あなたが担当した{reviewer_own_filename}と正しく連携できそうか
3. 例外処理の欠如・タイポなど、コードとして明らかな不備がないか

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


def _build_review_prompt(filename: str, code: str, reviewer_own_filename: str, reviewer_own_code: str, full_plan: str) -> str:
    return _REVIEW_PROMPT_TEMPLATE.format(
        filename=filename, code=code,
        reviewer_own_filename=reviewer_own_filename, reviewer_own_code=reviewer_own_code,
        full_plan=full_plan,
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


def _review_one_file(
    filename: str, code: str, reviewer: dict, reviewer_own_filename: str, reviewer_own_code: str,
    full_plan: str, org_fingerprint: str, round_label: str,
) -> tuple:
    """1回分のレビューを実行し、(問題なしかどうか, 指摘内容)を返す。
    問い合わせ自体が失敗した場合も「問題あり」として扱う(暴走防止のため
    無条件に成功扱いにはしない)。
    """
    print(f"[🔎 {round_label}] {reviewer['label']} が {filename} をレビューしています...")
    prompt = _build_review_prompt(filename, code, reviewer_own_filename, reviewer_own_code, full_plan)
    answer, error = _collect_answer_from_candidate(reviewer, org_fingerprint, [{"role": "user", "content": prompt}])
    if error or not answer:
        message = error or "応答がありませんでした"
        print(f"[{reviewer['label']}が{filename}をレビュー] 問い合わせに失敗しました: {message}")
        return False, f"レビューへの問い合わせに失敗しました: {message}"

    ok, feedback = _parse_review_verdict(answer)
    if ok:
        print(f"[{reviewer['label']}が{filename}をレビュー] 問題なし")
    else:
        print(f"[{reviewer['label']}が{filename}をレビュー] 問題あり: {feedback}")
    return ok, feedback


def _request_fix(filename: str, code: str, feedback: str, owner: dict, full_plan: str, org_fingerprint: str, out_dir: str):
    """レビューで指摘された内容を担当メンバーに伝え、修正版のコードを
    取得して保存する。修正後のコード文字列を返し、失敗時はNoneを返す。
    """
    print(f"[🔧 {owner['label']} に {filename} の修正を依頼しています...]")
    prompt = _build_fix_prompt(filename, code, feedback, full_plan)
    answer, error = _collect_answer_from_candidate(owner, org_fingerprint, [{"role": "user", "content": prompt}])
    if error or not answer:
        print(f"  → 修正依頼への問い合わせに失敗しました: {error or '応答がありませんでした'}")
        return None

    fixed_code = _extract_code_from_answer(answer)
    dest_path = os.path.join(out_dir, filename)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(fixed_code)
    print(f"[💾 修正版の {dest_path} を保存しました (担当: {owner['label']})]")
    return fixed_code


def _review_and_fix_one_file(
    filename: str, owner: dict, code: str, reviewer: dict, reviewer_own_filename: str, reviewer_own_code: str,
    full_plan: str, org_fingerprint: str, out_dir: str,
) -> bool:
    """1ファイル分のレビュー→(必要なら)修正→再レビューを行い、最終的に
    「問題なし」になったかどうかを返す。

    仮の判断: ループではなく「1回目のレビュー」→「(問題があれば)修正」→
    「修正後の再レビュー」という3ステップを直列に明示的に呼び出す構造に
    した。ループにして回数を変数でカウントする実装だと、条件分岐を
    間違えた場合に暴走するリスクが残るが、この構造なら最大2回のレビュー
    (+その間の1回の修正)しか物理的に発生し得ない。
    """
    ok, feedback = _review_one_file(
        filename, code, reviewer, reviewer_own_filename, reviewer_own_code, full_plan, org_fingerprint, "1回目のレビュー",
    )
    if ok:
        return True

    fixed_code = _request_fix(filename, code, feedback, owner, full_plan, org_fingerprint, out_dir)
    if fixed_code is None:
        return False

    ok, _feedback = _review_one_file(
        filename, fixed_code, reviewer, reviewer_own_filename, reviewer_own_code, full_plan, org_fingerprint, "修正後の再レビュー",
    )
    return ok


def _run_review_phase(implemented: list, agreed_plan: list, org_fingerprint: str, out_dir: str) -> None:
    """実装フェーズで生成された各ファイルを、担当外のメンバーにレビュー
    させる。`implemented`は`_dispatch_and_save_parallel_tasks`が返す
    `[{"filename":, "candidate":, "code":}, ...]`(タスクを書いた順、
    各要素の担当は互いに異なる)。

    仮の判断: レビュー担当は「次の要素の担当メンバー」を円環状(最後の
    要素の次は先頭に戻る)に割り当てる単純な方式にした。2ファイル・
    2メンバーの典型的なケースでは「お互いが相手をレビューする」に自然と
    一致する。ファイル数が3件以上に増えた場合の「誰が誰をレビューするのが
    最適か」という設計はさらに検討の余地があるが、今回のスコープでは
    「担当外の誰かが必ずレビューする」ことだけを保証する方式にとどめた。
    """
    print()
    print("[🔎 レビューフェーズ開始: お互いが担当外のファイルをレビューします]")

    full_plan = "\n".join(f"{fn}: {content}" for fn, content in agreed_plan)
    n = len(implemented)
    unresolved = []

    for i, entry in enumerate(implemented):
        reviewer_entry = implemented[(i + 1) % n]
        ok = _review_and_fix_one_file(
            filename=entry["filename"], owner=entry["candidate"], code=entry["code"],
            reviewer=reviewer_entry["candidate"], reviewer_own_filename=reviewer_entry["filename"],
            reviewer_own_code=reviewer_entry["code"], full_plan=full_plan,
            org_fingerprint=org_fingerprint, out_dir=out_dir,
        )
        if not ok:
            unresolved.append(entry["filename"])

    print()
    if unresolved:
        print(f"[⚠️ 未解決の指摘が残っています: {', '.join(unresolved)} (保存先: {out_dir})]")
    else:
        print(f"[✅ レビュー完了 (保存先: {out_dir})]")


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


def _classify_execution_mode(text: str) -> str:
    """依頼文から実行モード(単発/比較/協業)を判定する。

    仮の判断: キーワードベースの単純な判定にとどめる。協業系キーワード
    (「作って」等)を最優先で確認し、次に比較系キーワード(「意見を聞かせて」等)を
    確認する。「作ってほしいものについて意見を聞かせて」のような複合的な
    依頼文の優先順位は今回のスコープでは厳密には詰めない。どちらにも
    該当しなければ単発モードとする。
    """
    for keyword in _COLLABORATE_MODE_KEYWORDS:
        if keyword in text:
            return EXECUTION_MODE_COLLABORATE
    for keyword in _COMPARE_MODE_KEYWORDS:
        if keyword in text:
            return EXECUTION_MODE_COMPARE
    return EXECUTION_MODE_SINGLE


def _execution_mode_reason_label(mode: str) -> str:
    if mode == EXECUTION_MODE_COLLABORATE:
        return "制作依頼と判断し、事前すり合わせを行います"
    if mode == EXECUTION_MODE_COMPARE:
        return "複数の意見を求めていると判断し、複数メンバーに問い合わせます"
    return "単発の質問と判断しました"


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


# 仮の判断: 継続入力中(まだ空行を入力していない、複数行メッセージの
# 2行目以降)であることが一目で分かるよう、"..."系のプロンプトにする。
# 幅を"Yoriai> "(半角8文字相当)にできるだけ合わせている。
_REPL_FIRST_LINE_PROMPT = "Yoriai> "
_REPL_CONTINUATION_PROMPT = "...  "


def _read_multiline_input() -> tuple:
    """対話モードの1メッセージ分の入力を読み取る。

    仮の判断: 「1行入力してEnterを押した時点ではまだ送信しない」
    「空行(Enterのみ)が来たら、それまでの行をすべて連結して1メッセージ
    として確定する」という仕様にした。エディタ等から複数行の文章を
    貼り付けた場合、各行の改行がそれぞれ別々のEnterとして届くため、
    「1行=即送信」のままだと文章が意図せず分割送信されてしまう
    (実機で報告された不具合)。空行を明示的な区切りにすることで、
    1行だけの質問(1行入力→空行で即送信、という2回の操作)にも、
    複数行の文章(貼り付け・複数行の手入力→最後に空行で送信)にも
    同じ操作感で対応できる。

    戻り値は`(text, terminate)`のタプル。`terminate`が`True`の場合、
    対話モード自体を終了すべきことを示す(EOF、入力前(まだ何も
    入力していない状態)でのCtrl+C、または最初の1行として単独で
    入力された"exit"/"quit")。`terminate`が`False`の場合、`text`が
    確定したメッセージ本文(空にはならない)。

    仮の判断: "exit"/"quit"による終了判定は、複数行入力の最初の1行
    (まだ他の行を1行も入力していない状態)として、それ単体で入力された
    場合にのみ有効にする。継続入力中(2行目以降)に"exit"とだけ書かれた
    行が来ても、それはメッセージ本文の一部として扱う(空行が来るまで
    送信を待つ)。
    """
    lines = []
    while True:
        prompt = _REPL_FIRST_LINE_PROMPT if not lines else _REPL_CONTINUATION_PROMPT
        try:
            raw = input(prompt)
        except EOFError:
            print()
            return "", True
        except KeyboardInterrupt:
            print()
            if not lines:
                # 仮の判断: まだ何も入力していない状態でのCtrl+Cは、
                # 対話モード自体を終了する(従来通りの挙動)。
                return "", True
            # 継続入力中のCtrl+Cは、そのメッセージ全体を破棄して
            # 最初の入力待ちに戻る(対話モード自体は終了しない)。
            lines = []
            continue
        except UnicodeDecodeError:
            # 仮の判断: 端末側の文字コードの乱れ(IME入力の途中経過や、ターミナルの
            # 表示崩れなど)で読み取れないバイト列が来ることがある実機での報告により
            # 発覚した。この1行だけ読み捨て、それまでの入力内容は保ったまま
            # 続けて入力を待つ(ここでクラッシュさせると常駐サービスは無事でも
            # フロントだけ落ちてしまうため)。
            print("入力を文字コードとして読み取れませんでした。もう一度入力してください。")
            continue

        if not lines and raw.strip().lower() in ("exit", "quit"):
            return "", True

        if raw.strip() == "":
            if not lines:
                # まだ何も入力していない状態での空Enterは、何もせず
                # 次の入力を待つ(従来通りの挙動)。
                continue
            return "\n".join(lines).strip(), False

        lines.append(raw)


def _run_repl_client(port: int, org_fingerprint: str, out_dir: str) -> None:
    print()
    print("=== Yoriai 対話モード ===")
    print("組織のメンバーに質問できます。終了するには exit または quit とだけ入力するか、")
    print("入力前(何も入力していない状態)でCtrl+Cを押してください。")
    print("1行入力してEnterを押しただけではまだ送信されません。空行(Enterのみ)を")
    print("入力した時点で、それまでの行をすべて連結して1つのメッセージとして送信します")
    print("(1行だけの質問は、入力→Enter→もう一度Enter、の2回の操作で送信できます)。")
    print("コマンドを付けずに話しかけると、依頼内容から単発/比較/協業のどのモードで")
    print("問い合わせるかを自動的に判断します(判断理由は[判断: ...]として表示されます)。")
    print(f"{MULTI_QUERY_COMMAND} <質問文> で、自動判定に関わらず必ず空きリソース上位{MULTI_QUERY_TARGET_COUNT}台に同時に質問できます。")
    print(
        f"{AGREE_COMMAND} <制作依頼> で、自動判定に関わらず必ず事前すり合わせ(合意フェーズ)を経て並行実装させます"
        f"(生成物は{os.path.join(out_dir, PROJECTS_SUBDIR_NAME)}/<プロジェクト名>/に保存されます)。"
    )
    print(
        f"{PARALLEL_QUERY_COMMAND} <ファイル名1>:<依頼1> | <ファイル名2>:<依頼2> ... で、"
        f"割り振り内容を自分で指定し、異なる依頼を異なるメンバーに同時に振り分け、"
        f"回答からコードを抽出して{out_dir}に保存できます。"
    )
    print()

    # 仮の判断: 会話履歴(messages)はこのセッション内でのみ保持し、終了したら破棄する。
    # 次回起動時に前回の会話を引き継ぐ機能は今回のスコープ外。
    messages = []
    while True:
        text, terminate = _read_multiline_input()
        if terminate:
            break

        if text.startswith(AGREE_COMMAND):
            request = text[len(AGREE_COMMAND):].strip()
            if not request:
                print(f"使い方: {AGREE_COMMAND} <制作依頼>  (例: {AGREE_COMMAND} ToDoリストのCLIツールを作って)")
                continue
            try:
                _ask_organization_collaborate(port, org_fingerprint, request, out_dir)
            except KeyboardInterrupt:
                print()
            continue

        if text.startswith(PARALLEL_QUERY_COMMAND):
            command_text = text[len(PARALLEL_QUERY_COMMAND):].strip()
            try:
                _ask_organization_parallel(port, org_fingerprint, command_text, out_dir)
            except KeyboardInterrupt:
                print()
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
                print()
            continue

        # 仮の判断: 明示コマンド(//agree・//parallel・//multi)のいずれにも
        # 一致しない通常の入力は、依頼文の内容から実行モードを自動判定する。
        # 判定根拠は既存のメンバー選定理由の表示と同じスタイルで
        # 「[判断: ...]」として一言添える。
        mode = _classify_execution_mode(text)
        print(f"[判断: {_execution_mode_reason_label(mode)}]")

        if mode == EXECUTION_MODE_COLLABORATE:
            try:
                _ask_organization_collaborate(port, org_fingerprint, text, out_dir)
            except KeyboardInterrupt:
                print()
            continue

        messages.append({"role": "user", "content": text})
        try:
            if mode == EXECUTION_MODE_COMPARE:
                _ask_organization_multi(port, org_fingerprint, messages)
            else:
                _ask_organization(port, org_fingerprint, messages)
        except KeyboardInterrupt:
            print()
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
        label = f"({peer.get('via', 'unknown')}経由)"
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
