#!/usr/bin/env python3
"""対話モード(`--chat`)のREPL(`_run_repl_client`)が、空入力・終了コマンドを
最優先で正しく処理することを検証する。

prompt_toolkit採用(複数行入力のテキストエディタ化)に伴い、
`_run_repl_client`は`input()`ではなく`_create_repl_prompt_session()`が
作る`PromptSession`経由でメッセージを受け取るようになった。このテストでは
`yoriai._create_repl_prompt_session`を差し替え、`prompt_toolkit.input.
create_pipe_input`で仮想的なキー入力を送り込んだセッションを返すようにする
ことで、`_run_repl_client`全体を実際に動かして検証する(`input()`を
差し替えていた以前の手法から変更)。

`_classify_execution_mode`・`_ask_organization`系の関数が「呼ばれない
こと」まで確認することで、空入力が実際にはモード判定にすら進んでいない
ことを保証する。

使い方: python3 tests/test_repl_input_handling.py
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

# Alt+Enter(端末的にはEscapeキーに続けてEnter)を送信する際のバイト列。
_SUBMIT = "\x1b\r"


def _run_repl_with_keys(keystrokes: str):
    """指定したキー入力全体を仮想端末に送り込んで`_run_repl_client`を
    実際に走らせ、標準出力と「モード判定・問い合わせ系関数が呼ばれた
    回数」を返す。
    """
    calls = {"classify": 0, "ask_single": 0, "ask_multi": 0, "ask_collaborate": 0}

    original_create_session = yoriai._create_repl_prompt_session
    original_classify = yoriai._classify_execution_mode
    original_ask = yoriai._ask_organization
    original_ask_multi = yoriai._ask_organization_multi
    original_ask_collaborate = yoriai._ask_organization_collaborate

    def spy_classify(text):
        calls["classify"] += 1
        return original_classify(text)

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1

    def stub_ask_multi(*args, **kwargs):
        calls["ask_multi"] += 1

    def stub_ask_collaborate(*args, **kwargs):
        calls["ask_collaborate"] += 1

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(input=pipe_input, output=DummyOutput())

        yoriai._create_repl_prompt_session = fake_create_session
        yoriai._classify_execution_mode = spy_classify
        yoriai._ask_organization = stub_ask
        yoriai._ask_organization_multi = stub_ask_multi
        yoriai._ask_organization_collaborate = stub_ask_collaborate

        pipe_input.send_text(keystrokes)

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", ".")
        finally:
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._classify_execution_mode = original_classify
            yoriai._ask_organization = original_ask
            yoriai._ask_organization_multi = original_ask_multi
            yoriai._ask_organization_collaborate = original_ask_collaborate

    return buf.getvalue(), calls


def test_empty_submission_does_nothing_and_reprompts():
    """何も入力せずAlt+Enterのみ押した場合、何も処理されず(モード判定にすら
    進まず)、続けて次の入力を待つことを確認する。
    """
    output, calls = _run_repl_with_keys(_SUBMIT + "exit" + _SUBMIT)
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, (
        f"空入力なのに何らかの問い合わせ処理が呼ばれています: {calls}"
    )
    assert "対話モードを終了します。" in output, output


def test_whitespace_only_submission_does_nothing():
    """空白のみの送信も、空の送信と同様に何もしないことを確認する。"""
    output, calls = _run_repl_with_keys("   " + _SUBMIT + "exit" + _SUBMIT)
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, calls
    assert "対話モードを終了します。" in output, output


def test_exit_terminates_immediately():
    output, calls = _run_repl_with_keys("exit" + _SUBMIT)
    assert calls["classify"] == 0, "exitはモード判定に進まず即座に終了するはずです"
    assert "対話モードを終了します。" in output, output


def test_exit_with_surrounding_whitespace_terminates():
    """前後に空白がある'exit'でも終了することを確認する。"""
    output, calls = _run_repl_with_keys("  exit  " + _SUBMIT)
    assert calls["classify"] == 0, calls
    assert "対話モードを終了します。" in output, output


def test_quit_terminates_immediately():
    output, calls = _run_repl_with_keys("quit" + _SUBMIT)
    assert calls["classify"] == 0, calls
    assert "対話モードを終了します。" in output, output


def test_exit_and_quit_are_case_insensitive():
    for word in ("EXIT", "Exit", "QUIT", "Quit"):
        output, calls = _run_repl_with_keys(word + _SUBMIT)
        assert calls["classify"] == 0, f"{word!r}: {calls}"
        assert "対話モードを終了します。" in output, f"{word!r}: {output}"


def test_normal_input_still_reaches_mode_classification():
    """空入力・終了コマンド以外の通常の入力は、これまで通りモード判定に
    進むことを確認する(回帰検知: 判定ロジックそのものを壊していないか)。
    """
    output, calls = _run_repl_with_keys("富士山の標高は?" + _SUBMIT + "exit" + _SUBMIT)
    assert calls["classify"] == 1, calls
    assert calls["ask_single"] == 1, calls
    assert "[判断:" in output, output


def test_eof_terminates_the_repl():
    """Ctrl+D(EOF)で対話モードが終了することを確認する。"""
    output, calls = _run_repl_with_keys("\x04")
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, calls
    assert "対話モードを終了します。" in output, output


def main():
    tests = [
        test_empty_submission_does_nothing_and_reprompts,
        test_whitespace_only_submission_does_nothing,
        test_exit_terminates_immediately,
        test_exit_with_surrounding_whitespace_terminates,
        test_quit_terminates_immediately,
        test_exit_and_quit_are_case_insensitive,
        test_normal_input_still_reaches_mode_classification,
        test_eof_terminates_the_repl,
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
