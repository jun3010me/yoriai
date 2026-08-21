#!/usr/bin/env python3
"""対話モード起動時の案内文(ロゴ・メンバー数・基本操作・コマンド一覧)の
見た目の改善を検証する。

Claude Code等のツールに近い洗練された印象にしたいという依頼を受けて、
(1)シンプルなASCIIアートのロゴ、(2)情報量の多かった説明文を「基本操作」
「コマンド」の見出しの下に階層立てて整理、(3)検出済みのメンバー数の表示、
(4)色付け(ANSIエスケープシーケンス、ただし色に対応しない環境でも
崩れないように無効化できる)を実装した。

使い方: python3 tests/test_startup_banner.py
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_supports_ansi_color_is_false_when_no_color_env_set():
    original = os.environ.get("NO_COLOR")
    os.environ["NO_COLOR"] = "1"
    try:
        assert yoriai._supports_ansi_color() is False
    finally:
        if original is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = original


def test_supports_ansi_color_is_false_for_dumb_terminal():
    original_no_color = os.environ.pop("NO_COLOR", None)
    original_term = os.environ.get("TERM")
    os.environ["TERM"] = "dumb"
    try:
        assert yoriai._supports_ansi_color() is False
    finally:
        if original_term is None:
            os.environ.pop("TERM", None)
        else:
            os.environ["TERM"] = original_term
        if original_no_color is not None:
            os.environ["NO_COLOR"] = original_no_color


def test_supports_ansi_color_is_false_when_stdout_is_not_a_tty():
    """`contextlib.redirect_stdout`(このテストスイート全体で使っている
    出力キャプチャ手法)でリダイレクトされた`io.StringIO`は`isatty()`が
    常に偽を返すため、テスト実行中は自動的に色付けが無効になることを
    確認する(依頼の「色が使えない環境でも崩れずに表示できるように」を、
    パイプ・リダイレクト環境にも広く解釈して満たす)。
    """
    original_no_color = os.environ.pop("NO_COLOR", None)
    original_term = os.environ.pop("TERM", None)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert yoriai._supports_ansi_color() is False
    finally:
        if original_no_color is not None:
            os.environ["NO_COLOR"] = original_no_color
        if original_term is not None:
            os.environ["TERM"] = original_term


def test_ansi_wraps_text_only_when_color_enabled():
    assert yoriai._ansi("foo", yoriai._ANSI_BOLD, use_color=False) == "foo"
    colored = yoriai._ansi("foo", yoriai._ANSI_BOLD, use_color=True)
    assert colored != "foo"
    assert "foo" in colored
    assert colored.startswith(yoriai._ANSI_BOLD)
    assert colored.endswith(yoriai._ANSI_RESET)


def test_ansi_without_codes_returns_plain_text_even_when_color_enabled():
    assert yoriai._ansi("foo", use_color=True) == "foo"


def test_format_member_count_line_shows_count_including_self():
    assert "3台" in yoriai._format_member_count_line(3)
    assert "自分を含む" in yoriai._format_member_count_line(3)


def test_format_member_count_line_handles_unknown_count():
    line = yoriai._format_member_count_line(None)
    assert "3台" not in line
    assert "取得できませんでした" in line


def test_count_org_members_counts_self_plus_peers():
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: {
        "self": {}, "peers": [{"card": {}}, {"card": {}}],
    }
    try:
        assert yoriai._count_org_members(47120, "fingerprint") == 3
    finally:
        yoriai._fetch_org_snapshot = original_snapshot


def test_count_org_members_returns_none_when_snapshot_unavailable():
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._count_org_members(47120, "fingerprint")
        assert result is None
    finally:
        yoriai._fetch_org_snapshot = original_snapshot


def test_banner_stays_within_80_columns():
    """依頼の要件1: ターミナルの横幅(80文字程度)に収まる大きさにする、
    という要件を、ロゴ行を含む全行について確認する(色付けを無効にした
    状態で、全角文字を2列として数える簡易的な幅計算を使う)。
    """
    banner = yoriai._format_startup_banner(".", 3, use_color=False)
    for line in banner.splitlines():
        width = sum(2 if ord(ch) > 0xFF else 1 for ch in line)
        assert width <= 80, f"80文字を超える行があります({width}): {line!r}"


def test_banner_without_color_contains_no_ansi_escape_codes():
    banner = yoriai._format_startup_banner(".", 3, use_color=False)
    assert "\x1b[" not in banner


def test_banner_with_color_contains_ansi_escape_codes():
    banner = yoriai._format_startup_banner(".", 3, use_color=True)
    assert "\x1b[" in banner


def test_banner_includes_logo_and_member_count():
    banner = yoriai._format_startup_banner(".", 2, use_color=False)
    assert "YORIAI" in banner.replace(" ", "") or "Yoriai" in banner or "寄合" in banner
    assert "2台" in banner


def test_banner_includes_all_commands():
    banner = yoriai._format_startup_banner(".", None, use_color=False)
    for command in (
        yoriai.MULTI_QUERY_COMMAND, yoriai.AGREE_COMMAND,
        yoriai.PARALLEL_QUERY_COMMAND, yoriai.RESUME_ALL_COMMAND,
    ):
        assert command in banner, f"{command} が案内文に含まれていません"


def test_banner_still_documents_required_key_bindings_and_emergency_exit():
    """既存のtests/test_enter_to_submit.pyが検証している、送信キー
    (Enter/Shift+Enter)・非常口(Ctrl+C2回連続)の案内文言は、見た目の
    刷新後もそのまま残っていることを確認する(回帰検知)。
    """
    banner = yoriai._format_startup_banner(".", None, use_color=False)
    assert "Enterで送信" in banner
    assert "Shift+Enterで改行" in banner
    assert "Ctrl+Cを2秒以内に2回連続で押す" in banner


def test_banner_mentions_background_execution():
    banner = yoriai._format_startup_banner(".", None, use_color=False)
    assert "バックグラウンド" in banner


def main():
    tests = [
        test_supports_ansi_color_is_false_when_no_color_env_set,
        test_supports_ansi_color_is_false_for_dumb_terminal,
        test_supports_ansi_color_is_false_when_stdout_is_not_a_tty,
        test_ansi_wraps_text_only_when_color_enabled,
        test_ansi_without_codes_returns_plain_text_even_when_color_enabled,
        test_format_member_count_line_shows_count_including_self,
        test_format_member_count_line_handles_unknown_count,
        test_count_org_members_counts_self_plus_peers,
        test_count_org_members_returns_none_when_snapshot_unavailable,
        test_banner_stays_within_80_columns,
        test_banner_without_color_contains_no_ansi_escape_codes,
        test_banner_with_color_contains_ansi_escape_codes,
        test_banner_includes_logo_and_member_count,
        test_banner_includes_all_commands,
        test_banner_still_documents_required_key_bindings_and_emergency_exit,
        test_banner_mentions_background_execution,
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
