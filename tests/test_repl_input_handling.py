#!/usr/bin/env python3
"""対話モード(`--chat`)のREPL(`_run_repl_client`)が、空入力・終了コマンドを
最優先で正しく処理することを検証する。

実機から「Enterのみ押すと空文字列がそのまま質問として送信されてしまう」
「'exit'と入力しても終了せず通常の質問として処理されることがある」という
バグ報告があった。コードを確認したところ、`_run_repl_client`内では
入力直後・モード自動判定より前に

    text = input("Yoriai> ").strip()
    ...
    if not text:
        continue
    if text.lower() in ("exit", "quit"):
        break

という判定が既に最優先で行われており、コード上は正しい実装になっている
ことを確認した。本テストはこれを実際に動かして裏付けるとともに、今後の
変更でこの判定順序が崩れないようにする回帰テストとして追加する
(過去に「実機ではコードが直っているはずの挙動が再現する」という報告が
複数回あり、原因は常駐プロセス側の再起動漏れだったことが多い。同様の
可能性を疑う場合は、`--chat`を起動しているプロセスが最新の`yoriai.py`を
使っているか確認すること)。

`input()`をビルトインごと差し替え、`_classify_execution_mode`・
`_ask_organization`系の関数が「呼ばれないこと」まで確認することで、
空入力が実際にはモード判定にすら進んでいないことを保証する。

使い方: python3 tests/test_repl_input_handling.py
"""
import builtins
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _run_repl_with_inputs(inputs):
    """指定した入力列を順番に返す`input()`でREPLを走らせ、標準出力と
    「モード判定・問い合わせ系関数が呼ばれた回数」を返す。
    """
    it = iter(inputs)

    def fake_input(prompt=""):
        return next(it)

    calls = {"classify": 0, "ask_single": 0, "ask_multi": 0, "ask_collaborate": 0}

    original_input = builtins.input
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

    builtins.input = fake_input
    yoriai._classify_execution_mode = spy_classify
    yoriai._ask_organization = stub_ask
    yoriai._ask_organization_multi = stub_ask_multi
    yoriai._ask_organization_collaborate = stub_ask_collaborate

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yoriai._run_repl_client(47120, "fingerprint", ".")
    finally:
        builtins.input = original_input
        yoriai._classify_execution_mode = original_classify
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_multi = original_ask_multi
        yoriai._ask_organization_collaborate = original_ask_collaborate

    return buf.getvalue(), calls


def test_enter_only_does_nothing_and_reprompts():
    """何も入力せずEnterのみ押した場合、何も処理されず(モード判定にすら
    進まず)、続けて次の入力を待つことを確認する。
    """
    output, calls = _run_repl_with_inputs(["", "exit"])
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, (
        f"空入力なのに何らかの問い合わせ処理が呼ばれています: {calls}"
    )
    assert "対話モードを終了します。" in output, output


def test_whitespace_only_input_does_nothing():
    """空白のみの入力も、空文字列と同様に何もしないことを確認する。"""
    output, calls = _run_repl_with_inputs(["   ", "\t", "exit"])
    assert calls == {"ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "classify": 0}, calls
    assert "対話モードを終了します。" in output, output


def test_exit_terminates_immediately():
    output, calls = _run_repl_with_inputs(["exit"])
    assert calls["classify"] == 0, "exitはモード判定に進まず即座に終了するはずです"
    assert "対話モードを終了します。" in output, output


def test_exit_with_surrounding_whitespace_terminates():
    """前後に空白がある'exit'でも終了することを確認する。"""
    output, calls = _run_repl_with_inputs(["  exit  "])
    assert calls["classify"] == 0, calls
    assert "対話モードを終了します。" in output, output


def test_quit_terminates_immediately():
    output, calls = _run_repl_with_inputs(["quit"])
    assert calls["classify"] == 0, calls
    assert "対話モードを終了します。" in output, output


def test_exit_and_quit_are_case_insensitive():
    for word in ("EXIT", "Exit", "QUIT", "Quit"):
        output, calls = _run_repl_with_inputs([word])
        assert calls["classify"] == 0, f"{word!r}: {calls}"
        assert "対話モードを終了します。" in output, f"{word!r}: {output}"


def test_normal_input_still_reaches_mode_classification():
    """空入力・終了コマンド以外の通常の入力は、これまで通りモード判定に
    進むことを確認する(回帰検知: 判定ロジックそのものを壊していないか)。
    複数行入力の仕様(tests/test_multiline_input.py参照)により、1行だけの
    質問も空行での確定操作が必要になった点に合わせて入力列を調整した。
    """
    output, calls = _run_repl_with_inputs(["富士山の標高は?", "", "exit"])
    assert calls["classify"] == 1, calls
    assert calls["ask_single"] == 1, calls
    assert "[判断:" in output, output


def main():
    tests = [
        test_enter_only_does_nothing_and_reprompts,
        test_whitespace_only_input_does_nothing,
        test_exit_terminates_immediately,
        test_exit_with_surrounding_whitespace_terminates,
        test_quit_terminates_immediately,
        test_exit_and_quit_are_case_insensitive,
        test_normal_input_still_reaches_mode_classification,
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
