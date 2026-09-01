"""Yoriaiのチャットのプロキシ: Ollama/LM Studio/MLX-LMへのストリーミング
問い合わせ、ツール呼び出し記法の漏れ検出、自己紹介カードの生成。

仮の判断(モジュール分割第三弾への対応): これまで`yoriai.py`単一ファイルに
実装されていたこの層を、依存が少なく独立性の高い塊としてここに切り出した。
関数のロジックは`yoriai.py`から一切変更していない(コピー＆import配線の
変更のみ)。

第一弾(tools.py)・第二弾(network.py)の時点で、ここに定義される関数
(`stream_chat_completion`・`build_profile_card`)を実際に呼んでいた
コード(`CardRequestHandler`)は既に`network.py`へ移動済みだったため、
`yoriai.py`側にはこのモジュールの関数を直接呼ぶコードがもう残っていない
(`network.py`が自身のメソッド内部での遅延importで`llm_stream`から直接
呼ぶ)。そのため`yoriai.py`はこのモジュールをトップレベルでimportしていない。

逆に、ここに定義された関数は、まだ`yoriai.py`に残っているシステム情報
取得系の関数(`get_ollama_installed_models`・`get_ollama_loaded_models`・
`get_lmstudio_models`・`get_mlx_lm_models`・`get_short_hostname`・
`get_chip_info`・`get_memory_info`・`_merge_model_lists`・
`_infer_specialties`・`_decide_num_ctx`)を呼ぶ必要があるが、それらの一部
(`get_ollama_loaded_models`・`get_mlx_lm_models`)は`yoriai.py`側の
`_decide_num_ctx`等からも使われる「システム情報取得系」としてyoriai.py
側に残す設計とした。`yoriai.py`が本モジュールをimportしていないため
循環importにはならないが、`yoriai.py`側の初期化順序に依存しないよう、
念のためこれらも実際に呼ばれる関数の内部で遅延importしている。
"""

import json
import logging
import os
import platform
import re
import time
import unicodedata
import uuid

import requests

from tools import WEB_SEARCH_TOOL_SCHEMA, _execute_tool_call, _looks_like_tools_related_error
from yoriai_types import (
    CHAT_CONNECT_TIMEOUT_SEC,
    CHAT_MAX_OUTPUT_TOKENS,
    CHAT_READ_TIMEOUT_SEC,
    LMSTUDIO_BASE_URL,
    MLX_LM_BASE_URL,
    OLLAMA_BASE_URL,
)

logger = logging.getLogger("yoriai")

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


def _estimate_tokens(messages: list) -> int:
    """`messages`に含まれる全文字列から、雑にトークン数を概算する。

    仮の判断: 厳密なトークナイザ(モデルごとに異なる)を新たな依存として
    requirements.txtに追加するのは避けたい。目的は「切り捨てが起きて
    いることに人間が気づけるようにする」ことであり、桁が合っていれば
    十分なため、日本語などの非ASCII文字は1文字≒1トークン、ASCII文字は
    4文字≒1トークンとして数える簡易な概算にとどめる。
    """
    total_chars_ascii = 0
    total_chars_non_ascii = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            continue
        for ch in content:
            if ord(ch) < 128:
                total_chars_ascii += 1
            else:
                total_chars_non_ascii += 1
    return (total_chars_ascii // 4) + total_chars_non_ascii


# 仮の判断: num_ctxの何割を超えたら警告するかの閾値。厳密な根拠は無く、
# 「まだ余裕があるうちは黙っておき、実際に切り捨てが起きていそうな
# 水準に近づいたら知らせる」というバランスで8割とした。
_NUM_CTX_WARNING_RATIO = 0.8

# 仮の判断: Ollamaの既定のkeep_alive(モデルをメモリに保持する時間)は
# 5分で、それを過ぎるとモデルがアンロードされる。Yoriaiの協業モードは
# 合意・実装・レビュー・修正のフェーズ間で人間の入力待ちが挟まったり、
# 大規模なタスクキューの処理に数時間かかったりするため、フェーズの
# 合間にモデルの再ロード(実機で数十秒〜)が毎回発生してしまう。そこで
# 既定より長い30分をYoriai全体の既定値とする。環境変数
# YORIAI_OLLAMA_KEEP_ALIVEで上書きできるようにする(SEARXNG_BASE_URLと
# 同じ流儀)。
KEEP_ALIVE = os.environ.get("YORIAI_OLLAMA_KEEP_ALIVE", "30m")


def _stream_ollama_turn(model: str, messages: list, tools: list):
    """OllamaのネイティブAPI(/api/chat、NDJSONストリーミング)に1往復だけ
    問い合わせ、正規化したイベント({"content": ...} / {"error": ...})を
    順にyieldし、最後にそのターンで要求されたtool_calls一覧と、応答が
    CHAT_MAX_OUTPUT_TOKENS上限に達して打ち切られたかどうか
    ({"tool_calls": [...], "truncated": bool})をyieldする。

    仮の判断: options.num_ctx(コンテキストウィンドウの長さ)とkeep_alive
    (モデルをメモリに保持する時間)を明示的に指定する(詳細はMAX_NUM_CTX・
    KEEP_ALIVE定義部のコメントを参照)。num_ctxが決定できなかった場合
    (_decide_num_ctxがNoneを返した場合)はキー自体を送らず、Ollama側の
    デフォルト挙動に委ねる(従来の挙動のまま)。
    """
    from yoriai import _decide_num_ctx
    num_ctx = _decide_num_ctx(model)
    options = {"num_predict": CHAT_MAX_OUTPUT_TOKENS}
    if num_ctx is not None:
        options["num_ctx"] = num_ctx
        estimated_tokens = _estimate_tokens(messages)
        if estimated_tokens > num_ctx * _NUM_CTX_WARNING_RATIO:
            logger.warning(
                "モデル '%s' への問い合わせが概算%dトークンで、num_ctx(%d)の%d%%を"
                "超えています。入力がコンテキスト長を超えると、Ollamaは先頭から"
                "黙って切り捨てます。",
                model, estimated_tokens, num_ctx, int(_NUM_CTX_WARNING_RATIO * 100),
            )
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": model, "messages": messages, "tools": tools, "stream": True,
                "options": options, "keep_alive": KEEP_ALIVE,
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


_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _longest_tag_prefix_overlap(buffer: str, tag: str) -> int:
    """`buffer`の末尾が`tag`の先頭部分と一致している場合、その長さを返す
    (一致していなければ0)。`tag`自体が丸ごと`buffer`に含まれるケースは
    ここでは扱わない(呼び出し元が先に`buffer.find(tag)`で確認済みの
    前提)。開始/終了タグそのものが複数チャンクに分割されて届く場合
    (例: 1チャンク目が"<th"、2チャンク目が"ink>")に、タグの途中までを
    誤って通常のcontent/thinkingとして確定させてしまわないようにするために使う。
    """
    max_len = min(len(buffer), len(tag) - 1)
    for length in range(max_len, 0, -1):
        if buffer.endswith(tag[:length]):
            return length
    return 0


class _ThinkTagSplitter:
    """delta.contentに<think>...</think>タグの形で混在する思考過程を、
    タグが複数チャンクに分割されて届く場合であっても正しく検出し、
    タグの内側を{"thinking": ...}、外側を{"content": ...}としてyieldする
    ステートマシン。

    仮の判断: `_run_turn_with_leak_detection`の先頭ピークバッファと同じ
    考え方で、開始/終了タグの途中までしか届いていない末尾はバッファに
    保持し、タグ全体が確定するか、ストリーム終了(`flush()`)まで確定を
    保留する。<think>タグが一切現れない応答では、バッファの末尾が
    "<"を含まない限りこの保留は発生しないため、既存の挙動(chunkが
    届いたその場でそのまま{"content": ...}としてyieldする)は実質的に
    変わらない。
    """

    def __init__(self):
        self._inside = False
        self._buffer = ""

    def feed(self, text: str):
        if not text:
            return
        self._buffer += text
        yield from self._drain(final=False)

    def flush(self):
        yield from self._drain(final=True)

    def _drain(self, final: bool):
        while True:
            tag = _THINK_CLOSE_TAG if self._inside else _THINK_OPEN_TAG
            event_key = "thinking" if self._inside else "content"
            idx = self._buffer.find(tag)
            if idx != -1:
                before = self._buffer[:idx]
                if before:
                    yield {event_key: before}
                self._buffer = self._buffer[idx + len(tag):]
                self._inside = not self._inside
                continue

            if final:
                if self._buffer:
                    yield {event_key: self._buffer}
                    self._buffer = ""
                return

            overlap = _longest_tag_prefix_overlap(self._buffer, tag)
            emit_len = len(self._buffer) - overlap
            if emit_len > 0:
                yield {event_key: self._buffer[:emit_len]}
                self._buffer = self._buffer[emit_len:]
            return


def _stream_openai_compatible_turn(base_url: str, model: str, messages: list, tools: list):
    """OpenAI互換のstreaming chat completions API(/v1/chat/completions、SSE)に
    1往復だけ問い合わせ、正規化イベントを順にyieldする。LM Studio・MLX-LMは
    どちらもこの同じワイヤ形式を話すため、共通の実装として1箇所にまとめている。

    仮の判断: OpenAI互換のストリーミングではtool_callsが複数チャンクに
    分割されて送られてくる(引数のJSON文字列が少しずつ届く)ため、
    indexごとに文字列を連結して組み立てる。

    仮の判断: Ollamaのようにここではnum_ctx相当のオプションを指定して
    いない。LM Studio・MLX-LMはどちらもコンテキスト長をモデルの
    ロード時設定(LM StudioのUIでの「Context Length」設定、
    `mlx_lm.server`の起動オプション)としてサーバー側で固定しており、
    OpenAI互換のchat completions APIのリクエストボディにその場で
    指定できるパラメータが無い(`max_tokens`はあくまで出力側の上限で、
    入力側のコンテキスト長とは別物)。そのため、この2バックエンドに
    関しては「モデルロード時に十分な長さを設定しておいてもらう」ことが
    前提になり、Yoriai側からリクエスト単位で制御する手段は無い。

    仮の判断(思考モード対応モデルの応答が見えない問題への対応): 思考
    モード対応モデル(Qwen3.5-Flash-Next等)の思考過程は、(1)LM Studio側の
    設定で有効化されていれば独立したdelta.reasoning_contentとして、
    (2)そうでなければdelta.contentに<think>...</think>タグとして混在した
    形で、それぞれチャンクごとに届く。前者はそのまま{"thinking": ...}として
    yieldし、後者は`_ThinkTagSplitter`でタグの外側/内側を分離して
    {"content": ...}/{"thinking": ...}としてyieldする(タグ自体が複数
    チャンクに分割されて届く場合にも対応するため、ステートフルな
    パーサーとして1ターンにつき1つだけ生成する)。どちらの形式も一切
    現れない応答では、`_ThinkTagSplitter`は実質的に何もせずcontentを
    そのまま素通しするため、既存の挙動は変わらない。reasoning_effortの
    ようなパラメータでの思考の深さの制御、および最初の1トークンが
    出るまでのprefill待ち時間そのものへの対策は、このPRのスコープ外
    として別途扱う。
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
    think_splitter = _ThinkTagSplitter()
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
            reasoning_content = delta.get("reasoning_content")
            if reasoning_content:
                yield {"thinking": reasoning_content}
            content = delta.get("content")
            if content:
                for think_event in think_splitter.feed(content):
                    yield think_event
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
    for think_event in think_splitter.flush():
        yield think_event

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


def _run_turn_with_leak_detection(turn):
    """1ターン分の応答イベントを中継しつつ、先頭のcontentが漏れたツール
    呼び出し記法でないかを確認する。漏れを検出した場合は、それ以降の
    contentを画面に出さずに捨て、代わりに{"tool_call_failed": True} を
    1回だけyieldする(content/tool_callsのどちらも実質的には返さない)。

    仮の判断: 判定のためにcontentの先頭を少量バッファする間も、元のチャンクの
    区切り(トークン単位)は保ったままリプレイする。1つの大きな塊に結合して
    出すと、バッファ分だけ「一括表示」に戻ってしまい、ストリーミング表示という
    フェーズ5の目的が損なわれるため。

    仮の判断(バグ報告への対応): 当初は`tools`(このラウンドで実際に
    オファーしたツール一覧)が空の場合は漏れチェック自体を省略していた
    (ツールをオファーしていないのだから漏れようがない、という前提)。
    しかし実機で、`stream_chat_completion`のtools無し最終問い合わせ
    (FINAL_NO_TOOL_ROUND、会話履歴には直前までのツール呼び出しの
    やり取りが残ったまま`tools=None`で問い合わせる)において、モデルが
    それでも`<tool_call>`記法をそのまま出力してしまい、このラウンドだけ
    チェックがスキップされていたために生のツール呼び出し記法が回答本文
    としてそのままユーザーに表示されてしまう不具合が見つかった。会話
    履歴にツール呼び出しの前例が残っている限り、そのラウンド自体が
    ツール無しでもモデルが記法を漏らす可能性は消えないため、`tools`の
    有無に関わらず常に漏れチェックを行うようにした。
    """
    state = "peeking"
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


def stream_chat_completion(
    model: str, messages: list, extra_tools: list = None, client_tool_names: set = None,
    disable_default_tools: bool = False,
):
    """モデル名からOllama/LM Studio/MLX-LMのどれにチャットを振るかを決め、
    正規化されたストリーミングイベント({"content": ...} / {"thinking": ...} /
    {"tool_call": <ツール名>} / {"pending_tool_calls": [...], "truncated": bool} /
    {"done": True, "truncated": bool} / {"error": ...})を順にyieldする。
    `truncated`は、それぞれそのラウンドの応答がCHAT_MAX_OUTPUT_TOKENS上限に
    達して途中で打ち切られたかどうかを表す。

    仮の判断: {"thinking": ...}は、LM Studio/MLX-LM(OpenAI互換API)経由の
    問い合わせで、思考モード対応モデルの思考過程(delta.reasoning_content、
    または<think>タグ)が検出された場合にのみ発生する
    (`_stream_openai_compatible_turn`を参照)。Ollama経由の問い合わせでは
    現時点では発生しない(スコープ外)。

    モデルがツール(既定ではweb_searchのみ)の呼び出しを要求した場合は、
    ここでツールを実行して結果を会話履歴に追加し、モデルに再度問い合わせる
    (最大MAX_TOOL_CALL_ROUNDSラウンドまで)。呼び出し元(REPL等)からは
    ツールの存在を意識せず、通常のチャットと同じように使える。

    `extra_tools`(協業モードのレビュー専用read_file等)を渡すと、常時
    有効なCHAT_TOOLSに加えてこのリクエストだけ追加のツールをオファーする。

    仮の判断(不具合修正: 対話プロトコル一時停止後、実装フェーズに繋がらない
    問題への対応): `disable_default_tools=True`を渡すと、このリクエスト
    限定でCHAT_TOOLS(既定ではweb_searchのみ)をオファーしない
    (`extra_tools`が指定されていればそちらは引き続きオファーする)。
    合意フェーズの一時停止直後の単発質問フォールバック(`_ask_organization`)
    のように、スコープ確定・補足情報の伝達が主目的で外部情報の裏付けが
    不要な場面で、モデル(特に思考系モデル)がweb_searchを繰り返し要求して
    MAX_TOOL_CALL_ROUNDSに達し空の応答で打ち切られる現象を避けるために使う。

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

    仮の判断: 最終ラウンド(round_num == MAX_TOOL_CALL_ROUNDS)でモデルが
    content(回答本文)を1文字も生成せず、ツール呼び出しのみを要求してきた
    場合、そのまま空の応答で打ち切ると呼び出し元(_ask_organization等)が
    「有効な応答が得られなかった」と誤判定し、次の候補モデルへの
    フォールバックが発生してしまう(実機で、裏付けを取ろうとする思考系
    モデルがweb_searchを立て続けに要求するケースで確認済み)。これを
    避けるため、この場合に限り「これ以上ツールは使わない」旨の指示を
    会話履歴に1回だけ追加したうえで、tools無しでもう1往復だけ問い合わせ、
    その結果を最終回答として採用する。この最終問い合わせでもcontentが
    空だった場合は、従来通り空のまま終了する(無限ループにはしない)。
    """
    from yoriai import get_mlx_lm_models, get_ollama_loaded_models
    messages = list(messages)  # 呼び出し元のリストをツール実行の追記で汚さない
    base_tools = [] if disable_default_tools else CHAT_TOOLS
    tools = base_tools + list(extra_tools) if extra_tools else base_tools

    # 通常のツール呼び出しラウンドは0..MAX_TOOL_CALL_ROUNDSのMAX_TOOL_CALL_ROUNDS+1回。
    # それに加えて、最終ラウンドがcontent無しのツール要求のみで終わった場合だけ
    # 到達する「tools無し最終問い合わせ」を1ラウンドだけ確保する(必要なければ
    # 到達せずに従来通り終了する。下の`round_num == FINAL_NO_TOOL_ROUND`の分岐を参照)。
    FINAL_NO_TOOL_ROUND = MAX_TOOL_CALL_ROUNDS + 1
    need_final_no_tool_retry = False

    for round_num in range(FINAL_NO_TOOL_ROUND + 1):
        if round_num == FINAL_NO_TOOL_ROUND:
            if not need_final_no_tool_retry:
                break
            tools = None
            messages.append({
                "role": "system",
                "content": "これ以上ツールは使わず、これまでに分かっている情報だけで回答してください。",
            })

        if model in get_ollama_loaded_models():
            turn = _stream_ollama_turn(model, messages, tools)
        elif model in get_mlx_lm_models():
            turn = _stream_mlx_lm_turn(model, messages, tools)
        else:
            turn = _stream_lmstudio_turn(model, messages, tools)

        tool_calls = []
        round_truncated = False
        round_content_yielded = False
        leaked = False
        tools_rejected = False
        for event in _run_turn_with_leak_detection(turn):
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
                if event["content"]:
                    round_content_yielded = True
                yield event
            elif "thinking" in event:
                yield event
            elif "tool_calls" in event:
                tool_calls = event["tool_calls"]
                round_truncated = event.get("truncated", False)

        if (leaked or tools_rejected) and tools:
            tools = None  # このモデルにはツールを二度とオファーせず、素の会話として再試行する
            continue

        if not tool_calls:
            break

        if round_num == MAX_TOOL_CALL_ROUNDS:
            if tools and not round_content_yielded:
                # 最終ラウンドがcontent無しのツール要求のみだった場合に限り、
                # 次のFINAL_NO_TOOL_ROUNDへ進んでtools無しの最終問い合わせを行う。
                need_final_no_tool_retry = True
                continue
            break

        # 仮の判断: このラウンドのtool_callsに1つでもclient_tool_names該当
        # (read_file等)が含まれる場合、ここでは一切実行せずラウンド全体を
        # 呼び出し元に渡して終了する。1ラウンドでweb_searchとread_fileが
        # 混在するようなケースの部分実行は複雑さの割に実利が薄いため扱わず、
        # 呼び出し元側にラウンド全体の実行を委ねる単純な設計にした。
        #
        # 仮の判断(応答切れによるファイル破壊のガードへの対応): `round_truncated`
        # (このラウンドの応答がCHAT_MAX_OUTPUT_TOKENS上限に達して途中で
        # 打ち切られたかどうか)もあわせて渡す。tool_callsのJSON自体は
        # (ツール呼び出しの構造化出力なので)途中で切れていても不完全な
        # まま解析されうるが、その中身(特にwrite_fileのcontent引数)が
        # 途中で切れている可能性があるかどうかは、呼び出し元
        # (`_collect_answer_with_project_tools`)がここで初めて判断できる。
        if client_tool_names and any(
            tool_call.get("function", {}).get("name", "") in client_tool_names for tool_call in tool_calls
        ):
            yield {"pending_tool_calls": tool_calls, "truncated": round_truncated}
            return

        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        for tool_call in tool_calls:
            tool_name = tool_call.get("function", {}).get("name", "")
            yield {"tool_call": tool_name, "tool_call_arguments": tool_call.get("function", {}).get("arguments", "")}
            result = _execute_tool_call(tool_call)
            # 仮の判断(不具合報告への対応: ツール呼び出しの結果が画面に一切
            # 表示されず、何を検索して何が返ってきたのか分からないという
            # 指摘): 呼び出し元がツールの実行結果を画面に表示できるよう、
            # モデルへ返す結果(result)をそのままイベントとしても流す。
            # モデルへの実際の入力とは別物として扱えるよう、結果の中身
            # (JSON文字列)は"tool_result_content"というキーに入れて返す。
            yield {"tool_result": tool_name, "tool_result_content": result}
            messages.append({
                "role": "tool",
                "content": result,
                "tool_call_id": tool_call.get("id"),
            })

    yield {"done": True, "truncated": round_truncated}


def build_profile_card(agent_id: str) -> dict:
    from yoriai import (
        _infer_specialties,
        _merge_model_lists,
        get_chip_info,
        get_lmstudio_models,
        get_memory_info,
        get_mlx_lm_models,
        get_ollama_context_length,
        get_ollama_installed_models,
        get_ollama_loaded_models,
        get_short_hostname,
    )
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
            # 仮の判断: get_ollama_context_length()はキャッシュされるとはいえ、
            # 未取得のモデルは/api/showへの実HTTP問い合わせが発生する。
            # カード生成のたびにインストール済み全モデル分を問い合わせると、
            # モデル数が多い環境でカード生成(ハートビート応答にも使われる)が
            # 遅くなりかねないため、実際にロード済みのOllamaモデルだけに
            # 限定する(インストール済みだが未ロードのモデルは、どのみち
            # チャットの相手として選ばれるまでは表示する意味が薄い)。
            "context_lengths": {
                m: get_ollama_context_length(m) for m in ollama_loaded
            },
        },
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
