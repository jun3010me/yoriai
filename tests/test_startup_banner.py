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
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"  # Enterキー単体(送信)


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
    状態で、実装本体と同じ`yoriai._display_width`を使う)。
    """
    banner = yoriai._format_startup_banner(".", 3, use_color=False)
    for line in banner.splitlines():
        width = yoriai._display_width(line)
        assert width <= 80, f"80文字を超える行があります({width}): {line!r}"


def test_display_width_treats_box_drawing_characters_as_half_width():
    """実機で報告された不具合の再現確認: 罫線素片(╔╗╚╝═║等)を、
    日本語の全角文字と同様に幅2として数えてしまうと、実際のターミナル
    描画幅より過大に見積もってしまう(この不具合により、ロゴの箱の
    罫線行が実際には80文字に収まっているにもかかわらず80文字超過と
    誤検知されていた)。罫線素片はターミナル上では半角1文字分の幅で
    描画されることを確認する。
    """
    assert yoriai._display_width("╔") == 1
    assert yoriai._display_width("═" * 10) == 10
    assert yoriai._display_width("║") == 1


def test_display_width_treats_japanese_text_as_full_width():
    assert yoriai._display_width("寄合") == 4
    assert yoriai._display_width("ローカル") == 8


def test_logo_lines_all_stay_within_80_columns():
    """`_format_logo_lines`(figlet風のブロック体ワードマーク)の各行が、
    ターミナルの標準的な横幅(80文字)に収まっていることを確認する。
    """
    lines = yoriai._format_logo_lines(use_color=False)
    for line in lines:
        width = yoriai._display_width(line)
        assert width <= 80, f"80文字を超える行があります({width}): {line!r}"


def test_banner_without_color_contains_no_ansi_escape_codes():
    banner = yoriai._format_startup_banner(".", 3, use_color=False)
    assert "\x1b[" not in banner


def test_banner_with_color_contains_ansi_escape_codes():
    banner = yoriai._format_startup_banner(".", 3, use_color=True)
    assert "\x1b[" in banner


def test_banner_includes_logo_and_member_count():
    banner = yoriai._format_startup_banner(".", 2, use_color=False)
    assert "GATHER" in banner.replace(" ", "")
    assert "CREATE" in banner.replace(" ", "")
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


def test_supports_ansi_color_is_false_when_term_is_unset():
    """依頼の要件(問題1・3): 対応状況が判定できない場合は誤検知を避け、
    安全側(色を使わない)に倒すことを確認する。`TERM`環境変数が未設定・
    空の場合、色付けに対応しているかどうかを判定する材料が無いため、
    無効にする。
    """
    original_no_color = os.environ.pop("NO_COLOR", None)
    original_term = os.environ.pop("TERM", None)
    try:
        assert yoriai._supports_ansi_color() is False
    finally:
        if original_no_color is not None:
            os.environ["NO_COLOR"] = original_no_color
        if original_term is not None:
            os.environ["TERM"] = original_term


class _FakeTtyStdout:
    """`isatty()`が真を返す、実際の対話的端末を模擬する偽の標準出力。

    仮の判断: このテストスイート全体で使っている`contextlib.
    redirect_stdout(io.StringIO())`では`isatty()`が常に偽になるため、
    `patch_stdout()`が内部で使う`prompt_toolkit`の`Output`実装は
    (ttyではない、と判定されて)ESC文字を`?`に置換しない`PlainTextOutput`が
    選ばれてしまい、実機で報告された不具合(`Vt100_Output`だけで起きる
    ESC文字の`?`置換)を再現できない。このクラスで`isatty()`が真になる
    標準出力を模擬することで、`_run_repl_client`内部の`create_app_session()`
    が実際に`Vt100_Output`を選ぶようにし、実機と同じ経路で検証する。
    """

    def __init__(self):
        self._parts = []

    def write(self, data: str) -> int:
        self._parts.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1

    encoding = "utf-8"

    def getvalue(self) -> str:
        return "".join(self._parts)


def _run_repl_with_fake_tty_stdout(keystrokes: str) -> str:
    """`_run_repl_client`を、`isatty()`が真を返す偽の標準出力の上で
    実際に走らせ、出力全体(バイト列ではなく文字列として蓄積したもの)を
    返す。`_supports_ansi_color()`は差し替えず、実際の判定ロジックに
    このtty模擬の標準出力を見せることで、色付けが有効になる状況を
    実機に忠実な形で再現する。
    """
    fake_stdout = _FakeTtyStdout()
    original_stdout = sys.stdout
    original_create_session = yoriai._create_repl_prompt_session

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        yoriai._create_repl_prompt_session = fake_create_session
        pipe_input.send_text(keystrokes)

        sys.stdout = fake_stdout
        out_dir = tempfile.mkdtemp(prefix="yoriai_startup_banner_test_")
        try:
            yoriai._run_repl_client(47120, "fingerprint", out_dir)
        finally:
            sys.stdout = original_stdout
            yoriai._create_repl_prompt_session = original_create_session
            shutil.rmtree(out_dir, ignore_errors=True)

    return fake_stdout.getvalue()


def test_run_repl_client_does_not_corrupt_ansi_codes_in_the_banner():
    """実機で報告された不具合の再現・修正確認: 対話モードを実際に起動する
    と(`isatty()`が真を返す、対応した端末を模擬すると)、起動時の案内文の
    ANSIエスケープシーケンスが`?[1m?[36m`のような文字列に化けずに、
    正しく`\\x1b[`というエスケープシーケンスのまま出力されることを
    確認する。

    原因は、`patch_stdout()`が既定(`raw=False`)では、意図せず端末制御
    シーケンスを注入しないための安全策として、書き込むテキストの
    ESC文字(`\\x1b`)を`?`に置換してしまうことだった
    (`prompt_toolkit.output.vt100.Vt100_Output.write`)。この安全策は
    LLMの応答のような信頼できない出力には有用なため無効にはせず、
    案内文(Yoriai自身が用意した固定テキスト)だけを`patch_stdout()`に
    入る前に出力するよう修正した(`_run_repl_client`参照)。この修正前は
    このテストが実際に失敗していたことを確認済み(`?[1m`が出力に含まれて
    いた)。
    """
    output = _run_repl_with_fake_tty_stdout("exit" + _SUBMIT)
    assert "\x1b[1m" in output, (
        f"色付けに対応した端末を模擬したにもかかわらずANSIエスケープシーケンスが出力に含まれていません: {output!r}"
    )
    assert "?[1m" not in output and "?[36m" not in output, (
        f"ANSIエスケープシーケンスが文字化けしています(実機で報告された不具合の再現): {output!r}"
    )


def test_run_repl_client_banner_has_no_escape_codes_on_non_tty_output():
    """テストスイート全体で使っている`contextlib.redirect_stdout`
    (非tty)の環境下では、`_supports_ansi_color()`の実際の判定ロジックに
    より色付けが自動的に無効化され、ANSIエスケープシーケンスが一切
    出力に含まれないことを確認する。
    """
    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        yoriai._create_repl_prompt_session = fake_create_session
        pipe_input.send_text("exit" + _SUBMIT)

        buf = io.StringIO()
        out_dir = tempfile.mkdtemp(prefix="yoriai_startup_banner_test_")
        try:
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", out_dir)
        finally:
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._create_background_job_runner = original_create_runner
            shutil.rmtree(out_dir, ignore_errors=True)

    assert "\x1b[" not in buf.getvalue()


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
        test_display_width_treats_box_drawing_characters_as_half_width,
        test_display_width_treats_japanese_text_as_full_width,
        test_logo_lines_all_stay_within_80_columns,
        test_banner_without_color_contains_no_ansi_escape_codes,
        test_banner_with_color_contains_ansi_escape_codes,
        test_banner_includes_logo_and_member_count,
        test_banner_includes_all_commands,
        test_banner_still_documents_required_key_bindings_and_emergency_exit,
        test_banner_mentions_background_execution,
        test_supports_ansi_color_is_false_when_term_is_unset,
        test_run_repl_client_does_not_corrupt_ansi_codes_in_the_banner,
        test_run_repl_client_banner_has_no_escape_codes_on_non_tty_output,
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
