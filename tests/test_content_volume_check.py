#!/usr/bin/env python3
"""コンテンツ生成系の`//agree`向けに新設した内容量・完成度チェック
(`_check_content_volume`)を検証する。

背景: `_run_integration_verification`は検証コマンドの実行結果(成功/失敗)
のみを見ており、生成物の内容量やプレースホルダーの有無は一切チェック
していない。コンテンツ生成系の依頼は検証コマンドを持たないことが多く、
統合検証自体がスキップされるため、骨組みだけの成果物がそのまま素通りして
しまう問題があった。この段階的な改修の第四段(最終段)として、独立した
警告専用チェックを検証する。

使い方: python3 tests/test_content_volume_check.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _write_raw(project_dir, filename, content):
    with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)


def test_check_content_volume_warns_when_file_is_too_short():
    """生成ファイルの文字数が閾値(`_CONTENT_VOLUME_MIN_CHARS`)を下回る
    場合、警告が出て`ok`が`False`になることを確認する。
    """
    project_dir = tempfile.mkdtemp()
    try:
        _write_raw(project_dir, "index.html", "<html><body>短い内容です</body></html>")
        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert result["ok"] is False, result
        assert any("index.html" in w and "文字数" in w for w in result["warnings"]), result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_check_content_volume_warns_on_placeholder_markers():
    """「TODO」「準備中」等の未完成を示す文字列が含まれる場合、文字数が
    十分でも警告が出ることを確認する。
    """
    project_dir = tempfile.mkdtemp()
    try:
        long_body = "本文のダミーテキストです。" * 40  # 十分な文字数を確保する
        content = f"<html><body>{long_body}<!-- TODO: 後で書く --></body></html>"
        _write_raw(project_dir, "index.html", content)
        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert result["ok"] is False, result
        assert any("TODO" in w for w in result["warnings"]), result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_check_content_volume_warns_when_research_keywords_are_not_reflected():
    """`research_notes.md`から抽出したキーワードが、生成コンテンツ側に
    ほとんど反映されていない場合、警告が出ることを確認する。
    """
    project_dir = tempfile.mkdtemp()
    try:
        _write_raw(
            project_dir, "research_notes.md",
            "Obsidianは強力なPKMツールです。バックリンクという機能でノート同士を関連付けられます。",
        )
        # 上の調査結果のキーワード(Obsidian/PKM/バックリンク)を一切含まない
        # 内容で、文字数・プレースホルダーの条件は満たすようにする。
        long_body = "知識管理についての一般的な説明を長めに書いたダミー本文です。" * 20
        _write_raw(project_dir, "index.html", f"<html><body>{long_body}</body></html>")

        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert result["ok"] is False, result
        assert any("キーワード" in w for w in result["warnings"]), result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_check_content_volume_ok_when_content_is_sufficient_and_reflects_keywords():
    """文字数が十分・プレースホルダーが無い・調査結果のキーワードも
    反映されている場合は警告が出ないことを確認する(正常系)。
    """
    project_dir = tempfile.mkdtemp()
    try:
        _write_raw(
            project_dir, "research_notes.md",
            "Obsidianは強力なPKMツールです。バックリンクという機能でノート同士を関連付けられます。",
        )
        long_body = (
            "Obsidianを使ったPKM(パーソナルナレッジマネジメント)の構築方法について解説します。"
            "バックリンク機能を使うことで、ノート同士を柔軟に関連付けられます。"
        ) * 8
        _write_raw(project_dir, "index.html", f"<html><body>{long_body}</body></html>")

        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert result["ok"] is True, result
        assert result["warnings"] == [], result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_check_content_volume_skips_keyword_check_when_research_notes_missing():
    """`research_notes.md`が存在しない場合(リサーチフェーズを経ていない
    場合)でも、キーワード反映チェックだけがスキップされ、文字数・
    プレースホルダーのチェックは通常通り行われることを確認する。
    """
    project_dir = tempfile.mkdtemp()
    try:
        long_body = "本文のダミーテキストです。" * 40
        _write_raw(project_dir, "index.html", f"<html><body>{long_body}</body></html>")

        result = yoriai._check_content_volume(project_dir, [("index.html", "説明")])
        assert result["ok"] is True, result
        assert not any("キーワード" in w for w in result["warnings"]), result["warnings"]
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def main():
    tests = [
        test_check_content_volume_warns_when_file_is_too_short,
        test_check_content_volume_warns_on_placeholder_markers,
        test_check_content_volume_warns_when_research_keywords_are_not_reflected,
        test_check_content_volume_ok_when_content_is_sufficient_and_reflects_keywords,
        test_check_content_volume_skips_keyword_check_when_research_notes_missing,
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
