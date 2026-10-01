#!/usr/bin/env python3
"""思考(thinking/reasoning)ストリームの中で同じ文章が延々と繰り返される
「思考ループ」を、`_looks_looping`で検知して問い合わせを早期に打ち切ることを
検証する。

実機報告: 提案役(MacStudio・ラウンド3)が思考中の表示のまま同じ数行の文章を
8分以上繰り返した。既存の`_looks_garbled`は最終回答にしか適用されず、
1〜6文字の短いパターンしか見ないため検知できなかった。

使い方: python3 -m pytest tests/test_thinking_loop_detection.py
        python3 tests/test_thinking_loop_detection.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402
import llm_stream  # noqa: E402

_LOOP_SENTENCE = "まず現状を整理し、次に課題を洗い出して、最後に提案をまとめます。\nもう一度確認しましょう。\n"


def _candidate(label="MacStudio", model="m1"):
    return {
        "label": label, "model": model, "address": "127.0.0.1", "port": 47120,
        "free_gb": 10, "has_coding_model": True,
        "specialties": [yoriai.DIALOGUE_SPECIALTY_CODING],
    }


def _diverse_text(n_chars):
    # 繰り返しの無い長い思考を模した、毎回内容が異なる文章。
    out, i = [], 0
    while sum(len(x) for x in out) < n_chars:
        out.append(f"手順{i}: 項目{i * 7 % 13}の値{i * i}を確認し、結果{i + 100}を記録する。\n")
        i += 1
    return "".join(out)[:n_chars]


# --- _looks_looping 単体 ---

def test_looks_looping_true_for_repeated_multiline_sentence():
    assert yoriai._looks_looping(_LOOP_SENTENCE * 5) is True


def test_looks_looping_true_regardless_of_phase():
    text = "前置きの文章です。" + _LOOP_SENTENCE * 4 + _LOOP_SENTENCE[:17]
    assert yoriai._looks_looping(text) is True  # 末尾がブロック途中で切れていても検知する
    assert yoriai._looks_looping("前置きの文章です。" + _LOOP_SENTENCE) is False


def test_looks_looping_false_for_long_diverse_text():
    assert yoriai._looks_looping(_diverse_text(5000)) is False


def test_looks_looping_false_for_only_two_repeats():
    assert yoriai._looks_looping(_diverse_text(300) + _LOOP_SENTENCE * 2) is False


def test_looks_looping_false_for_single_char_ruler_line():
    assert yoriai._looks_looping("-" * 200) is False


def test_existing_looks_garbled_cannot_detect_this_loop():
    # 回帰防止: 既存関数の守備範囲外であることの記録(新関数が必要な理由)。
    assert yoriai._looks_garbled(_LOOP_SENTENCE * 50) is False


# --- speak()経由(_run_dialogue) ---

def _run_dialogue_with_chunks(chunks):
    """`_collect_answer_from_candidate`を、`chunks`を`on_thinking`へ順に流し、
    真値が返ったら打ち切る偽物に差し替えて`_run_dialogue`を実行する。
    """
    fed = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        for chunk in chunks:
            fed.append(chunk)
            if on_thinking(chunk):
                break
        return "", None, False

    original_collect = yoriai._collect_answer_from_candidate
    original_print = yoriai._print_tagged
    yoriai._collect_answer_from_candidate = fake_collect
    yoriai._print_tagged = lambda lock, tag, text: None
    try:
        result = yoriai._run_dialogue(
            org_fingerprint="fp", topic="議題", background="背景", candidates=[_candidate()],
            output_instruction="出力形式の指示",
        )
    finally:
        yoriai._collect_answer_from_candidate = original_collect
        yoriai._print_tagged = original_print
    return result, fed


def test_dialogue_aborts_early_when_thinking_loops():
    chunks = list(_LOOP_SENTENCE * 200)  # 1文字ずつ届く、大量のループ
    result, fed = _run_dialogue_with_chunks(chunks)
    assert result["status"] == yoriai.DIALOGUE_STATUS_GARBLED, result
    assert "思考過程" in result["human_message"], result
    assert len(fed) < len(chunks) // 2, (len(fed), len(chunks))
    # 打ち切りまでの思考は議事録にreasoningとして残る
    utterance = result["transcript"][-1]
    assert utterance["reasoning"] and utterance["content"] == "", utterance


def test_dialogue_does_not_abort_long_non_repeating_thinking():
    chunks = list(_diverse_text(8000))
    result, fed = _run_dialogue_with_chunks(chunks)
    assert len(fed) == len(chunks)
    assert result["status"] == yoriai.DIALOGUE_STATUS_NO_ENGAGEMENT, result


# --- _collect_answer_from_candidate / ストリームのclose ---

def test_collect_stops_and_closes_stream_when_on_thinking_returns_true():
    state = {"closed": False, "yielded": 0}

    def fake_stream(candidate, fp, messages, disable_web_search=False):
        try:
            for i in range(1000):
                state["yielded"] += 1
                yield {"thinking": f"t{i}"}
        finally:
            state["closed"] = True

    original = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        answer, error, truncated = yoriai._collect_answer_from_candidate(
            _candidate(), "fp", [], on_thinking=lambda t: t == "t4",
        )
    finally:
        yoriai._stream_chat_from_candidate = original
    assert (answer, error, truncated) == ("", None, False)
    assert state["yielded"] == 5 and state["closed"] is True, state


def test_collect_ignores_none_return_from_legacy_on_thinking():
    def fake_stream(candidate, fp, messages, disable_web_search=False):
        yield {"thinking": "a"}
        yield {"content": "答え"}
        yield {"done": True}

    original = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        answer, _error, _truncated = yoriai._collect_answer_from_candidate(
            _candidate(), "fp", [], on_thinking=lambda t: None,
        )
    finally:
        yoriai._stream_chat_from_candidate = original
    assert answer == "答え"


def test_openai_compatible_turn_closes_response_when_generator_closed():
    class FakeResp:
        ok = True
        closed = False

        def iter_lines(self):
            for _ in range(1000):
                yield b'data: {"choices":[{"delta":{"reasoning_content":"x"}}]}'

        def close(self):
            self.closed = True

    resp = FakeResp()
    original_post = llm_stream.requests.post
    llm_stream.requests.post = lambda *a, **k: resp
    try:
        gen = llm_stream._stream_openai_compatible_turn("http://x", "m", [], None)
        assert next(gen) == {"thinking": "x"}
        gen.close()
    finally:
        llm_stream.requests.post = original_post
    assert resp.closed is True


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
