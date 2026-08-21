#!/usr/bin/env python3
"""対話モード(`--chat`)の複数行入力(`_read_multiline_input`)を検証する。

以前は「1行入力してEnter→空行で送信」という、`readline`の1行単位の
編集に依存した仕様だった。これには、上下矢印キーで前の行に戻って編集
できない(readlineは1行内のカーソル移動にしか対応していない)という
原理的な制約があり、実機からも複数行にまたがる入力の改行部分に制御
文字と思われる記号が表示されるという報告があった。

`prompt_toolkit`のmultiline対応セッションを採用し、複数行の入力全体を
1つの編集可能な領域として扱うようにした。標準のキー割り当てにより、
Enterは常に改行を挿入し、Alt+Enter(Escキーに続けてEnter)で入力を
確定する(空行での送信という以前の仕様は、この変更に伴い廃止した:
自由に編集できる複数行バッファでは、途中に意図的な空行=段落区切りを
入れられる必要があるため)。上下矢印キーによる行間移動そのものの検証は
tests/test_repl_line_editing.pyで行う。このファイルでは、
`_read_multiline_input`が返す`(text, terminate)`の契約(送信内容・
終了判定)を検証する。

`prompt_toolkit.input.create_pipe_input`で仮想的なキー入力を送り込み、
`prompt_toolkit.output.DummyOutput`で実際の画面描画を行わないように
した`PromptSession`を使う(prompt_toolkit公式のテスト手法。実際の
擬似端末を使わずに済む)。

使い方: python3 tests/test_multiline_input.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

# Alt+Enter(端末的にはEscapeキーに続けてEnter)を送信する際のバイト列。
_SUBMIT = "\x1b\r"


def _read_with_keys(keystrokes: str):
    """指定したキー入力(複数回分の`_read_multiline_input`呼び出しを
    またいでよい)を仮想端末に送り込み、1回分の`_read_multiline_input`の
    戻り値を返す。
    """
    with create_pipe_input() as pipe_input:
        session = PromptSession(input=pipe_input, output=DummyOutput())
        pipe_input.send_text(keystrokes)
        return yoriai._read_multiline_input(session)


def test_single_line_then_submit_key_sends_as_one_message():
    """1行だけ書いてAlt+Enterで、正しく1つのメッセージとして送信される
    ことを確認する。
    """
    text, terminate = _read_with_keys("富士山の標高は?" + _SUBMIT)
    assert terminate is False
    assert text == "富士山の標高は?", repr(text)


def test_multiple_lines_typed_then_submit_key_are_joined():
    """複数行の文章を1行ずつ手打ちし(Enterで改行を挿入)、最後に
    Alt+Enterで送信されることを確認する。
    """
    text, terminate = _read_with_keys(
        "以下の内容でプロジェクトを作って:\r"
        "1. storage.pyでデータを保存する\r"
        "2. cli.pyでコマンドライン操作を行う"
        + _SUBMIT
    )
    assert terminate is False
    assert text == (
        "以下の内容でプロジェクトを作って:\n"
        "1. storage.pyでデータを保存する\n"
        "2. cli.pyでコマンドライン操作を行う"
    ), repr(text)


def test_pasted_multiline_text_delivered_as_rapid_successive_keys():
    """エディタ等から複数行の文章をコピーして貼り付けた場合を再現する。
    貼り付けは実装上、端末から見ると高速な連続キー入力(行ごとに改行が
    Enterとして解釈される)として届く。
    """
    pasted = (
        "ToDoリストのCLIツールを作って。\r"
        "storage.pyでデータの保存・読み込みを、\r"
        "cli.pyでコマンドライン操作を担当する形にしたい。"
    )
    text, terminate = _read_with_keys(pasted + _SUBMIT)
    assert terminate is False
    assert text == (
        "ToDoリストのCLIツールを作って。\n"
        "storage.pyでデータの保存・読み込みを、\n"
        "cli.pyでコマンドライン操作を担当する形にしたい。"
    ), repr(text)


def test_blank_line_no_longer_sends_and_can_be_part_of_the_message():
    """依頼の設計変更: 以前は空行が送信のトリガーだったが、現在は
    Alt+Enterのみが送信のトリガーであり、空行(段落区切り)はメッセージ
    本文の一部としてそのまま含められることを確認する。
    """
    text, terminate = _read_with_keys("1段落目\r\r2段落目" + _SUBMIT)
    assert terminate is False
    assert text == "1段落目\n\n2段落目", repr(text)


def test_exit_alone_terminates():
    text, terminate = _read_with_keys("exit" + _SUBMIT)
    assert terminate is True
    assert text == ""


def test_quit_alone_terminates():
    text, terminate = _read_with_keys("quit" + _SUBMIT)
    assert terminate is True


def test_exit_with_surrounding_whitespace_terminates():
    text, terminate = _read_with_keys("  exit  " + _SUBMIT)
    assert terminate is True


def test_exit_is_case_insensitive():
    for word in ("EXIT", "Exit", "QUIT", "Quit"):
        text, terminate = _read_with_keys(word + _SUBMIT)
        assert terminate is True, word


def test_exit_as_part_of_a_longer_message_is_treated_as_message_content():
    """'exit'が単独の送信内容ではなく、他の行と一緒に送信された場合は、
    メッセージ本文の一部として扱われることを確認する。
    """
    text, terminate = _read_with_keys("以下の手順を実行して:\rexit" + _SUBMIT)
    assert terminate is False
    assert text == "以下の手順を実行して:\nexit", repr(text)


def test_empty_submission_does_nothing_and_waits_for_more_input():
    """何も入力せずにAlt+Enterを押した場合、何もせず次の入力を待ち、
    続けて入力した内容が送信されることを確認する。
    """
    text, terminate = _read_with_keys(_SUBMIT + "こんにちは" + _SUBMIT)
    assert terminate is False
    assert text == "こんにちは", repr(text)


def test_whitespace_only_submission_does_nothing():
    """空白のみの送信も、空の送信と同様に何もしないことを確認する。"""
    text, terminate = _read_with_keys("   " + _SUBMIT + "こんにちは" + _SUBMIT)
    assert terminate is False
    assert text == "こんにちは", repr(text)


def test_eof_terminates():
    """Ctrl+D(EOF)で対話モードの終了を示す(terminate=True)ことを確認する。"""
    text, terminate = _read_with_keys("\x04")
    assert terminate is True


def test_ctrl_c_discards_input_and_waits_for_more_input():
    """Ctrl+Cは、入力済みの内容(バッファに何か入っているかどうかに
    関わらず)を破棄して新しい入力待ちに戻ることを確認する(対話モード
    自体は終了しない)。
    """
    text, terminate = _read_with_keys("途中まで書いた文章\x03こんにちは" + _SUBMIT)
    assert terminate is False
    assert text == "こんにちは", repr(text)


def main():
    tests = [
        test_single_line_then_submit_key_sends_as_one_message,
        test_multiple_lines_typed_then_submit_key_are_joined,
        test_pasted_multiline_text_delivered_as_rapid_successive_keys,
        test_blank_line_no_longer_sends_and_can_be_part_of_the_message,
        test_exit_alone_terminates,
        test_quit_alone_terminates,
        test_exit_with_surrounding_whitespace_terminates,
        test_exit_is_case_insensitive,
        test_exit_as_part_of_a_longer_message_is_treated_as_message_content,
        test_empty_submission_does_nothing_and_waits_for_more_input,
        test_whitespace_only_submission_does_nothing,
        test_eof_terminates,
        test_ctrl_c_discards_input_and_waits_for_more_input,
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
