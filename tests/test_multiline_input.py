#!/usr/bin/env python3
"""対話モード(`--chat`)の複数行入力サポートを検証する。

これまで「1行=1メッセージ」として即座に処理されていたため、エディタ等
から複数行の文章を貼り付けると、貼り付け内の改行がそれぞれ別々のEnter
入力として扱われ、文章が意図せず分割送信されたり、空行部分が空の質問
として送信されてしまう不具合があった。これをバグ修正ではなく「複数行の
入力を正式にサポートする機能」として実装した:

- 1行入力してEnterを押した時点ではまだ送信しない
- 空行(Enterのみ)が入力された時点で、それまでの行をすべて連結して
  1つのメッセージとして送信する
- "exit"/"quit"は、複数行入力の最初の行として単独で入力された場合のみ
  即座に終了と判定する(継続入力中に出てきた場合はメッセージ本文として
  扱う)

`_read_multiline_input()`を直接呼び出し、`input()`をビルトインごと
差し替えて検証する。依頼の「動作確認」節に挙げられた4パターンすべてを
含む。

使い方: python3 tests/test_multiline_input.py
"""
import builtins
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _feed(inputs):
    it = iter(inputs)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return next(it)

    original = builtins.input
    builtins.input = fake_input
    try:
        result = yoriai._read_multiline_input()
    finally:
        builtins.input = original
    return result, prompts


def test_single_line_then_blank_line_sends_as_one_message():
    """依頼の動作確認1: 1行だけ書いてEnter→もう一度Enterで、
    正しく1つのメッセージとして送信されることを確認する。
    """
    (text, terminate), prompts = _feed(["富士山の標高は?", ""])
    assert terminate is False
    assert text == "富士山の標高は?", repr(text)
    assert prompts == [yoriai._REPL_FIRST_LINE_PROMPT, yoriai._REPL_CONTINUATION_PROMPT], prompts


def test_multiple_lines_typed_then_blank_line_are_joined():
    """依頼の動作確認2: 複数行の文章を1行ずつ手打ちし、最後に空行で
    送信されることを確認する。
    """
    (text, terminate), prompts = _feed([
        "以下の内容でプロジェクトを作って:",
        "1. storage.pyでデータを保存する",
        "2. cli.pyでコマンドライン操作を行う",
        "",
    ])
    assert terminate is False
    assert text == (
        "以下の内容でプロジェクトを作って:\n"
        "1. storage.pyでデータを保存する\n"
        "2. cli.pyでコマンドライン操作を行う"
    ), repr(text)
    # 1行目だけ"Yoriai> "、2行目以降は継続入力プロンプトになっていること。
    assert prompts == [
        yoriai._REPL_FIRST_LINE_PROMPT,
        yoriai._REPL_CONTINUATION_PROMPT,
        yoriai._REPL_CONTINUATION_PROMPT,
        yoriai._REPL_CONTINUATION_PROMPT,
    ], prompts


def test_pasted_multiline_text_delivered_as_rapid_successive_inputs():
    """依頼の動作確認3: エディタ等から複数行の文章をコピーして貼り付けた
    場合を再現する。貼り付けは実装上、ターミナルから見ると`input()`への
    高速な連続呼び出し(行ごとに改行がEnterとして解釈される)として届く。
    貼り付け特有の処理を`_read_multiline_input`側で特別扱いしている
    わけではないため、通常の複数行手入力と同じテストで代表させる。
    """
    pasted_lines = [
        "ToDoリストのCLIツールを作って。",
        "storage.pyでデータの保存・読み込みを、",
        "cli.pyでコマンドライン操作を担当する形にしたい。",
    ]
    (text, terminate), _prompts = _feed(pasted_lines + [""])
    assert terminate is False
    assert text == "\n".join(pasted_lines), repr(text)


def test_exit_as_first_line_terminates_without_waiting_for_blank_line():
    """依頼の動作確認4: 'exit'とだけ入力した場合、空行を待たずに即座に
    終了することを確認する。"""
    (text, terminate), prompts = _feed(["exit"])
    assert terminate is True
    assert prompts == [yoriai._REPL_FIRST_LINE_PROMPT], prompts


def test_quit_as_first_line_terminates_without_waiting_for_blank_line():
    (text, terminate), _prompts = _feed(["quit"])
    assert terminate is True


def test_exit_with_surrounding_whitespace_as_first_line_terminates():
    (text, terminate), _prompts = _feed(["  exit  "])
    assert terminate is True


def test_exit_is_case_insensitive_as_first_line():
    for word in ("EXIT", "Exit", "QUIT", "Quit"):
        (text, terminate), _prompts = _feed([word])
        assert terminate is True, word


def test_exit_typed_as_continuation_line_is_treated_as_message_content():
    """継続入力中(2行目以降)に'exit'とだけ書かれた行が来ても、即座に
    終了せず、メッセージ本文の一部として扱われることを確認する。
    """
    (text, terminate), _prompts = _feed([
        "以下の手順を実行して:",
        "exit",
        "",
    ])
    assert terminate is False
    assert text == "以下の手順を実行して:\nexit", repr(text)


def test_blank_first_line_does_not_start_a_message():
    """まだ何も入力していない状態での空Enterは、何もせず次の入力を
    待つ(=呼び出しごとに再度input()が呼ばれる)ことを確認する。
    """
    (text, terminate), prompts = _feed(["", "", "こんにちは", ""])
    assert terminate is False
    assert text == "こんにちは", repr(text)
    # 空行が2回続いても、いずれも"Yoriai> "のまま(継続入力に入らない)。
    assert prompts == [
        yoriai._REPL_FIRST_LINE_PROMPT,
        yoriai._REPL_FIRST_LINE_PROMPT,
        yoriai._REPL_FIRST_LINE_PROMPT,
        yoriai._REPL_CONTINUATION_PROMPT,
    ], prompts


def test_whitespace_only_line_triggers_send_like_blank_line():
    """空白のみの行も、空行と同様にメッセージ確定のトリガーになることを
    確認する(依頼の「空行(何も入力せずEnterのみ)」の判定は
    strip()後の比較であるため)。
    """
    (text, terminate), _prompts = _feed(["こんにちは", "   "])
    assert terminate is False
    assert text == "こんにちは", repr(text)


def main():
    tests = [
        test_single_line_then_blank_line_sends_as_one_message,
        test_multiple_lines_typed_then_blank_line_are_joined,
        test_pasted_multiline_text_delivered_as_rapid_successive_inputs,
        test_exit_as_first_line_terminates_without_waiting_for_blank_line,
        test_quit_as_first_line_terminates_without_waiting_for_blank_line,
        test_exit_with_surrounding_whitespace_as_first_line_terminates,
        test_exit_is_case_insensitive_as_first_line,
        test_exit_typed_as_continuation_line_is_treated_as_message_content,
        test_blank_first_line_does_not_start_a_message,
        test_whitespace_only_line_triggers_send_like_blank_line,
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
