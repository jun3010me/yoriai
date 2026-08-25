#!/usr/bin/env python3
"""対話モードの「非常口」機能(Ctrl+Cを2回連続で押すと確実に終了できる)を
検証する。

実機で、Windows機からraspi4にSSH接続して対話モードを操作したところ、
送信キー(Alt+Enter)がSSHクライアント側(Windows側のターミナルソフト)の
「ウィンドウの最大化/復元」ショートカットとして奪われ、Yoriaiに一切
届かない不具合が報告された。Ctrl+Enterも試されたが、Windowsのネイティブ
コンソールと異なりSSH経由の接続では「Ctrl+EnterをAlt+Enterに変換する」
仕組みが適用されないため機能しなかった。結果として、exit/quit・Ctrl+D・
Ctrl+Cのいずれも反応せず、対話モードから一切抜け出せなくなり、最終的に
別セッションからのpkillで強制終了する必要があった。

この対策として、新しい送信キーの選定は行わず、Ctrl+C(SIGINT、SSH接続を
含むどのような端末経由でも標準的に転送される割り込み)を2秒以内に2回
連続で押すことを検出する「非常口」を実装した。送信キーの状態に一切
関係なく、入力編集中・組織への問い合わせ中(応答待ち)のどちらでも
確実に対話モードを終了できる(`_DoubleInterruptGuard`)。

使い方: python3 tests/test_double_ctrl_c_emergency_exit.py
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

# Enterキー単体(送信)を送信する際のバイト列。
_SUBMIT = "\r"


def test_double_interrupt_guard_does_not_trigger_on_first_press():
    guard = yoriai._DoubleInterruptGuard()
    assert guard.note_interrupt() is False


def test_double_interrupt_guard_triggers_on_immediate_second_press():
    guard = yoriai._DoubleInterruptGuard()
    assert guard.note_interrupt() is False
    assert guard.note_interrupt() is True, "2秒以内の2回目のCtrl+Cは検出されるはずです"


def test_double_interrupt_guard_does_not_trigger_after_window_elapses():
    """2回のCtrl+Cの間隔が上限(既定2秒)を超えている場合は、それぞれ
    独立した「1回目」として扱われ、即座には終了と判定されないことを
    確認する。`time.monotonic`を差し替えて経過時間を模擬する。
    """
    fake_now = {"t": 1000.0}
    original_monotonic = yoriai.time.monotonic
    yoriai.time.monotonic = lambda: fake_now["t"]
    try:
        guard = yoriai._DoubleInterruptGuard(window_sec=2.0)
        assert guard.note_interrupt() is False
        fake_now["t"] += 3.0  # 上限(2秒)を超えて経過させる
        assert guard.note_interrupt() is False, "上限を超えた間隔では2回連続とみなされないはずです"
        assert guard.note_interrupt() is True, "直後のもう一度は検出されるはずです"
    finally:
        yoriai.time.monotonic = original_monotonic


def test_handle_repl_command_interrupt_returns_false_on_first_press():
    guard = yoriai._DoubleInterruptGuard()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = yoriai._handle_repl_command_interrupt(guard)
    assert result is False
    assert "もう一度Ctrl+C" in buf.getvalue(), buf.getvalue()


def test_handle_repl_command_interrupt_returns_true_on_second_press():
    guard = yoriai._DoubleInterruptGuard()
    guard.note_interrupt()  # 1回目
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = yoriai._handle_repl_command_interrupt(guard)
    assert result is True
    assert "2回連続" in buf.getvalue(), buf.getvalue()


def test_read_multiline_input_terminates_on_double_ctrl_c():
    """入力編集中に(送信キーを一切使わず)Ctrl+Cを2回連続で押すだけで、
    `_read_multiline_input`が終了を示す`("", True)`を返すことを確認する。
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text("\x03\x03")
        text, terminate = yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())
    assert terminate is True
    assert text == ""


def test_read_multiline_input_single_ctrl_c_does_not_terminate():
    """Ctrl+Cが1回だけの場合は、これまで通り入力を破棄するだけで対話
    モードは終了しないことを確認する(依頼の動作確認: 既存の中断挙動が
    壊れていないこと)。
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text("\x03こんにちは" + _SUBMIT)
        text, terminate = yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())
    assert terminate is False
    assert text == "こんにちは", repr(text)


def _run_repl_with_keys(keystrokes: str, ask_single_side_effect=None):
    """`tests/test_repl_input_handling.py`と同様、`_run_repl_client`
    全体を実際に走らせる。`ask_single_side_effect`を指定すると、
    `_ask_organization`(単発質問の問い合わせ)が呼ばれた際にその
    関数を実行する(組織への問い合わせ中にCtrl+Cが発生する状況を
    模擬するために使う)。
    """
    calls = {"classify": 0, "ask_single": 0, "ask_multi": 0, "ask_collaborate": 0}

    original_create_session = yoriai._create_repl_prompt_session
    original_classify = yoriai._classify_execution_mode
    original_ask = yoriai._ask_organization
    original_ask_multi = yoriai._ask_organization_multi
    original_ask_collaborate = yoriai._ask_organization_collaborate

    def spy_classify(text, out_dir=None):
        calls["classify"] += 1
        return original_classify(text, out_dir)

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1
        if ask_single_side_effect:
            ask_single_side_effect()

    def stub_ask_multi(*args, **kwargs):
        calls["ask_multi"] += 1

    def stub_ask_collaborate(*args, **kwargs):
        calls["ask_collaborate"] += 1

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        yoriai._create_repl_prompt_session = fake_create_session
        yoriai._classify_execution_mode = spy_classify
        yoriai._ask_organization = stub_ask
        yoriai._ask_organization_multi = stub_ask_multi
        yoriai._ask_organization_collaborate = stub_ask_collaborate

        pipe_input.send_text(keystrokes)

        buf = io.StringIO()
        out_dir = tempfile.mkdtemp(prefix="yoriai_double_ctrl_c_test_")
        try:
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", out_dir)
        finally:
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._classify_execution_mode = original_classify
            yoriai._ask_organization = original_ask
            yoriai._ask_organization_multi = original_ask_multi
            yoriai._ask_organization_collaborate = original_ask_collaborate
            shutil.rmtree(out_dir, ignore_errors=True)

    return buf.getvalue(), calls


def test_repl_terminates_on_double_ctrl_c_during_input_editing():
    """依頼の動作確認: 送信キーが一切機能しない状況を想定し、入力編集中に
    Ctrl+Cを2回連続で押すだけで確実に対話モードが終了することを、
    `_run_repl_client`全体を通して確認する。
    """
    output, calls = _run_repl_with_keys("途中まで入力した文章\x03\x03")
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, (
        f"何も送信されていないため、問い合わせ系関数は呼ばれないはずです: {calls}"
    )
    assert "対話モードを終了します。" in output, output


def test_repl_terminates_on_double_ctrl_c_spanning_response_wait_and_input():
    """依頼の動作確認: 「入力中」「応答待ち」のどちらでCtrl+Cが発生しても、
    2回連続であれば確実に終了することを確認する(状況をまたいだ検出)。
    1回目のCtrl+Cは組織への問い合わせ中(応答待ち)に発生させ、2回目は
    (中断後に戻ってきた)次の入力プロンプトで発生させる。
    """
    def raise_interrupt_once():
        raise KeyboardInterrupt

    output, calls = _run_repl_with_keys(
        "富士山の標高は?" + _SUBMIT + "\x03",
        ask_single_side_effect=raise_interrupt_once,
    )
    assert calls["ask_single"] == 1, calls
    assert "(処理を中断しました。2秒以内にもう一度Ctrl+Cを押すと" in output, output
    assert "[⚠️ Ctrl+Cが2回連続で押されたため、対話モードを終了します]" in output, output
    assert "対話モードを終了します。" in output, output


def test_repl_still_supports_exit_command_normally():
    """依頼の動作確認: 通常の操作(exit入力→送信キー)でも引き続き終了
    できることを確認する(非常口の追加によって既存の終了方法が壊れて
    いないことの回帰検知)。
    """
    output, calls = _run_repl_with_keys("exit" + _SUBMIT)
    assert calls["classify"] == 0, calls
    assert "対話モードを終了します。" in output, output


def test_repl_still_supports_eof_normally():
    """依頼の動作確認: Ctrl+D(EOF)でも引き続き終了できることを確認する。"""
    output, calls = _run_repl_with_keys("\x04")
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, calls
    assert "対話モードを終了します。" in output, output


def main():
    tests = [
        test_double_interrupt_guard_does_not_trigger_on_first_press,
        test_double_interrupt_guard_triggers_on_immediate_second_press,
        test_double_interrupt_guard_does_not_trigger_after_window_elapses,
        test_handle_repl_command_interrupt_returns_false_on_first_press,
        test_handle_repl_command_interrupt_returns_true_on_second_press,
        test_read_multiline_input_terminates_on_double_ctrl_c,
        test_read_multiline_input_single_ctrl_c_does_not_terminate,
        test_repl_terminates_on_double_ctrl_c_during_input_editing,
        test_repl_terminates_on_double_ctrl_c_spanning_response_wait_and_input,
        test_repl_still_supports_exit_command_normally,
        test_repl_still_supports_eof_normally,
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
