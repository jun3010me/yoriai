#!/usr/bin/env python3
"""`//agree`の対話プロトコル(`_run_dialogue`の`speak()`内`_on_thinking`)が、
SSEのdeltaチャンク(数文字〜1単語程度)ごとに`_print_tagged`を呼ばず、ある
程度まとまるまでバッファしてから1回だけ`_print_tagged`を呼ぶことを検証する。

実機報告: チャンクが届くたびに`_print_tagged`(改行付きprint)を呼んで
いたため、単語1つごとにプレフィックス([🤔 ...さん(...) 思考中])が
繰り返し表示され非常に読みにくかった。並行ワーカーの出力が混ざらない
ようにする`_print_tagged`自体(print_lock保持・1回のprintでまとめて出す)
はそのまま使い続け、`speak()`側でチャンクを溜めてからまとめて渡す方式に
変更した。

tests/test_dialogue_protocol.pyと同じ方式(`_collect_answer_from_candidate`
自体を差し替え、`on_thinking`コールバックを直接呼び出す)でテストする。
単一候補・1発言(提案役ラウンド1)だけでNO_ENGAGEMENTとして打ち切られる
`test_run_dialogue_no_engagement_when_proposer_never_answers`と同じ経路
(空応答・error無し・truncated無し)を使うことで、`speak()`が1回しか
呼ばれない状況を作り、`_on_thinking`の挙動をチャンク単位で直接観察できる
ようにしている。

使い方: python3 -m pytest tests/test_thinking_display_buffering.py
        python3 tests/test_thinking_display_buffering.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _candidate(label, model, free_gb=10, coding=True):
    return {
        "label": label, "model": model, "address": "127.0.0.1", "port": 47120,
        "free_gb": free_gb, "has_coding_model": coding,
        "specialties": [yoriai.DIALOGUE_SPECIALTY_CODING if coding else yoriai.DIALOGUE_SPECIALTY_GENERAL],
    }


# 単一候補が「提案役・ラウンド1」として発言する際に使われるプレフィックス。
_THINKING_PREFIX = "[🤔 MacStudioさん(提案役・ラウンド1) 思考中] "


def _run_with_thinking(fake_collect):
    """`_collect_answer_from_candidate`を`fake_collect`に差し替え、
    `_print_tagged`への呼び出し(text引数のみ)を記録したリストを返す。
    `fake_collect`は空応答(error無し・truncated無し)を返すことで、
    `_run_dialogue`が提案役ラウンド1の1発言だけでNO_ENGAGEMENTとして
    打ち切るようにし、`speak()`(と`_on_thinking`)がちょうど1回だけ
    呼ばれる状況を作る。
    """
    printed = []

    def fake_print_tagged(print_lock, tag, text):
        printed.append(text)

    original_collect = yoriai._collect_answer_from_candidate
    original_print_tagged = yoriai._print_tagged
    yoriai._collect_answer_from_candidate = fake_collect
    yoriai._print_tagged = fake_print_tagged
    try:
        candidates = [_candidate("MacStudio", "m1")]
        result = yoriai._run_dialogue(
            org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
            output_instruction="出力形式の指示",
        )
    finally:
        yoriai._collect_answer_from_candidate = original_collect
        yoriai._print_tagged = original_print_tagged

    assert result["status"] == yoriai.DIALOGUE_STATUS_NO_ENGAGEMENT, result
    assert result["total_utterances"] == 1, result
    return printed


def _thinking_texts(printed):
    return [text for text in printed if text.startswith("[🤔")]


# ---------------------------------------------------------------------------
# 短いチャンクは閾値に達するまでバッファされる
# ---------------------------------------------------------------------------

def test_on_thinking_buffers_short_chunks_until_threshold():
    seen_after_each_chunk = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        # 1回ごとは短く(合計でも40文字未満)、文末記号も含まない。
        for chunk in ["We", " need", " to", " think", " about", " this", " carefully"]:
            on_thinking(chunk)
            seen_after_each_chunk.append(chunk)
        return "", None, False

    printed = _run_with_thinking(fake_collect)

    # 閾値(40文字)未満のうちは、一度も`_print_tagged`が呼ばれていない
    # (=まとめてフラッシュされる前に途中経過が出ていない)ことを、
    # 呼び出し直後ではなく最終結果から確認する: 全チャンクを結合しても
    # 40文字未満なので、フラッシュは`_collect_answer_from_candidate`完了後
    # の最終フラッシュ1回だけのはず。
    combined = "".join(seen_after_each_chunk)
    assert len(combined) < 40, "このテストの前提(40文字未満)が崩れています"

    thinking_texts = _thinking_texts(printed)
    assert len(thinking_texts) == 1, thinking_texts
    assert thinking_texts[0] == _THINKING_PREFIX + combined


def test_on_thinking_does_not_flush_mid_stream_below_threshold():
    """閾値到達前は、チャンクが届くたびに`_print_tagged`が呼ばれていない
    ことを、コールバックの最中(まだ`_collect_answer_from_candidate`が
    完了する前)に直接確認する。
    """
    printed_snapshot_after_first_chunk = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        original_print_tagged = yoriai._print_tagged
        calls_so_far = []

        def counting_print_tagged(print_lock, tag, text):
            calls_so_far.append(text)

        yoriai._print_tagged = counting_print_tagged
        try:
            on_thinking("短い")
            printed_snapshot_after_first_chunk.append(list(calls_so_far))
            on_thinking("チャンク")
            printed_snapshot_after_first_chunk.append(list(calls_so_far))
        finally:
            yoriai._print_tagged = original_print_tagged
        return "", None, False

    _run_with_thinking(fake_collect)

    assert printed_snapshot_after_first_chunk[0] == [], "1チャンク目でフラッシュされてしまっています"
    assert printed_snapshot_after_first_chunk[1] == [], "2チャンク目でフラッシュされてしまっています"


# ---------------------------------------------------------------------------
# 文末記号(半角・全角)で終わっていれば、閾値未満でも即座にフラッシュ
# ---------------------------------------------------------------------------

def test_on_thinking_flushes_on_sentence_ending_punctuation():
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        on_thinking("ok.")  # 半角ピリオドで終わる短いチャンク → 即座にフラッシュされるはず
        on_thinking("残り")  # 続く端数(このコールバック内ではまだフラッシュされない)
        return "", None, False

    printed = _run_with_thinking(fake_collect)
    thinking_texts = _thinking_texts(printed)

    # 1回目: "ok."が即座にフラッシュされたもの。2回目: 残った端数の最終フラッシュ。
    assert thinking_texts == [_THINKING_PREFIX + "ok.", _THINKING_PREFIX + "残り"], thinking_texts


def test_on_thinking_flushes_on_full_width_sentence_ending_punctuation():
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        on_thinking("これで結論です。")  # 全角句点で終わる → 即座にフラッシュされるはず
        return "", None, False

    printed = _run_with_thinking(fake_collect)
    thinking_texts = _thinking_texts(printed)
    assert thinking_texts == [_THINKING_PREFIX + "これで結論です。"], thinking_texts


# ---------------------------------------------------------------------------
# 問い合わせ完了後、端数が残っていれば必ず最後にフラッシュされる
# ---------------------------------------------------------------------------

def test_on_thinking_flushes_remaining_buffer_after_answer_completes():
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        # 40文字未満・文末記号無しの端数を残したまま問い合わせを完了する。
        on_thinking("まだ結論に至っていない途中経過")
        return "", None, False

    printed = _run_with_thinking(fake_collect)
    thinking_texts = _thinking_texts(printed)

    assert thinking_texts == [_THINKING_PREFIX + "まだ結論に至っていない途中経過"], thinking_texts


# ---------------------------------------------------------------------------
# 表示の間引きによってチャンクの内容が欠けたり順序が入れ替わったりしない
# ---------------------------------------------------------------------------

def test_on_thinking_display_does_not_lose_or_reorder_content():
    chunks = [
        "First", " we", " consider", " the", " requirements", " carefully", " and", " thoroughly",
        " before", " moving", " on.",  # ここで文末記号によりフラッシュ
        "次に", "日本語で", "検討します", "。",  # ここでも文末記号によりフラッシュ
        "最後に", "端数が残る",  # 閾値・文末記号どちらにも達しない端数
    ]

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, on_thinking=None):
        for chunk in chunks:
            on_thinking(chunk)
        return "", None, False

    printed = _run_with_thinking(fake_collect)
    thinking_texts = _thinking_texts(printed)

    reconstructed = "".join(text[len(_THINKING_PREFIX):] for text in thinking_texts)
    assert reconstructed == "".join(chunks), (reconstructed, "".join(chunks))
    # 複数回にわたってフラッシュされている(1回にまとめて出力されている
    # わけではない)ことも確認しておく。
    assert len(thinking_texts) >= 2, thinking_texts


def main():
    tests = [
        test_on_thinking_buffers_short_chunks_until_threshold,
        test_on_thinking_does_not_flush_mid_stream_below_threshold,
        test_on_thinking_flushes_on_sentence_ending_punctuation,
        test_on_thinking_flushes_on_full_width_sentence_ending_punctuation,
        test_on_thinking_flushes_remaining_buffer_after_answer_completes,
        test_on_thinking_display_does_not_lose_or_reorder_content,
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
