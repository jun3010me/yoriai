#!/usr/bin/env python3
"""`_is_research_failed`(研究ノートが検索未実施/失敗を示しているかの
判定ヘルパー)と、その判定を使う既存の呼び出し元を検証する。

背景: `//agree`のコンテンツ生成系タスクでは、リサーチフェーズ
(`_run_research_phase`)がWeb検索に失敗した場合、`research_notes.md`に
`_RESEARCH_NO_RESULTS_MESSAGE`という固定文言を保存する。この「検索に
失敗したかどうか」の判定は、従来`research_notes != _RESEARCH_NO_RESULTS_
MESSAGE`という文字列完全一致が、`_build_collaborative_implementation_
request`(実装フェーズへの調査結果引き継ぎ)・`_check_content_volume`
(生成物への調査結果キーワード反映チェック)の2箇所に直書きされていた。
判定ロジックを`_is_research_failed`に共通化する(判定条件自体・
`_RESEARCH_NO_RESULTS_MESSAGE`の文言はいずれも変更しない)。

なお、リサーチフェーズ自身が「web_searchが1回も呼ばれなかった場合に
1回だけ促し直して再試行する」機構は、`_run_research_phase`独自のもの
ではなく`tools.py`の`_collect_answer_with_project_tools`が汎用的な
ツール未呼び出しナッジ機構として既に備えており、
`tests/test_research_phase.py`の
`test_research_phase_recovers_by_nudging_with_web_search_specific_message`
で検証済みのため、ここでは重複させない。

使い方: python3 tests/test_research_phase_enforcement.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_is_research_failed_true_for_no_results_message():
    """`_RESEARCH_NO_RESULTS_MESSAGE`そのものは検索失敗と判定される。"""
    assert yoriai._is_research_failed(yoriai._RESEARCH_NO_RESULTS_MESSAGE)


def test_is_research_failed_true_with_surrounding_whitespace():
    """`research_notes.md`をファイル経由で読み込んだ場合等、前後に空白・
    改行が付いていても検索失敗と判定される(厳密な完全一致より頑健)。
    """
    assert yoriai._is_research_failed(f"\n{yoriai._RESEARCH_NO_RESULTS_MESSAGE}\n")


def test_is_research_failed_false_for_empty_string():
    """空文字列は「検索に失敗した」わけではない(ソフトウェア実装系
    `//agree`ではリサーチフェーズ自体を実行せず、research_notesは空文字列
    のまま各呼び出し元に渡ってくる)ため、検索失敗とはみなさない。
    """
    assert not yoriai._is_research_failed("")


def test_is_research_failed_false_for_real_research_notes():
    """実質的な調査結果の要約は検索失敗と判定されない。"""
    assert not yoriai._is_research_failed("## 調査結果\n- 具体的な内容\n")


def test_collaborative_implementation_request_omits_section_when_research_failed():
    """検索に失敗した`research_notes`が渡された場合、実装フェーズへの
    依頼文に調査結果セクションが含まれないことを確認する(既存の挙動の
    回帰防止)。
    """
    request = yoriai._build_collaborative_implementation_request(
        "page.md", "本文", [("page.md", "内容")], yoriai._RESEARCH_NO_RESULTS_MESSAGE,
    )
    assert "【調査結果" not in request


def test_collaborative_implementation_request_includes_section_when_research_succeeded():
    """検索に成功した`research_notes`が渡された場合は、調査結果セクションが
    含まれることを確認する(既存の挙動の回帰防止)。
    """
    request = yoriai._build_collaborative_implementation_request(
        "page.md", "本文", [("page.md", "内容")], "## 調査結果\n- 具体的な内容\n",
    )
    assert "【調査結果" in request
    assert "具体的な内容" in request


def _write_raw(project_dir, filename, content):
    with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)


def test_check_content_volume_skips_keyword_check_when_research_failed():
    """`research_notes.md`が検索失敗の内容のままだった場合、キーワード
    反映チェック自体がスキップされ、そのことを理由にした警告が出ない
    ことを確認する(既存の挙動の回帰防止)。
    """
    project_dir = tempfile.mkdtemp()
    try:
        long_body = "本文のダミーテキストです。" * 40
        _write_raw(project_dir, "index.html", f"<html><body>{long_body}</body></html>")
        _write_raw(project_dir, "research_notes.md", yoriai._RESEARCH_NO_RESULTS_MESSAGE)
        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert not any("キーワード" in w for w in result["warnings"]), result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def main():
    tests = [
        test_is_research_failed_true_for_no_results_message,
        test_is_research_failed_true_with_surrounding_whitespace,
        test_is_research_failed_false_for_empty_string,
        test_is_research_failed_false_for_real_research_notes,
        test_collaborative_implementation_request_omits_section_when_research_failed,
        test_collaborative_implementation_request_includes_section_when_research_succeeded,
        test_check_content_volume_skips_keyword_check_when_research_failed,
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
