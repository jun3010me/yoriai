#!/usr/bin/env python3
"""思考モード対応モデル(Qwen3.5-Flash-Next等)の思考過程(reasoning)を
ストリーミングのdelta単位でリアルタイムに検出する機能を検証するテスト。

大規模な//agreeセッションでは議事録全文が累積プロンプトに積まれ、モデルが
思考している間コンソールに何も表示されず、実際には正常に動作している
(タイムアウトもしていない)にもかかわらず「応答が返ってこない・固まって
いる」ように見えてしまう問題があった。LM Studio/MLX-LM(OpenAI互換の
streaming chat completions API)は、思考過程を(1)delta.reasoning_content
として独立に、または(2)reasoning_contentが無い場合はdelta.contentに
<think>...</think>タグとして混在させて、チャンクごとに返してくる。
`llm_stream._stream_openai_compatible_turn`がこれをdelta単位で検出し、
{"thinking": ...}イベントとしてyieldできることを確認する。

pytestからも`python3 tests/test_reasoning_extraction.py`単体実行からも
使えるよう、既存のテスト(tests/test_response_truncation.py等)と同じ
スタイル(素のassert・_FakeResponseによるrequests.postの差し替え)で書く。

使い方: python3 -m pytest tests/test_reasoning_extraction.py
        python3 tests/test_reasoning_extraction.py
"""
import json as _json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


class _FakeResponse:
    """`requests.post(..., stream=True)`の戻り値を模擬する最小限のスタブ
    (tests/test_response_truncation.pyと同じもの)。"""

    def __init__(self, lines):
        self.ok = True
        self.status_code = 200
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line


def _sse_lines(delta_dicts):
    """delta辞書のリストからSSE形式の行リストを組み立てる。"""
    lines = ["data: " + _json.dumps({"choices": [{"delta": d}]}) for d in delta_dicts]
    lines.append("data: [DONE]")
    return lines


def _run_stream(delta_dicts):
    def fake_post(url, json=None, stream=None, timeout=None):
        return _FakeResponse(_sse_lines(delta_dicts))

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        return list(llm_stream._stream_openai_compatible_turn(
            yoriai.LMSTUDIO_BASE_URL, "qwen3.5-flash-next", [{"role": "user", "content": "こんにちは"}], [],
        ))
    finally:
        yoriai.requests.post = original_post


# ---------------------------------------------------------------------------
# パターン1: delta.reasoning_contentが複数チャンクに分かれて届く場合
# ---------------------------------------------------------------------------

def test_reasoning_content_split_across_multiple_chunks_yields_thinking_events():
    events = _run_stream([
        {"reasoning_content": "まず"},
        {"reasoning_content": "ユーザーの意図を考える"},
        {"content": "こんにちは!"},
    ])

    thinking_events = [e for e in events if "thinking" in e]
    content_events = [e for e in events if "content" in e]

    assert [e["thinking"] for e in thinking_events] == ["まず", "ユーザーの意図を考える"], thinking_events
    assert "".join(e["content"] for e in content_events) == "こんにちは!", content_events


# ---------------------------------------------------------------------------
# パターン2: <think>タグの開始・終了・内容がすべて1チャンクに収まっている場合
# ---------------------------------------------------------------------------

def test_think_tag_fully_contained_in_single_chunk():
    events = _run_stream([
        {"content": "<think>とりあえず考える</think>こんにちは!"},
    ])

    thinking_text = "".join(e["thinking"] for e in events if "thinking" in e)
    content_text = "".join(e["content"] for e in events if "content" in e)

    assert thinking_text == "とりあえず考える", events
    assert content_text == "こんにちは!", events
    # 既存の{"tool_calls": ..., "truncated": ...}以外に、意図しないキーの
    # イベントが混ざっていないことも確認する。
    for event in events:
        assert set(event.keys()) <= {"thinking", "content", "tool_calls", "truncated"}, event


# ---------------------------------------------------------------------------
# パターン3: <think>タグの開始タグと終了タグが別々のチャンクに分かれている場合
# ---------------------------------------------------------------------------

def test_think_tag_open_and_close_split_across_chunks():
    events = _run_stream([
        {"content": "<think>思考"},
        {"content": "の続き</think>回答"},
    ])

    thinking_text = "".join(e["thinking"] for e in events if "thinking" in e)
    content_text = "".join(e["content"] for e in events if "content" in e)

    assert thinking_text == "思考の続き", events
    assert content_text == "回答", events


def test_think_tag_itself_split_mid_tag_across_chunks():
    """タグそのもの(<think>という文字列自体)が2チャンクにまたがって届く
    さらに極端なケース(例: 1チャンク目が"<th"、2チャンク目が"ink>")でも、
    タグとして正しく認識され、外側に漏れないことを確認する。
    """
    events = _run_stream([
        {"content": "前置き<th"},
        {"content": "ink>内緒の考え</th"},
        {"content": "ink>本題"},
    ])

    thinking_text = "".join(e["thinking"] for e in events if "thinking" in e)
    content_text = "".join(e["content"] for e in events if "content" in e)

    assert thinking_text == "内緒の考え", events
    assert content_text == "前置き本題", events


# ---------------------------------------------------------------------------
# パターン4: reasoning_contentも<think>タグも無い、通常のcontentのみの場合
# ---------------------------------------------------------------------------

def test_plain_content_without_reasoning_never_yields_thinking_event():
    events = _run_stream([
        {"content": "こんにちは、"},
        {"content": "今日はいい天気ですね。"},
    ])

    assert not any("thinking" in e for e in events), events
    content_text = "".join(e["content"] for e in events if "content" in e)
    assert content_text == "こんにちは、今日はいい天気ですね。", events


def test_plain_content_with_stray_angle_bracket_is_not_mistaken_for_think_tag():
    """<think>と無関係な"<"を含むcontent(例: "1 < 2"のような比較演算子)が、
    誤って思考過程として握りつぶされたり、タグ判定のバッファ待ちのまま
    最後まで出力されなかったりしないことを確認する。
    """
    events = _run_stream([
        {"content": "1 < 2 は真です"},
    ])

    assert not any("thinking" in e for e in events), events
    content_text = "".join(e["content"] for e in events if "content" in e)
    assert content_text == "1 < 2 は真です", events


# ---------------------------------------------------------------------------
# _ThinkTagSplitter単体: ストリーミング終了時のflush()の挙動
# ---------------------------------------------------------------------------

def test_think_tag_splitter_flush_emits_unterminated_think_block_as_thinking():
    """閉じタグが来る前にストリームが終了した場合(応答が打ち切られた等)、
    flush()時点でバッファに残っている内容はthinkingとして扱われることを
    確認する(開いたままの<think>の中身を、誤って回答本文として扱わない
    ようにするための挙動)。
    """
    splitter = llm_stream._ThinkTagSplitter()
    events = list(splitter.feed("<think>まだ考え中")) + list(splitter.flush())
    assert events == [{"thinking": "まだ考え中"}], events


def main():
    tests = [
        test_reasoning_content_split_across_multiple_chunks_yields_thinking_events,
        test_think_tag_fully_contained_in_single_chunk,
        test_think_tag_open_and_close_split_across_chunks,
        test_think_tag_itself_split_mid_tag_across_chunks,
        test_plain_content_without_reasoning_never_yields_thinking_event,
        test_plain_content_with_stray_angle_bracket_is_not_mistaken_for_think_tag,
        test_think_tag_splitter_flush_emits_unterminated_think_block_as_thinking,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        else:
            print(f"OK:   {test.__name__}")

    if failures:
        print(f"\n{failures}件のテストが失敗しました。")
        sys.exit(1)
    print("\nすべてのテストが成功しました。")


if __name__ == "__main__":
    main()
