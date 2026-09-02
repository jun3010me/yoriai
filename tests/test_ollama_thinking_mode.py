#!/usr/bin/env python3
"""Ollama経由の思考モード対応モデル(GLM-4.7-flash・Kimi K2.5等)の思考過程が
Yoriaiの対話プロトコルに一切表示されない不具合への対応を検証するテスト。

背景: `_stream_ollama_turn`はこれまでリクエストに`"think"`パラメータを
一切含めておらず、レスポンスの`message.thinking`フィールドも見ていな
かった。そのため、これらのモデルがどれだけ思考していても画面には
一切表示されなかった(llama3.3:70bのように元々思考モードを持たない
モデルは、この対応をしても表示が増えないのが正しい挙動)。

ここでは(1)リクエストのトップレベルに`"think": True`が含まれること、
(2)`message.thinking`フィールドから{"thinking": ...}イベントが正しく
yieldされること、(3)LM Studio対応時に作った`_ThinkTagSplitter`を
Ollama側でも再利用し、`message.content`に混在する<think>タグが
複数チャンクにまたがる場合も正しく分割されること、(4)`think`無し
リクエストが拒否された場合に`"think"`キー無しで1回だけ再試行する
フォールバックが機能すること、(5)思考過程が一切無い通常の応答では
従来通り{"content": ...}イベントのみが発生することを確認する。

実機のOllamaに接続できない環境のため、`requests.post`を差し替えて模擬する
(tests/test_response_truncation.py・tests/test_num_ctx.pyと同じ方式)。

使い方: python3 -m pytest tests/test_ollama_thinking_mode.py
        python3 tests/test_ollama_thinking_mode.py
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

    def __init__(self, lines, ok=True, status_code=200):
        self.ok = ok
        self.status_code = status_code
        self._lines = lines
        self.text = ""

    def raise_for_status(self):
        pass

    def json(self):
        return {}

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line


def _run_stream(model, message_dicts):
    """message辞書のリストからNDJSON行を組み立てて`_stream_ollama_turn`を
    実行し、yieldされたイベントのリストを返す。"""
    lines = [_json.dumps({"message": m, "done": False}) for m in message_dicts]
    lines.append(_json.dumps({"message": {}, "done": True, "done_reason": "stop"}))

    def fake_post(url, json=None, stream=None, timeout=None):
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        return list(llm_stream._stream_ollama_turn(model, [{"role": "user", "content": "こんにちは"}], []))
    finally:
        yoriai.requests.post = original_post


# ---------------------------------------------------------------------------
# リクエストのトップレベルにthink: Trueが含まれること
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_sends_think_true_in_request_payload():
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["json"] = json
        lines = [_json.dumps({"message": {"content": "OK"}, "done": True, "done_reason": "stop"})]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        list(llm_stream._stream_ollama_turn("glm-4.7-flash", [{"role": "user", "content": "こんにちは"}], []))
    finally:
        yoriai.requests.post = original_post

    assert captured["json"]["think"] is True, captured["json"]
    # optionsの中ではなく、トップレベル(model/messages/streamと同じ階層)にあること。
    assert "think" not in captured["json"].get("options", {}), captured["json"]


# ---------------------------------------------------------------------------
# message.thinkingフィールドからのthinkingイベント
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_yields_thinking_event_from_message_thinking_field():
    events = _run_stream("glm-4.7-flash", [
        {"thinking": "まず", "content": ""},
        {"thinking": "ユーザーの意図を考える", "content": ""},
        {"thinking": "", "content": "こんにちは!"},
    ])

    thinking_events = [e for e in events if "thinking" in e]
    content_events = [e for e in events if "content" in e]

    assert [e["thinking"] for e in thinking_events] == ["まず", "ユーザーの意図を考える"], thinking_events
    assert "".join(e["content"] for e in content_events) == "こんにちは!", content_events


def test_stream_ollama_turn_yields_both_thinking_and_content_from_same_line():
    """1行に`message.thinking`と`message.content`の両方が含まれる場合、
    両方が独立してyieldされることを確認する。"""
    events = _run_stream("kimi-k2.5", [
        {"thinking": "考え中", "content": "回答の一部"},
    ])

    assert {"thinking": "考え中"} in events, events
    assert {"content": "回答の一部"} in events, events


# ---------------------------------------------------------------------------
# message.contentに混在する<think>タグの、チャンクをまたぐ分割
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_splits_think_tags_in_content_across_chunks():
    events = _run_stream("some-model-with-think-tags", [
        {"content": "<think>思考"},
        {"content": "の続き</think>回答"},
    ])

    thinking_text = "".join(e["thinking"] for e in events if "thinking" in e)
    content_text = "".join(e["content"] for e in events if "content" in e)

    assert thinking_text == "思考の続き", events
    assert content_text == "回答", events


def test_stream_ollama_turn_flushes_unterminated_think_tag_at_stream_end():
    """閉じタグが来る前にストリームが終了した場合でも、flush()相当の処理で
    バッファに残っている内容が取りこぼされずthinkingとして出力されることを
    確認する。"""
    events = _run_stream("some-model", [
        {"content": "<think>まだ考え中"},
    ])

    assert events[0] == {"thinking": "まだ考え中"}, events


# ---------------------------------------------------------------------------
# 思考過程が一切無い通常の応答(llama3.3:70b等、思考モード非対応モデルを想定)
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_unaffected_when_no_thinking_present():
    events = _run_stream("llama3.3:70b", [
        {"content": "こんにちは、"},
        {"content": "今日はいい天気ですね。"},
    ])

    assert not any("thinking" in e for e in events), events
    content_text = "".join(e["content"] for e in events if "content" in e)
    assert content_text == "こんにちは、今日はいい天気ですね。", content_text


# ---------------------------------------------------------------------------
# think非対応モデルがtrue付きリクエストを拒否した場合のフォールバック再試行
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_retries_without_think_key_when_think_request_fails():
    """`"think": true`を含むリクエストが失敗(400)した場合、そのモデルへの
    問い合わせ自体を失敗させないよう、`"think"`キー無しで1回だけ自動的に
    再試行することを確認する。"""
    calls = []

    def fake_post(url, json=None, stream=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _FakeResponse([], ok=False, status_code=400)
        lines = [_json.dumps({"message": {"content": "OK"}, "done": True, "done_reason": "stop"})]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_ollama_turn("llama3.3:70b", [{"role": "user", "content": "hi"}], []))
    finally:
        yoriai.requests.post = original_post

    assert len(calls) == 2, calls
    assert calls[0]["think"] is True, calls[0]
    assert "think" not in calls[1], calls[1]
    content_text = "".join(e["content"] for e in events if "content" in e)
    assert content_text == "OK", events
    assert not any("error" in e for e in events), events


def test_stream_ollama_turn_reports_error_when_both_think_and_fallback_requests_fail():
    """`"think"`キー無しで再試行しても失敗する場合は、従来通りエラー
    イベントとして扱われることを確認する(無限リトライにはならない)。"""
    calls = []

    def fake_post(url, json=None, stream=None, timeout=None):
        calls.append(json)
        return _FakeResponse([], ok=False, status_code=500)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_ollama_turn("some-model", [{"role": "user", "content": "hi"}], []))
    finally:
        yoriai.requests.post = original_post

    assert len(calls) == 2, calls
    assert any("error" in e for e in events), events


def main():
    tests = [
        test_stream_ollama_turn_sends_think_true_in_request_payload,
        test_stream_ollama_turn_yields_thinking_event_from_message_thinking_field,
        test_stream_ollama_turn_yields_both_thinking_and_content_from_same_line,
        test_stream_ollama_turn_splits_think_tags_in_content_across_chunks,
        test_stream_ollama_turn_flushes_unterminated_think_tag_at_stream_end,
        test_stream_ollama_turn_unaffected_when_no_thinking_present,
        test_stream_ollama_turn_retries_without_think_key_when_think_request_fails,
        test_stream_ollama_turn_reports_error_when_both_think_and_fallback_requests_fail,
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
