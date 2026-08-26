"""Yoriaiのツール実行層: ウェブ検索(SearXNG)と、既存プロジェクトへの
修正依頼専用のファイル操作ツール群(read_file/write_file/edit_file等)。

仮の判断(モジュール分割第一弾への対応): これまで`yoriai.py`単一ファイルに
実装されていたツール群のうち、依存が少なく独立性の高い「web_search関連」
「プロジェクトファイル操作ツール」をここに切り出した。関数のロジックは
`yoriai.py`から一切変更していない(コピー＆import配線の変更のみ)。

ここに定義された関数のうち一部(`_write_project_file`・`_run_project_command`・
`_collect_answer_with_project_tools`・`_syntax_check_all_files`・
`_ask_organization_multi`・`PROJECT_TOOLS_SCHEMAS`・`PROJECT_TOOLS_CLIENT_NAMES`・
`web_search`・`_execute_tool_call`・`_looks_like_tools_related_error`・
`WEB_SEARCH_TOOL_NAME`・`WEB_SEARCH_TOOL_SCHEMA`)は、まだ`yoriai.py`側に
残っているコードからも呼ばれるため、`yoriai.py`側で`from tools import ...`
して使う。逆にここに置かれた関数の一部は、まだ`yoriai.py`側に残っている
関数(`_check_file_syntax`・`_list_project_files`・`_print_tagged`・
`_read_project_file_fresh`・`_search_in_project_file`・
`_planned_filenames_from_progress`・`_parse_progress_markdown`・
`_write_progress_md`・`_fetch_org_snapshot`・`_classify_task`・
`_select_chat_candidates`・`_selection_reason_label`・
`_stream_chat_from_candidate`・`_collect_answer_from_candidate`)を呼ぶ
必要があるが、`yoriai.py`が本モジュールをimportしているため、モジュール
読み込み時点でのトップレベルimportは循環importになってしまう。そのため、
これらは実際に呼ばれる関数の内部で遅延import(呼び出し時には両モジュール
とも読み込みが完了しているため問題なく解決できる)している。
"""

import datetime
import json
import logging
import os
import re
import select
import subprocess
import threading
import time

import requests

from yoriai_types import (
    CHAT_CONNECT_TIMEOUT_SEC,
    CHAT_MAX_OUTPUT_TOKENS,
    MULTI_QUERY_TARGET_COUNT,
    PROGRESS_FILENAME,
    READ_FILE_TOOL_NAME,
    READ_FILE_TOOL_SCHEMA,
    SEARCH_IN_FILE_TOOL_NAME,
    SEARCH_IN_FILE_TOOL_SCHEMA,
)

logger = logging.getLogger("yoriai")

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

# 仮の判断: 4xxのうち400(Bad Request)/422(Unprocessable Entity)は
# 「リクエストの中身が原因で拒否された」ことを示す代表的なクラスのエラーだが、
# LM Studioなどのバックエンドは、tools付きリクエストが原因のエラー以外にも
# 同じ400を返してくることが実機の検証で分かった。ステータスコードだけでは
# 判別できないため、エラーメッセージの本文にツール/Function Calling関連の
# 語が含まれているかどうかも合わせて確認する(呼び出し元はyoriai.py側の
# `stream_chat_completion`で、`TOOLS_UNSUPPORTED_STATUS_CODES`と組み合わせて
# 使われる)。
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

# 仮の判断(edit_fileの追加に伴う変更): 以前は「既存ファイルの一部だけを
# 直したい場合も、ファイル全体をここに渡すこと」と明示的に指示していたが、
# これには2つの実害があった。(1)ファイル破壊: 数百行のファイルの数行を
# 直すためだけに毎回全体を出力させるため、CHAT_MAX_OUTPUT_TOKENSに達して
# 途中で切れると、切れたままのコードが書き込まれてしまう。(2)意図しない
# 改変: ローカルLLMは全文を通すたびに、触る必要のないコメントや実装まで
# 書き換えてしまいやすい。edit_file(一意な文字列の置換)を新設したことで、
# 指示を逆転させ、部分修正にはedit_fileを使うよう促す。
WRITE_FILE_TOOL_NAME = "write_file"
WRITE_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": WRITE_FILE_TOOL_NAME,
        "description": (
            "新規ファイルの作成、またはファイル全体を作り直す場合に使う。"
            "'サブディレクトリ名/ファイル名'のようにサブディレクトリ内のパスを指定することもでき、"
            "そのサブディレクトリがまだ存在しなければ自動的に作成される"
            "(make_directoryを先に呼ぶ必要はない)。"
            "既存ファイルの一部だけを直したい場合はedit_fileを使うこと"
            "(write_fileで全体を書き直すと、直す必要のない箇所まで変わってしまったり、"
            "応答の長さ制限で途中で切れてファイルが壊れたりする恐れがある)。"
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

# 仮の判断: replace_allのようなオプションは付けない。一意マッチを強制する
# ことがこのツールの安全性の根拠であり、ローカルLLMに一括置換の権限を
# 渡すと、たまたま複数箇所に出現する短い文字列(例: 変数名や共通の記法)を
# old_stringに指定してしまった場合に、意図しない箇所まで一斉に書き換えて
# しまう恐れがある。一意に定まらない場合は、呼び出し元に前後の文脈を
# 広げて再試行させる(Claude CodeのEditツールと同じ設計方針)。
EDIT_FILE_TOOL_NAME = "edit_file"
EDIT_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": EDIT_FILE_TOOL_NAME,
        "description": (
            "既存ファイルの一部だけを置き換える(部分修正)。write_fileと違い、"
            "ファイル全体ではなく変更したい箇所だけを渡せばよいため、ファイルが壊れたり"
            "無関係な箇所まで書き換わったりする心配が無い。"
            "old_stringはファイル内で一意に定まる必要があるので、変更したい行だけでなく、"
            "その前後の行も含めて(インデント・空白も含めて実際のファイルと完全に一致する形で)"
            "十分な文脈を含めること。new_stringに空文字列を渡すと、old_stringの箇所を"
            "削除したことになる。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "編集するファイル名(例: utils.py、またはtemplates/base.htmlのようなサブディレクトリ内のパス)",
                },
                "old_string": {
                    "type": "string",
                    "description": "置換前の文字列。ファイル内で一意に定まるだけの前後の文脈を含めること(インデント・空白も含めて実際のファイルの中身と完全に一致させる)",
                },
                "new_string": {
                    "type": "string",
                    "description": "置換後の文字列。空文字列を渡すとold_stringの箇所を削除したことになる",
                },
            },
            "required": ["filename", "old_string", "new_string"],
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
    EDIT_FILE_TOOL_SCHEMA, MOVE_FILE_TOOL_SCHEMA, DELETE_FILE_TOOL_SCHEMA, MAKE_DIRECTORY_TOOL_SCHEMA,
    RUN_COMMAND_TOOL_SCHEMA,
]
PROJECT_TOOLS_CLIENT_NAMES = {
    READ_FILE_TOOL_NAME, SEARCH_IN_FILE_TOOL_NAME, LIST_DIR_TOOL_NAME, WRITE_FILE_TOOL_NAME,
    EDIT_FILE_TOOL_NAME, MOVE_FILE_TOOL_NAME, DELETE_FILE_TOOL_NAME, MAKE_DIRECTORY_TOOL_NAME,
    RUN_COMMAND_TOOL_NAME,
}


def _list_project_directory(project_dir: str) -> str:
    from yoriai import _list_project_files
    return json.dumps({"files": _list_project_files(project_dir)}, ensure_ascii=False)


def _syntax_check_result_fields(filename_for_syntax: str, text: str, file_path: str) -> dict:
    """`_check_file_syntax`の結果を、ツールの返り値にそのまま混ぜ込める
    フィールドの辞書にする共通ヘルパー。write_file・edit_fileの両方が
    「書き込み直後にその場でフィードバックし、モデル自身が次のラウンドで
    直せるようにする」という同じ設計を必要とするため、ここに切り出して
    共有する。
    """
    from yoriai import _check_file_syntax
    status, detail = _check_file_syntax(filename_for_syntax, text, file_path=file_path)
    if status == "ok":
        return {"syntax_ok": True}
    if status == "error":
        return {"syntax_ok": False, "syntax_error": detail}
    return {"syntax_check_skipped": detail}  # skipped


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
    result.update(_syntax_check_result_fields(os.path.basename(safe_path), text, safe_path))
    return json.dumps(result, ensure_ascii=False)


def _edit_project_file(project_dir: str, filename: str, old_string: str, new_string: str) -> str:
    """edit_fileツールの応答本体。`old_string`がファイル内でちょうど1箇所に
    一致する場合のみ`new_string`に置き換える(一意マッチの強制がこのツールの
    安全性の根拠なので、0件・複数件はどちらもエラーとし、ファイルには
    一切触れない)。

    仮の判断: パス解決は他の全ツールと同じく`_resolve_safe_project_path`を
    経由する(PROGRESS.mdの保護もこれで効く)。ファイルが存在しない場合は、
    `_read_project_file_fresh`と同様にPROGRESS.mdの計画に含まれる名前かどうかを
    確認し、「計画上は存在するがまだ未実装」なのか「そもそも計画に無い名前」
    なのかを区別して伝える(read_fileと同じ理由。詳細は
    `_planned_filenames_from_progress`定義部のコメントを参照)。
    """
    from yoriai import _planned_filenames_from_progress
    safe_path, error = _resolve_safe_project_path(project_dir, filename)
    if error:
        return json.dumps({"ok": False, "message": error}, ensure_ascii=False)
    if not os.path.isfile(safe_path):
        planned = _planned_filenames_from_progress(project_dir)
        if planned and os.path.basename(filename or "") not in planned:
            return json.dumps({
                "ok": False,
                "message": (
                    f"'{filename}' は実装計画(PROGRESS.mdのモジュール分割案)に"
                    "含まれていないファイル名です。新規に作成するつもりであれば"
                    "write_fileを使ってください。そうでなければ、計画の記述ミスの"
                    "可能性があります。計画に実在するファイル: "
                    f"{', '.join(sorted(planned))}"
                ),
            }, ensure_ascii=False)
        return json.dumps(
            {"ok": False, "message": "そのファイルはまだ存在しません(未実装です)。新規作成の場合はwrite_fileを使ってください。"},
            ensure_ascii=False,
        )

    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"読み取りに失敗しました: {exc}"}, ensure_ascii=False)

    match_count = text.count(old_string) if old_string else 0
    if match_count == 0:
        return json.dumps({
            "ok": False,
            "message": (
                "指定された文字列が見つかりませんでした。read_fileで現在の内容を確認してから、"
                "実際のファイルの中身と完全に一致する文字列を指定してください"
                "(インデントや空白も含めて一致する必要があります)。"
            ),
        }, ensure_ascii=False)
    if match_count > 1:
        # 仮の判断: モデルが次の試行で範囲を広げる判断材料になるよう、
        # 一致した箇所の行番号一覧も返す(1-indexed。read_file/search_in_fileの
        # 行番号表記と揃える)。
        matched_lines = []
        offset = 0
        for _ in range(match_count):
            idx = text.index(old_string, offset)
            matched_lines.append(text.count("\n", 0, idx) + 1)
            offset = idx + 1
        return json.dumps({
            "ok": False,
            "message": (
                f"指定された文字列が{match_count}箇所に一致しました。どの箇所を編集するか"
                "一意に定まるよう、前後の行を含めて範囲を広げてください。"
            ),
            "matched_lines": matched_lines,
        }, ensure_ascii=False)

    new_text = text.replace(old_string, new_string, 1)
    try:
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except Exception as exc:
        return json.dumps({"ok": False, "message": f"書き込みに失敗しました: {exc}"}, ensure_ascii=False)

    result = {"ok": True, "filename": _project_relpath(project_dir, safe_path)}
    result.update(_syntax_check_result_fields(os.path.basename(safe_path), new_text, safe_path))
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
    from yoriai import _parse_progress_markdown, _print_tagged, _write_progress_md
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
    "python3": {"-c", "-i"},
    "python": {"-c", "-i"},
    "node": {"-e", "--eval", "-p", "--print", "-r", "--require", "-i", "--interactive"},
    "ruby": {"-e"},
    "perl": {"-e", "-E"},
    "php": {"-r"},
}

# 仮の判断(統合検証ループへの対応): 以前は`-m`もインタプリタ全体への
# ブロック対象フラグとして一律禁止していたが、これでは`python3 -m pytest`
# `python3 -m py_compile`のような正当な検証手段まで塞いでしまい、
# 統合検証ループ(実行して落ちたら直す)の実現の妨げになる。そこで`-m`は
# 全面禁止をやめ、直後に来るモジュール名がこの許可リストにある場合のみ
# 通す方式に緩和する(検証コマンド・run_commandの両方に効く)。
# `http.server`はサーバーが起動しっぱなしになるが、RUN_COMMAND_TIMEOUT_SEC
# (10秒)で必ず強制終了されるため問題ない。判断に迷うモジュール
# (`webbrowser`・`pdb`・`venv`等、任意のコード実行や外部プロセス起動に
# つながりうるもの)は含めず、必要になった時点で個別に追加する方針とする。
_PYTHON_ALLOWED_MODULES = {"pytest", "unittest", "py_compile", "compileall", "json.tool", "http.server"}


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

    if parts[0] in ("python3", "python") and "-m" in parts[1:]:
        m_index = parts.index("-m")
        module = parts[m_index + 1] if m_index + 1 < len(parts) else None
        if module not in _PYTHON_ALLOWED_MODULES:
            return None, (
                f"'{parts[0]} -m {module or ''}'は使用できません。"
                f"-mで実行できるのは次のモジュールのみです: {', '.join(sorted(_PYTHON_ALLOWED_MODULES))}"
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
    from yoriai import _print_tagged, _read_project_file_fresh, _search_in_project_file
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
    if name == EDIT_FILE_TOOL_NAME:
        filename = arguments.get("filename", "")
        _print_tagged(print_lock, tag, f"[✂️ {filename or '(不明なファイル)'} の一部を編集しています...]")
        return _edit_project_file(
            project_dir, filename, arguments.get("old_string", ""), arguments.get("new_string", ""),
        )
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
MAX_PROJECT_TOOL_ROUNDS = 500

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
# run_commandはファイルを変更しないため対象外。edit_fileはwrite_fileと
# 同じくファイルの中身を実際に書き換えるツールのため、同様に含める。
_MUTATING_PROJECT_TOOL_NAMES = {
    WRITE_FILE_TOOL_NAME, EDIT_FILE_TOOL_NAME, MOVE_FILE_TOOL_NAME, DELETE_FILE_TOOL_NAME,
}

# 仮の判断(実機報告への対応): MAX_PROJECT_TOOL_ROUNDSは「暴走してもいずれ
# 止まる」ための歯止めであって、「同じ検索・一覧確認を延々繰り返して
# 一向に前進しない」こと自体の検知にはならない(上限を50→500に緩和した
# ところ、read_file/search_in_file/list_dirだけを回り続けて
# write_file/edit_fileに一度も到達しないまま500ラウンドかけて堂々巡りする
# 実例が報告された)。回数をいくら許容してもこの種の停滞は解消しないため、
# 「同じツール名+同じ引数の(ファイルを変更しない)呼び出しが、直近の
# 変更成功から何回連続したか」を別途数え、これがこの閾値に達した時点で
# ラウンド上限を待たずに、まず1度だけモデルに繰り返しを自覚させて
# 修正へ着手するよう促し(_PROJECT_TOOL_LOOP_NUDGE_MESSAGE)、それでも
# 同じ繰り返しが解消しなければ打ち切る。ただ打ち切って人間に投げ返す
# だけでは、//resume-allで再開しても同じ堂々巡りを繰り返すだけだった
# (実機で2回連続再現)ための対応。3回(=最低でも一往復は「様子見の
# 繰り返し」を許し、それでも変わらなければ停滞と判断する)とした。
PROJECT_TOOL_LOOP_REPEAT_LIMIT = 3

# 仮の判断(実機報告への対応): 促しても堂々巡りが解消しない場合、それは
# 「このモデルの能力不足」ではなく「1つのサブタスクとして複数ファイルに
# またがる調査+修正を丸ごと詰め込んだ、タスクの設計自体が大きすぎる」
# 可能性が高いという実機報告があった(index.html・plugins-guide.html・
# workflow.htmlの3ファイルへのnav属性追加を1サブタスクにまとめていた)。
# `_collect_answer_with_project_tools`が返す`error`にこの文字列が含まれて
# いるかどうかで、呼び出し元(`yoriai.py`の`_run_fix_task_queue`)が
# 「堂々巡りによる打ち切りだったか」を、他の種類の失敗(接続エラー・
# タイムアウト・ラウンド上限到達)と区別できるようにする。エラー文言の
# 一部をマーカーとして使う簡便な方法だが、この文字列は本ファイル内の
# この打ち切り処理でしか使っていないため十分に一意(戻り値のタプルに
# 新しいフィールドを追加すると、既存の全呼び出し元・テストの戻り値の
# 分解を変更する必要が生じてしまうため、既存の型を変えずに済むこちらを
# 選んだ)。
PROJECT_TOOL_LOOP_ERROR_MARKER = "堂々巡り"


def _normalize_project_tool_call(tool_call: dict) -> tuple:
    """`tool_call`から`(name, arguments)`を取り出す。`arguments`がJSON
    文字列で渡ってきた場合も辞書に戻す(`_execute_project_tool_call`の
    引数正規化処理と同じ考え方)。停滞検知(`_project_tool_call_repeat_
    key`)・実際のツール実行の双方から使われる共通の正規化処理。
    """
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
    return name, arguments


def _project_tool_call_repeat_key(name: str, arguments: dict) -> str:
    """`(name, arguments)`を、停滞検知用に「同じ呼び出しかどうか」を
    比較できる文字列キーに正規化する。キー順序を揃えることで、見た目の
    違い(キー順序等)だけで別の呼び出しと誤判定しないようにする。
    """
    return name + ":" + json.dumps(arguments, sort_keys=True, ensure_ascii=False)


def _mutated_filename_from_tool_result(tool_call: dict, result: str) -> str:
    """`tool_call`が実際にファイルを変更した(write_file/edit_file/move_file/
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

# 仮の判断(実機報告への対応): 堂々巡り検知(PROJECT_TOOL_LOOP_REPEAT_LIMIT)が
# 発火した際、ただ打ち切って「未完了のサブタスク」として人間に投げ返すだけでは、
# //resume-allで再開しても同じ堂々巡りを繰り返すだけで(実際に実機で
# 2回連続して同じ症状が再現した)、結局人間が手で介入するしかなくなる。
# 打ち切る前に一度だけ、モデル自身に「同じ呼び出しを繰り返していること」を
# 明示的に伝え、探索をやめて今ある情報で修正するか、できない理由を説明して
# 終えるよう促す。_NO_TOOL_CALL_NUDGE_MESSAGE(ツールを一度も呼ばなかった
# ケース)と対になる、逆方向(ツールを呼びすぎて前進しないケース)の言い直し
# 要求。
_PROJECT_TOOL_LOOP_NUDGE_MESSAGE = (
    "同じツール呼び出し({call_description})を、ファイルの変更を1件も挟まずに"
    "繰り返しています。これ以上同じ検索・確認を繰り返しても新しい情報は得られません。"
    "探すのをやめて、ここまでに得られた情報だけを根拠に、今すぐwrite_fileまたは"
    "edit_fileで実際の修正を行ってください。対象のファイル・箇所がまだ特定できて"
    "いない場合は、探索を続けるのではなく、何が分からないのか・なぜ対応できないのかを"
    "具体的に説明した上で作業を終えてください。"
)


def _collect_answer_with_project_tools(
    candidate: dict, org_fingerprint: str, messages: list, project_dir: str,
    print_lock: threading.Lock = None, tag: str = None,
    on_web_search: callable = None,
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

    仮の判断(`//agree`のリサーチフェーズへの対応): `on_web_search`
    (既定`None`)を渡すと、web_search(既定のCHAT_TOOLS、このプロセスでは
    実行されずモデル側のキッチンプロセス内で完結する)が呼ばれるたびに
    引数無しで呼び出す。呼び出し元がweb_searchの呼び出し回数を数えたい
    場合(例: リサーチ担当が実際に検索したかどうかの検知)に使う。
    既存の呼び出し元は渡さない(`None`のまま)ため、この引数の追加で
    既存の挙動は変わらない。

    仮の判断(応答切れによるファイル破壊のガード): あるラウンドの応答が
    CHAT_MAX_OUTPUT_TOKENS上限に達して途中で打ち切られていた
    (`pending_tool_calls`イベントの`truncated`)場合、そのラウンドに
    write_file呼び出しが含まれていても実行しない。write_fileのcontent
    引数(ファイル全体)が生成の途中で切れている可能性が高く、そのまま
    書き込むとファイルが壊れた状態で保存されてしまうため。read_file等の
    読み取り系ツールは(内容の生成ではなく既存ファイルの参照なので)
    通常どおり実行する。

    仮の判断(堂々巡り検知): MAX_PROJECT_TOOL_ROUNDSは回数さえ許せば
    いずれ抜けられる暴走への歯止めだが、「同じ検索・一覧確認だけを
    繰り返して一向に前進しない」停滞はラウンド数をいくら増やしても
    解消しない。そのため、ファイルの変更(`_MUTATING_PROJECT_TOOL_NAMES`)を
    1件も挟まずに同じツール名+同じ引数の呼び出しが
    `PROJECT_TOOL_LOOP_REPEAT_LIMIT`回連続したら、まず`_PROJECT_TOOL_
    LOOP_NUDGE_MESSAGE`で「繰り返しに気づいて探索をやめ、今すぐ修正するか
    できない理由を説明するように」1度だけ促す(`_NO_TOOL_CALL_NUDGE_
    MESSAGE`と対になる、ただ打ち切るだけでは`//resume-all`で再開しても
    同じ堂々巡りを繰り返すだけだった実機報告への対応)。促した後も同じ
    繰り返しが解消しなければ、その時点でラウンド上限を待たずに打ち切り、
    `error`にその旨を返す(呼び出し元は`error`を`MAX_PROJECT_TOOL_ROUNDS`
    到達時と同じ経路でそのまま表示・記録できる)。
    """
    from yoriai import _print_tagged, _stream_chat_from_candidate
    messages = list(messages)
    modified_files = []
    nudged = False
    loop_nudged = False
    repeat_counts = {}
    for _round_num in range(MAX_PROJECT_TOOL_ROUNDS):
        answer_parts = []
        pending_tool_calls = None
        pending_truncated = False
        error = None
        truncated = False
        for event in _stream_chat_from_candidate(candidate, org_fingerprint, messages, offer_project_tools=True):
            if "error" in event:
                error = event["error"]
                break
            if on_web_search is not None and event.get("tool_call") == WEB_SEARCH_TOOL_NAME:
                on_web_search()
            if "pending_tool_calls" in event:
                pending_tool_calls = event["pending_tool_calls"]
                pending_truncated = bool(event.get("truncated"))
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
        loop_trigger = None
        for tool_call in pending_tool_calls:
            function_name = tool_call.get("function", {}).get("name", "")
            if pending_truncated and function_name == WRITE_FILE_TOOL_NAME:
                result = json.dumps({
                    "ok": False,
                    "message": (
                        "応答が長さ制限で途中までしか届いていないため、この write_file は実行しませんでした。"
                        "ファイルが壊れるのを防ぐためです。edit_file で必要な箇所だけを直すか、"
                        "複数回に分けて書き込んでください。"
                    ),
                }, ensure_ascii=False)
            else:
                result = _execute_project_tool_call(project_dir, tool_call, print_lock, tag or candidate["label"])
            messages.append({"role": "tool", "content": result, "tool_call_id": tool_call.get("id")})
            mutated_filename = _mutated_filename_from_tool_result(tool_call, result)
            if mutated_filename:
                modified_files.append(mutated_filename)
                # 仮の判断: 実際にファイルの変更に成功した=前進があったので、
                # ここまでの「様子見の繰り返し」カウントは白紙に戻す。
                repeat_counts = {}
                continue
            _, call_arguments = _normalize_project_tool_call(tool_call)
            repeat_key = _project_tool_call_repeat_key(function_name, call_arguments)
            repeat_counts[repeat_key] = repeat_counts.get(repeat_key, 0) + 1
            if repeat_counts[repeat_key] >= PROJECT_TOOL_LOOP_REPEAT_LIMIT and loop_trigger is None:
                # 仮の判断: 1ラウンドに複数のツール呼び出しが含まれる場合でも、
                # 残りの呼び出しへの応答(role: tool)は普段どおり返しきる必要が
                # ある(会話履歴上、assistantのtool_callsに対応するtool応答が
                # 欠けると次回以降の問い合わせが壊れるため)。そのため検知しても
                # このforループ自体は抜けず、ラウンドの処理が終わってから対応する。
                loop_trigger = (function_name, call_arguments)

        if loop_trigger is not None:
            function_name, call_arguments = loop_trigger
            call_description = f"{function_name}: {json.dumps(call_arguments, ensure_ascii=False)}"
            if not loop_nudged:
                # 仮の判断(実機報告への対応): ただ打ち切って人間に投げ返す
                # だけでは、//resume-allで再開しても同じ堂々巡りを繰り返す
                # だけだった(実機で2回連続再現)。打ち切る前に一度だけ、
                # モデル自身に繰り返しを自覚させ、探索をやめて今ある情報で
                # 修正するか、できない理由を説明して終えるよう促す。
                loop_nudged = True
                repeat_counts = {}
                _print_tagged(
                    print_lock, tag or candidate["label"],
                    f"[⚠️ 同じツール呼び出し({call_description})の繰り返しを検知したため、"
                    "探索をやめて実際の修正に着手するよう促して再試行しています...]",
                )
                messages.append({
                    "role": "user",
                    "content": _PROJECT_TOOL_LOOP_NUDGE_MESSAGE.format(call_description=call_description),
                })
                continue
            return "", (
                f"同じツール呼び出し({call_description})が、ファイルの変更を1件も挟まずに"
                f"{PROJECT_TOOL_LOOP_REPEAT_LIMIT}回繰り返され、探索をやめて修正するよう1度促しても"
                f"改善しなかったため、{PROJECT_TOOL_LOOP_ERROR_MARKER}と判断してこの時点で打ち切りました"
            ), False, modified_files

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
    from yoriai import _check_file_syntax, _list_project_files
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
    from yoriai import (
        _classify_task,
        _collect_answer_from_candidate,
        _fetch_org_snapshot,
        _select_chat_candidates,
        _selection_reason_label,
    )
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
