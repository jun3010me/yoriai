#!/usr/bin/env python3
"""ツール呼び出しラウンド上限(MAX_TOOL_CALL_ROUNDS)到達時に、空の応答の
まま打ち切られる不具合の修正を検証するテスト。

思考系モデルがweb_searchを律儀に立て続けに要求し続けると、最終ラウンド
(round_num == MAX_TOOL_CALL_ROUNDS)でもcontentを1文字も生成せずツール
呼び出しだけを返してくることがあり、従来はそのままanswer_partsが空の
まま呼び出し元(_ask_organization等)に返ってしまい、「有効な応答が
得られなかった」と誤判定されて次の候補モデルへのフォールバックが
発生していた。この修正では、その場合に限り「これ以上ツールは使わない」
旨のシステムメッセージを1回だけ追加し、tools無しでもう1往復だけ
問い合わせて最終回答として採用する。

使い方: python3 tests/test_final_no_tool_retry.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_round_limit_with_empty_content_falls_back_to_final_no_tool_query():
    """最終ラウンドまでcontentを一切生成せずweb_searchだけを要求し続けた
    場合、tools無しの最終問い合わせが1回だけ行われ、その結果(content)が
    最終回答として得られることを確認する。
    """
    call_count = {"n": 0}

    def fake_turn(base_url, model, messages, tools):
        call_count["n"] += 1
        if tools:
            yield {
                "tool_calls": [
                    {"id": f"call_{call_count['n']}", "type": "function",
                     "function": {"name": "web_search", "arguments": {"query": "しつこく調べる"}}},
                ],
            }
        else:
            yield {"content": "ツール無しで分かる範囲の回答です"}
            yield {"tool_calls": []}

    original_turn = yoriai._stream_openai_compatible_turn
    original_search = yoriai.web_search
    yoriai._stream_openai_compatible_turn = fake_turn
    yoriai.web_search = lambda query: [{"title": "dummy", "url": "http://example.com", "snippet": "dummy"}]
    try:
        events = list(yoriai.stream_chat_completion(
            "thinking-model", [{"role": "user", "content": "念入りに調べて回答して"}],
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original_turn
        yoriai.web_search = original_search

    # ラウンド0,1,2,3(MAX_TOOL_CALL_ROUNDS+1回)まではツール付きで
    # web_searchだけを要求し続け、最後にtools無しの1往復が追加で
    # 呼ばれるはずなので、合計で MAX_TOOL_CALL_ROUNDS + 2 回問い合わせる。
    assert call_count["n"] == yoriai.MAX_TOOL_CALL_ROUNDS + 2, (
        f"tools無し最終問い合わせを含めた呼び出し回数が想定と異なります: {call_count['n']}"
    )
    contents = [e["content"] for e in events if "content" in e]
    assert contents == ["ツール無しで分かる範囲の回答です"], (
        f"tools無し最終問い合わせの回答が最終回答として得られていません: {events}"
    )
    assert events[-1] == {"done": True, "truncated": False}, events[-1]


def test_final_no_tool_query_still_empty_is_treated_as_no_valid_response():
    """tools無しの最終問い合わせでもcontentが空だった場合は、従来通り
    空のまま(無限ループにならず1回だけ試行して)終了することを確認する。
    """
    call_count = {"n": 0}

    def fake_turn(base_url, model, messages, tools):
        call_count["n"] += 1
        if tools:
            yield {
                "tool_calls": [
                    {"id": f"call_{call_count['n']}", "type": "function",
                     "function": {"name": "web_search", "arguments": {"query": "しつこく調べる"}}},
                ],
            }
        else:
            # tools無しでもなお空(content無し・tool_calls無し)を返すケース。
            yield {"tool_calls": []}

    original_turn = yoriai._stream_openai_compatible_turn
    original_search = yoriai.web_search
    yoriai._stream_openai_compatible_turn = fake_turn
    yoriai.web_search = lambda query: [{"title": "dummy", "url": "http://example.com", "snippet": "dummy"}]
    try:
        events = list(yoriai.stream_chat_completion(
            "thinking-model", [{"role": "user", "content": "念入りに調べて回答して"}],
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original_turn
        yoriai.web_search = original_search

    # tools無し最終問い合わせは1回限りで、そこでも空ならそれ以上は
    # 再試行せずに終了する(無限ループにならない)。
    assert call_count["n"] == yoriai.MAX_TOOL_CALL_ROUNDS + 2, (
        f"tools無し最終問い合わせが1回だけ行われるはずです: {call_count['n']}"
    )
    contents = [e["content"] for e in events if "content" in e]
    assert contents == [], f"contentが空のまま終わるはずです: {events}"
    assert events[-1] == {"done": True, "truncated": False}, events


def test_content_produced_before_round_limit_does_not_trigger_final_no_tool_query():
    """通常通り、最終ラウンドより前にcontent付きの回答が得られていれば
    (=空の応答で打ち切られる問題が発生していなければ)、tools無しの
    追加問い合わせは行われないことを確認する(既存挙動の非退行確認)。
    """
    call_count = {"n": 0}

    def fake_turn(base_url, model, messages, tools):
        call_count["n"] += 1
        yield {"content": "1回で答えられます"}
        yield {"tool_calls": []}

    original_turn = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        events = list(yoriai.stream_chat_completion(
            "simple-model", [{"role": "user", "content": "こんにちは"}],
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original_turn

    assert call_count["n"] == 1, f"1回で完了するはずです: {call_count['n']}"
    contents = [e["content"] for e in events if "content" in e]
    assert contents == ["1回で答えられます"], events


def main():
    tests = [
        test_round_limit_with_empty_content_falls_back_to_final_no_tool_query,
        test_final_no_tool_query_still_empty_is_treated_as_no_valid_response,
        test_content_produced_before_round_limit_does_not_trigger_final_no_tool_query,
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
