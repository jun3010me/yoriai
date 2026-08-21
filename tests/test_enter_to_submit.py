#!/usr/bin/env python3
"""対話モードの送信キーを「Enterで送信、Shift+Enterで改行」に変更した
ことを検証する。

以前の送信キー(Alt+Enter)は、実機(Windows機からraspi4へのSSH接続)で
SSHクライアント側の「ウィンドウの最大化/復元」ショートカットに奪われ、
Yoriaiに一切届かない不具合が報告された。この対策として、Claude Code等の
他のツールと同様の配置(Enterで送信、Shift+Enterで改行)に変更した。

技術的な制約(依頼で明示的に許容された既知のリスク): prompt_toolkit
(3.0.53時点)には「Shift+Enter」を表す固有のキー名が存在しない。多くの
端末がShift+Enterに対して送るxterm modifyOtherKeys形式のエスケープ
シーケンス(`\\x1b[27;2;13~`)は、prompt_toolkitの入力パーサー自体が
キー割り当ての仕組みに渡す前の時点で素のEnterと同じ`Keys.ControlM`に
変換してしまうため、通常のキー割り当てAPIでは区別できない。
`yoriai._make_repl_key_bindings`は、Enterキーの共通ハンドラ内で
`KeyPress.data`(変換前の生バイト列)を調べることでこの2つを区別する。
このシーケンスをそのまま送ってこない端末では、Shift+Enterも単に
Enterと同様に送信として扱われる(改行を挿入したい場面で送信されて
しまう)ことが、依頼で明示的に許容された既知の制約である。この
劣化時の挙動(区別できない場合は送信として扱われる)自体もテストで
固定する。

`yoriai._DoubleInterruptGuard`によるCtrl+C2回連続での「非常口」
(tests/test_double_ctrl_c_emergency_exit.py参照)は、送信キーの変更
とは独立した仕組みであり、この変更後も引き続き機能する必要がある
(依頼の要件3)。このファイルでも、送信キー変更後に非常口が壊れて
いないことを回帰検知する。

使い方: python3 tests/test_enter_to_submit.py
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"  # Enterキー単体
_SHIFT_ENTER = "\x1b[27;2;13~"  # 一部の端末がShift+Enterに対して送るバイト列
_OLD_SUBMIT_KEY_ALT_ENTER = "\x1b\r"  # 旧・送信キー(Alt+Enter)


def _read_with_keys(keystrokes: str):
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text(keystrokes)
        return yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())


def test_enter_submits_a_single_line_message():
    """依頼の要件1: Enterキー単体を送信に割り当てたことを確認する。"""
    text, terminate = _read_with_keys("富士山の標高は?" + _SUBMIT)
    assert terminate is False
    assert text == "富士山の標高は?", repr(text)


def test_shift_enter_inserts_a_newline_and_enter_then_submits():
    """依頼の要件1・2: Shift+Enterで改行を挿入し、その後のEnterで
    複数行のメッセージ全体が送信されることを確認する。
    """
    text, terminate = _read_with_keys(
        "1行目" + _SHIFT_ENTER + "2行目" + _SHIFT_ENTER + "3行目" + _SUBMIT
    )
    assert terminate is False
    assert text == "1行目\n2行目\n3行目", repr(text)


def test_terminal_that_cannot_distinguish_shift_enter_degrades_to_submit():
    """依頼の「既知のリスクと、確認してほしいこと」への回答: Shift+Enterの
    生バイト列を送ってこない端末(=素のEnterと見分けが付かない端末)では、
    Shift+Enterのつもりで押しても単に送信されてしまうことを確認する
    (依頼で明示的に許容された劣化時の挙動)。ここでは、素のEnter
    (`\\r`)を2回送ることでこの状況を再現する: 1回目のEnterで「1行目」が
    確定・送信され、2回目の`_read_multiline_input`呼び出しで「2行目」が
    別のメッセージとして送信される(1つの複数行メッセージにはならない)。
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text("1行目" + _SUBMIT + "2行目" + _SUBMIT)
        first_text, first_terminate = yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())
        second_text, second_terminate = yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())
    assert first_terminate is False
    assert first_text == "1行目", repr(first_text)
    assert second_terminate is False
    assert second_text == "2行目", repr(second_text)


def test_old_alt_enter_submit_key_no_longer_has_special_meaning():
    """旧・送信キー(Alt+Enter)は、キー割り当てとしては特別扱いしなく
    なったことを確認する。Escapeキーに続けてEnterを送った場合、まず
    Escapeキー単体の(既定の)処理が行われ、続くEnterで送信される。
    通常の文字入力の後にAlt+Enterを送っても、それまでに入力した内容が
    そのまま送信されることを確認する(新しい割り当てに実害がないことの
    回帰検知)。
    """
    text, terminate = _read_with_keys("こんにちは" + _OLD_SUBMIT_KEY_ALT_ENTER)
    assert terminate is False
    assert text == "こんにちは", repr(text)


def test_double_ctrl_c_emergency_exit_still_works_after_the_key_change():
    """依頼の要件3(最優先): 送信キーをEnter/Shift+Enterに変更した後も、
    Ctrl+Cを2秒以内に2回連続で押すと確実に対話モードを終了できる
    「非常口」が引き続き機能することを確認する(詳細な検証は
    tests/test_double_ctrl_c_emergency_exit.pyで行っている。ここでは
    今回の変更による回帰が無いことをこのファイル内でも確認する)。
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text("送信キーが効かない状況を想定した入力\x03\x03")
        text, terminate = yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())
    assert terminate is True
    assert text == ""


def test_exit_and_quit_still_terminate_normally():
    """依頼の要件4: 既存のexit/quitによる終了方法が引き続き使えることを
    確認する。
    """
    for word in ("exit", "quit"):
        text, terminate = _read_with_keys(word + _SUBMIT)
        assert terminate is True, word


def test_eof_ctrl_d_still_terminates_normally():
    """依頼の要件4: 既存のCtrl+D(EOF)による終了方法が引き続き使える
    ことを確認する。
    """
    text, terminate = _read_with_keys("\x04")
    assert terminate is True


def test_banner_documents_the_new_keys_prominently():
    """依頼の要件5: 起動時の案内文が新しいキー割り当て(Enterで送信、
    Shift+Enterで改行)に合わせて更新されていることを確認する。
    """
    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        original_create_session = yoriai._create_repl_prompt_session
        yoriai._create_repl_prompt_session = fake_create_session
        try:
            pipe_input.send_text("\x04")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", ".")
        finally:
            yoriai._create_repl_prompt_session = original_create_session

    output = buf.getvalue()
    assert "Enterで送信" in output, output
    assert "Shift+Enterで改行" in output, output
    assert "Ctrl+Cを2秒以内に2回連続で押す" in output, output


def main():
    tests = [
        test_enter_submits_a_single_line_message,
        test_shift_enter_inserts_a_newline_and_enter_then_submits,
        test_terminal_that_cannot_distinguish_shift_enter_degrades_to_submit,
        test_old_alt_enter_submit_key_no_longer_has_special_meaning,
        test_double_ctrl_c_emergency_exit_still_works_after_the_key_change,
        test_exit_and_quit_still_terminate_normally,
        test_eof_ctrl_d_still_terminates_normally,
        test_banner_documents_the_new_keys_prominently,
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
