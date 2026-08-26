#!/usr/bin/env python3
"""`//agree`のコンテンツ生成系の依頼向けに新設した独立したリサーチ
フェーズ(`_run_research_phase`)を検証する。

背景: `//agree`はコンテンツ生成系の依頼でも常にソフトウェア実装用の
タスク分解を適用し、Web検索で調査した実質的な内容を伴わない骨組みだけの
成果物が生成される問題があった。この段階的な改修の第二段として、
タスク分解より前に独立したリサーチフェーズを設け、担当メンバーに
`web_search`ツールを実際に呼び出させたうえで、その結果を`research_notes.md`
として保存する仕組みを検証する。`web_search`自体はモデル側のキッチン
プロセス内(サーバー側)で実行され、呼び出し元にはtool_call/tool_result
イベントとして素通しされるだけのため、`yoriai._stream_chat_from_candidate`
をモックしてこれらのイベントを模擬する(`tests/test_fix_project.py`の
`_fake_stream_fix_with_tools`等と同じ手法)。

使い方: python3 tests/test_research_phase.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _researcher():
    return {"label": "MacStudio", "model": "qwen2.5-coder-32b", "address": "127.0.0.1", "port": 47120}


def _fake_stream_two_searches_then_summary(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    """web_searchを2回呼び出してから、要点をまとめた最終回答を返す
    フェイクの/chat応答。web_search自体はモデル側のキッチンプロセス内で
    実行されるため、pending_tool_callsにはならず、tool_call/tool_result
    イベントとして流れたのち同じラウンド内で最終回答に進む。
    """
    yield {"tool_call": "web_search", "tool_call_arguments": {"query": "Obsidian PKM 構築方法"}}
    yield {
        "tool_result": "web_search",
        "tool_result_content": json.dumps(
            {"query": "Obsidian PKM 構築方法", "results": [{"title": "Obsidian公式ガイド", "url": "https://obsidian.md/"}]},
            ensure_ascii=False,
        ),
    }
    yield {"tool_call": "web_search", "tool_call_arguments": {"query": "Obsidian バックリンク 使い方"}}
    yield {
        "tool_result": "web_search",
        "tool_result_content": json.dumps({"query": "Obsidian バックリンク 使い方", "results": []}, ensure_ascii=False),
    }
    yield {"content": "## 調査結果\n- Obsidianはバックリンクでノート同士を関連付けられる\n"}
    yield {"done": True}


def _fake_stream_no_search(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    """web_searchを一度も呼ばず、一般論だけで最終回答を返すフェイク。"""
    yield {"content": "PKMとは知識を個人で管理する手法です(一般論)。"}
    yield {"done": True}


def _fake_stream_error(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    yield {"error": "接続に失敗しました"}


def test_research_phase_saves_notes_when_web_search_is_actually_called():
    """web_searchが実際に呼ばれた場合、その最終回答がそのまま
    research_notes.mdとして保存され、戻り値としても返される。
    """
    project_dir = tempfile.mkdtemp()
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_two_searches_then_summary
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            notes = yoriai._run_research_phase(
                _researcher(), "fp", "ObsidianでPKMを構築するための知識をまとめたWebページを作って", project_dir,
            )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        printed = stdout.getvalue()

    assert "バックリンク" in notes
    saved_path = os.path.join(project_dir, "research_notes.md")
    assert os.path.exists(saved_path)
    with open(saved_path, encoding="utf-8") as f:
        saved_content = f.read()
    assert saved_content == notes
    assert "[🔍 リサーチフェーズ開始" in printed
    shutil.rmtree(project_dir, ignore_errors=True)


def test_research_phase_warns_and_saves_explicit_message_when_no_search_happened():
    """web_searchが一度も呼ばれなかった場合、警告を標準出力に表示し、
    research_notes.mdには空文字列ではなく明示的な「検索結果なし」を
    保存する(呼び出し元がリサーチ失敗を検知できるようにするため)。
    """
    project_dir = tempfile.mkdtemp()
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_no_search
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            notes = yoriai._run_research_phase(_researcher(), "fp", "PKMについてまとめて", project_dir)
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        printed = stdout.getvalue()

    assert notes == yoriai._RESEARCH_NO_RESULTS_MESSAGE
    assert notes != ""
    saved_path = os.path.join(project_dir, "research_notes.md")
    with open(saved_path, encoding="utf-8") as f:
        saved_content = f.read()
    assert saved_content == yoriai._RESEARCH_NO_RESULTS_MESSAGE
    assert "一度もWeb検索が行われませんでした" in printed or "検索が一度も行われませんでした" in printed
    shutil.rmtree(project_dir, ignore_errors=True)


def test_research_phase_falls_back_to_explicit_message_on_query_error():
    """リサーチ担当への問い合わせ自体が失敗した場合も、空文字列ではなく
    明示的な「検索結果なし」メッセージを保存・返却する。
    """
    project_dir = tempfile.mkdtemp()
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_error
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            notes = yoriai._run_research_phase(_researcher(), "fp", "PKMについてまとめて", project_dir)
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        printed = stdout.getvalue()

    assert notes == yoriai._RESEARCH_NO_RESULTS_MESSAGE
    assert "問い合わせに失敗" in printed
    shutil.rmtree(project_dir, ignore_errors=True)


def main():
    tests = [
        test_research_phase_saves_notes_when_web_search_is_actually_called,
        test_research_phase_warns_and_saves_explicit_message_when_no_search_happened,
        test_research_phase_falls_back_to_explicit_message_on_query_error,
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
