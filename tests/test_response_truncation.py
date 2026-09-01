#!/usr/bin/env python3
"""バグ報告への対応を検証する: 複雑な修正指摘を受けたモデルの応答が、
CHAT_MAX_OUTPUT_TOKENS(依頼のmax_tokens/num_predict)の上限が無いために
際限なく増え続け、生成が終わらなくなる不具合(index.htmlの修正依頼で
13,819トークンから20,000超まで増え続け、手動でのCtrl+C×2による強制終了が
必要になった実機の報告)への対応を検証する。

(1)Ollama(options.num_predict)・LM Studio/MLX-LM(OpenAI互換API、
max_tokens)への問い合わせに、実際に上限が付与されること、(2)応答が
その上限で打ち切られたこと(Ollamaのdone_reason、OpenAI互換APIの
finish_reasonがどちらも"length")を検知し、`stream_chat_completion`の
最終`done`イベントまで`truncated`として伝わること、(3)修正依頼
(`_request_fix`)がこの打ち切りを検知した場合、通常の問い合わせ失敗と
同様に扱って(暴走・再試行ループに陥らず)Noneを返し、既存の「最大2回の
レビュー」構造の中でその回の修正を不採用として次に進む(未完了として
報告する)ことを確認する。

実機のOllama/LM Studio/MLX-LMに接続できない環境のため、`requests.post`
(ワイヤレベル)または`_stream_chat_from_candidate`(呼び出し元レベル)を
差し替えてバックエンドを模擬する。

使い方: python3 tests/test_response_truncation.py
"""
import contextlib
import io
import json as _json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


class _FakeResponse:
    """`requests.post(..., stream=True)`の戻り値を模擬する最小限のスタブ。"""

    def __init__(self, lines):
        self.ok = True
        self.status_code = 200
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


# ---------------------------------------------------------------------------
# ワイヤレベル: Ollama・OpenAI互換API(LM Studio/MLX-LM)への上限付与と検知
# ---------------------------------------------------------------------------

def test_stream_ollama_turn_sends_num_predict_and_detects_length_truncation():
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        lines = [
            _json.dumps({"message": {"content": "こんにちは"}, "done": False}),
            _json.dumps({"message": {"content": ""}, "done": True, "done_reason": "length"}),
        ]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_ollama_turn("qwen3-235b", [{"role": "user", "content": "直して"}], []))
    finally:
        yoriai.requests.post = original_post

    assert captured["json"]["options"]["num_predict"] == yoriai.CHAT_MAX_OUTPUT_TOKENS, captured["json"]
    final_event = events[-1]
    assert final_event == {"tool_calls": [], "truncated": True}, final_event


def test_stream_ollama_turn_reports_not_truncated_on_normal_stop():
    def fake_post(url, json=None, stream=None, timeout=None):
        lines = [
            _json.dumps({"message": {"content": "OK"}, "done": False}),
            _json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
        ]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_ollama_turn("qwen3-235b", [{"role": "user", "content": "直して"}], []))
    finally:
        yoriai.requests.post = original_post

    assert events[-1] == {"tool_calls": [], "truncated": False}, events[-1]


def test_stream_openai_compatible_turn_sends_max_tokens_and_detects_length_truncation():
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        lines = [
            "data: " + _json.dumps({"choices": [{"delta": {"content": "こんにちは"}}]}),
            "data: " + _json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
            "data: [DONE]",
        ]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_openai_compatible_turn(
            yoriai.LMSTUDIO_BASE_URL, "qwen3-235b", [{"role": "user", "content": "直して"}], [],
        ))
    finally:
        yoriai.requests.post = original_post

    assert captured["json"]["max_tokens"] == yoriai.CHAT_MAX_OUTPUT_TOKENS, captured["json"]
    final_event = events[-1]
    assert final_event == {"tool_calls": [], "truncated": True}, final_event


def test_stream_openai_compatible_turn_reports_not_truncated_on_normal_stop():
    def fake_post(url, json=None, stream=None, timeout=None):
        lines = [
            "data: " + _json.dumps({"choices": [{"delta": {"content": "OK"}}]}),
            "data: " + _json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
            "data: [DONE]",
        ]
        return _FakeResponse(lines)

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        events = list(llm_stream._stream_openai_compatible_turn(
            yoriai.LMSTUDIO_BASE_URL, "qwen3-235b", [{"role": "user", "content": "直して"}], [],
        ))
    finally:
        yoriai.requests.post = original_post

    assert events[-1] == {"tool_calls": [], "truncated": False}, events[-1]


# ---------------------------------------------------------------------------
# stream_chat_completion: 最終doneイベントへのtruncated伝播
# ---------------------------------------------------------------------------

def test_stream_chat_completion_propagates_truncated_flag_to_done_event():
    def fake_lmstudio_turn(model, messages, tools, max_output_tokens=None):
        yield {"content": "途中まで生成された内容"}
        yield {"tool_calls": [], "truncated": True}

    original_ollama = yoriai.get_ollama_loaded_models
    original_mlx = yoriai.get_mlx_lm_models
    original_turn = llm_stream._stream_lmstudio_turn
    yoriai.get_ollama_loaded_models = lambda: []
    yoriai.get_mlx_lm_models = lambda: []
    llm_stream._stream_lmstudio_turn = fake_lmstudio_turn
    try:
        events = list(llm_stream.stream_chat_completion("qwen3-235b", [{"role": "user", "content": "直して"}]))
    finally:
        yoriai.get_ollama_loaded_models = original_ollama
        yoriai.get_mlx_lm_models = original_mlx
        llm_stream._stream_lmstudio_turn = original_turn

    done_events = [e for e in events if e.get("done")]
    assert len(done_events) == 1, events
    assert done_events[0] == {"done": True, "truncated": True}, done_events[0]


# ---------------------------------------------------------------------------
# _request_fix: 打ち切られた応答を「不採用」として扱い、暴走しない
# ---------------------------------------------------------------------------

def test_request_fix_treats_truncated_response_as_failure_without_saving():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"content": "```python\ndef add_todo(text):\n"}  # 閉じ括弧が来る前に打ち切られる想定
        yield {"done": True, "truncated": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_truncation_test_")
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fixed_code = yoriai._request_fix(
                "storage.py", "def add_todo(text):\n    pass\n", "戻り値がNoneです。",
                _member("MacStudio", "qwen3-235b"), "実装計画", "fingerprint", out_dir,
            )
        output = buf.getvalue()
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert fixed_code is None, fixed_code
    assert "打ち切りました" in output, output
    assert not os.path.exists(os.path.join(out_dir, "storage.py")), (
        "打ち切られた不完全な内容でファイルを上書きしてはいけません"
    )


# ---------------------------------------------------------------------------
# _review_and_fix_one_file: 実機の再現シナリオ(複雑な指摘→修正応答が打ち切り)
# ---------------------------------------------------------------------------

def test_review_and_fix_one_file_reports_unresolved_when_fix_response_is_truncated():
    """実機で報告された不具合の再現: 複数の複雑な指摘を受けた修正依頼の
    応答が、CHAT_MAX_OUTPUT_TOKENS上限で打ち切られた場合、暴走(生成が
    終わらない、再試行を繰り返す等)せずに、その回の修正を不採用として
    (=既存の最大2回の枠を1回消費して)、元のレビュー指摘内容を未解決として
    報告することを確認する。2回目のレビューは行われない(=次のアクション
    に確実に進む)ことも合わせて確認する。
    """
    complex_feedback = (
        "ESモジュールとグローバル変数が混在しており、type=\"module\"のscriptから\n"
        "window.initGameを参照できません。iframeのid名がpreview-frameとpreviewFrameで\n"
        "一致していません。スクリプトの読み込み順序により、utils.jsの関数が\n"
        "main.jsから呼ばれる前に定義されていない可能性があります。"
    )
    review_round = {"n": 0}
    fix_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            review_round["n"] += 1
            yield {"content": f"問題あり\n{complex_feedback}"}
            yield {"done": True, "truncated": False}
        elif "現在のコード" in text:
            fix_calls["n"] += 1
            # 実機の報告と同様、生成が延々と続いた末にmax_tokens上限で打ち切られる状況を模擬する。
            yield {"content": "<html><body>\n<script type=\"module\">\n// 修正の途中..."}
            yield {"done": True, "truncated": True}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:100]}")

    out_dir = tempfile.mkdtemp(prefix="yoriai_truncation_test_")
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        ok, feedback = yoriai._review_and_fix_one_file(
            filename="index.html", owner=_member("MacStudio", "qwen3-235b"),
            code="<html><body></body></html>", reviewer=_member("junnoMac-mini", "qwen2.5-coder-14b"),
            reviewer_own_filename="style.css", reviewer_own_code="body {}",
            full_plan="index.html: HTML/CSS/JS学習サイトのトップページ", org_fingerprint="fingerprint",
            out_dir=out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is False, (ok, feedback)
    assert feedback == complex_feedback, feedback
    assert review_round["n"] == 1, f"打ち切られた修正の後、2回目のレビューは行われないはずです(実際: {review_round['n']}回)"
    assert fix_calls["n"] == 1, "修正依頼は既存の枠内で1回だけ行われるはずです(再試行ループに陥っていないこと)"


def main():
    tests = [
        test_stream_ollama_turn_sends_num_predict_and_detects_length_truncation,
        test_stream_ollama_turn_reports_not_truncated_on_normal_stop,
        test_stream_openai_compatible_turn_sends_max_tokens_and_detects_length_truncation,
        test_stream_openai_compatible_turn_reports_not_truncated_on_normal_stop,
        test_stream_chat_completion_propagates_truncated_flag_to_done_event,
        test_request_fix_treats_truncated_response_as_failure_without_saving,
        test_review_and_fix_one_file_reports_unresolved_when_fix_response_is_truncated,
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
