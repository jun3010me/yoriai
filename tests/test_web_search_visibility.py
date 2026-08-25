#!/usr/bin/env python3
"""単発質問(`_ask_organization`)がweb_searchツールを使う際、何を検索し
何が見つかったのかが画面に一切表示されず、ユーザーから見て「うまく
いっているのか失敗しているのか分からない」という指摘への対応を検証する。

以前は`stream_chat_completion`がツール呼び出し自体({"tool_call": ...})
しかイベントとして流さず、実際の検索結果(モデルへ返す`result`)は
`messages`に内部的に追記されるだけで、呼び出し元には一切見えなかった。
そのため`_ask_organization`は「[🔍 ウェブ検索しています...]」という
検索語も結果も分からない汎用メッセージしか表示できなかった。

この修正では、
- `stream_chat_completion`がツール実行結果も{"tool_result": ..., "tool_result_content": ...}
  としてイベントに流すようにした
- `_ask_organization`が検索語(tool_call_arguments)と検索結果の件数・
  タイトル・URL(`_format_web_search_result_summary`)を画面に表示する
ようにした。

使い方: python3 tests/test_web_search_visibility.py
"""
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tools  # noqa: E402
import yoriai  # noqa: E402


def _fake_snapshot(port, fp, fail_fast=False):
    return {
        "self": {
            "device_name": "MacStudio",
            "models": {"loaded": ["qwen2.5-coder-32b"], "installed": ["qwen2.5-coder-32b"]},
            "memory": {"free_gb": 40},
        },
        "peers": [],
    }


def test_stream_chat_completion_yields_tool_result_event_for_web_search():
    """`stream_chat_completion`が、web_searchの実行結果を`tool_call`とは
    別に`tool_result`イベントとしても流すことを確認する(呼び出し元が
    検索結果を画面表示できるようにするための土台)。
    """
    call_count = {"n": 0}

    def fake_turn(base_url, model, messages, tools):
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield {
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "web_search", "arguments": {"query": "Obsidian PKM"}}},
                ],
            }
        else:
            yield {"content": "検索結果を踏まえた回答です"}
            yield {"tool_calls": []}

    original_turn = yoriai._stream_openai_compatible_turn
    original_search = tools.web_search
    yoriai._stream_openai_compatible_turn = fake_turn
    tools.web_search = lambda query: [
        {"title": "Obsidian公式", "url": "https://obsidian.md/", "snippet": "Obsidianの公式サイト"},
    ]
    try:
        events = list(yoriai.stream_chat_completion(
            "some-model", [{"role": "user", "content": "ObsidianでPKMを構築するには?"}],
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original_turn
        tools.web_search = original_search

    tool_result_events = [e for e in events if e.get("tool_result") == "web_search"]
    assert len(tool_result_events) == 1, f"tool_resultイベントが1件流れるはずです: {events}"
    payload = json.loads(tool_result_events[0]["tool_result_content"])
    assert payload["query"] == "Obsidian PKM", payload
    assert payload["results"] == [
        {"title": "Obsidian公式", "url": "https://obsidian.md/", "snippet": "Obsidianの公式サイト"},
    ], payload


def test_format_web_search_result_summary_shows_titles_and_urls():
    content = json.dumps({
        "query": "Obsidian PKM",
        "results": [
            {"title": "Obsidian公式", "url": "https://obsidian.md/", "snippet": "..."},
            {"title": "PKM入門", "url": "https://example.com/pkm", "snippet": "..."},
        ],
    }, ensure_ascii=False)
    summary = yoriai._format_web_search_result_summary(content)
    assert "2件ヒット" in summary, summary
    assert "Obsidian公式" in summary and "https://obsidian.md/" in summary, summary
    assert "PKM入門" in summary and "https://example.com/pkm" in summary, summary


def test_format_web_search_result_summary_handles_no_results():
    content = json.dumps({"query": "存在しないキーワード", "results": []}, ensure_ascii=False)
    summary = yoriai._format_web_search_result_summary(content)
    assert "見つかりませんでした" in summary, summary


def test_ask_organization_prints_query_and_result_titles():
    """`_ask_organization`が、検索語(何を調べたか)と検索結果のタイトル・
    URL(何が見つかったか)を画面に表示することを確認する(修正前は
    「[🔍 ウェブ検索しています...]」としか表示されず、検索語も結果も
    一切分からなかった)。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"tool_call": "web_search", "tool_call_arguments": {"query": "Obsidian PKM 構築方法"}}
        yield {
            "tool_result": "web_search",
            "tool_result_content": json.dumps({
                "query": "Obsidian PKM 構築方法",
                "results": [
                    {"title": "Obsidianでゼロから始めるPKM", "url": "https://example.com/a", "snippet": "..."},
                ],
            }, ensure_ascii=False),
        }
        yield {"content": "調査結果を踏まえた回答です"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = _fake_snapshot
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization(47120, "fingerprint", [{"role": "user", "content": "ObsidianでPKMを構築したい"}])
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream

    output = buf.getvalue()
    assert "Obsidian PKM 構築方法" in output, f"検索語が表示されるはずです: {output}"
    assert "Obsidianでゼロから始めるPKM" in output, f"検索結果のタイトルが表示されるはずです: {output}"
    assert "https://example.com/a" in output, f"検索結果のURLが表示されるはずです: {output}"


def main():
    tests = [
        test_stream_chat_completion_yields_tool_result_event_for_web_search,
        test_format_web_search_result_summary_shows_titles_and_urls,
        test_format_web_search_result_summary_handles_no_results,
        test_ask_organization_prints_query_and_result_titles,
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
