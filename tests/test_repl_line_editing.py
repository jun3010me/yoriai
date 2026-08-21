#!/usr/bin/env python3
"""対話モードでの複数行編集(上下矢印キーでの行間移動、Enter/Shift+Enterの
使い分け、Backspace、継続行の見た目)を検証する。

以前は`readline`(1行単位の編集)に依存していたが、readlineは1行内の
カーソル移動にしか対応しておらず、上下矢印キーで前の行に戻って編集する
ことが原理的にできなかった。実機からも、複数行にまたがる入力の改行部分に
制御文字と思われる記号が表示されることがあるという報告があった。

`prompt_toolkit`の`multiline=True`セッションを採用し、複数行の入力全体を
1つの編集可能な領域として扱うようにした。矢印キーによる行間移動や
Backspaceといった基本的な編集機能自体はprompt_toolkit本体が提供する
ものであり、ライブラリ側で十分にテストされているため、ここでは
「Yoriaiがこの機能をどう使っているか」(multiline設定・送信キーの
割り当て・複数行バッファでの実際の編集結果)を検証する。

送信キーの割り当ては「空行で送信」→「Alt+Enterで送信」→「Enterで送信、
Shift+Enterで改行」の順に変更してきた(詳細な経緯は
`yoriai._make_repl_key_bindings`のコメントを参照)。このファイルの
テストは本番と同じ`yoriai._make_repl_key_bindings()`のキー割り当てで
セッションを作り、Enter/Shift+Enterのキー割り当てそのものの検証は
tests/test_enter_to_submit.pyで行う。

`prompt_toolkit.input.create_pipe_input`で仮想的なキー入力を送り込み、
`prompt_toolkit.output.DummyOutput`で実際の画面描画を行わないように
した`PromptSession`を使う(prompt_toolkit公式のテスト手法。実際の
擬似端末を使わずに済む)。

使い方: python3 tests/test_repl_line_editing.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"  # Enterキー単体(送信)
# Shift+Enter(改行を挿入)を送る際のバイト列。詳細は
# yoriai._make_repl_key_bindingsのコメントを参照。
_NEWLINE = "\x1b[27;2;13~"


def _read_with_keys(keystrokes: str):
    with create_pipe_input() as pipe_input:
        session = PromptSession(
            input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
        )
        pipe_input.send_text(keystrokes)
        return yoriai._read_multiline_input(session, yoriai._DoubleInterruptGuard())


def test_continuation_lines_have_no_prefix():
    """依頼の改善2: 2行目以降に"..."のようなプレフィックスを付けず、
    プレーンな見た目(カーソルだけが次の行に移る)にすることを確認する。
    以前は`readline`ベースの実装で「まだ入力途中である」ことを示すための
    "...  "というプレフィックスを使っていたが、`prompt_toolkit`の
    multiline編集では複数行入力欄であること自体が視覚的に自明なため、
    プレフィックスを廃止した(1行目のプロンプト"Yoriai> "は維持する)。
    """
    for line_number in (0, 1, 5):
        for is_soft_wrap in (False, True):
            assert yoriai._repl_prompt_continuation(80, line_number, is_soft_wrap) == "", (
                f"line_number={line_number}, is_soft_wrap={is_soft_wrap}"
            )


def test_shift_enter_inserts_a_newline_instead_of_submitting():
    """依頼の中核要件: Shift+Enterは改行を挿入するだけで送信しないことを
    確認する(Enterで送信されるようになったことに伴い、改行の挿入は
    Shift+Enterの役割になった)。
    """
    text, terminate = _read_with_keys("1行目" + _NEWLINE + "2行目" + _SUBMIT)
    assert terminate is False
    assert text == "1行目\n2行目", repr(text)


def test_up_arrow_moves_cursor_to_the_previous_line_for_editing():
    """依頼の中核要件: 上矢印キーで、直前に入力した行に戻ってカーソルを
    置き、その場で編集できることを確認する(readlineでは不可能だった
    複数行にまたがる自由な編集)。「foo」「bar」と入力後、上矢印で
    「foo」の行末に戻り、「X」を挿入すると「fooX」になるはず(「bar」の
    行はそのまま)。
    """
    keystrokes = "foo" + _NEWLINE + "bar" + "\x1b[A" + "X" + _SUBMIT
    text, terminate = _read_with_keys(keystrokes)
    assert terminate is False
    assert text == "fooX\nbar", repr(text)


def test_down_arrow_moves_cursor_back_to_the_next_line():
    """上矢印で前の行に戻った後、下矢印で元いた行(最後の行)に戻れる
    ことを確認する。
    """
    # foo / bar と入力 → 上矢印で foo の行末へ → 下矢印で bar の行末へ戻る → Y を追記
    keystrokes = "foo" + _NEWLINE + "bar" + "\x1b[A" + "\x1b[B" + "Y" + _SUBMIT
    text, terminate = _read_with_keys(keystrokes)
    assert terminate is False
    assert text == "foo\nbarY", repr(text)


def test_backspace_deletes_one_character_within_multiline_buffer():
    """Backspace(DEL、0x7f)で、複数行バッファ内でも1文字ずつ正しく
    削除できることを確認する。「hellox」と入力してBackspaceを押すと
    「hello」になるはず。
    """
    text, terminate = _read_with_keys("1行目" + _NEWLINE + "hellox\x7f" + _SUBMIT)
    assert terminate is False
    assert text == "1行目\nhello", repr(text)


def test_multiline_japanese_text_has_no_leaked_control_characters():
    """実機で報告された不具合の再現確認: 複数行の日本語文章を入力した際、
    改行部分やその他の箇所に制御文字(エスケープシーケンスの断片等)が
    紛れ込まないことを確認する。
    """
    keystrokes = (
        "以下の内容でプロジェクトを作って:" + _NEWLINE +
        "1. storage.pyでデータを保存する" + _NEWLINE +
        "2. cli.pyでコマンドライン操作を行う"
        + _SUBMIT
    )
    text, terminate = _read_with_keys(keystrokes)
    assert terminate is False
    assert "\x1b" not in text, f"制御文字(ESC)が結果に紛れ込んでいます: {text!r}"
    assert all(0x20 <= ord(c) or c in "\n" for c in text), (
        f"印字不可能な制御文字が結果に紛れ込んでいます: {text!r}"
    )
    assert text == (
        "以下の内容でプロジェクトを作って:\n"
        "1. storage.pyでデータを保存する\n"
        "2. cli.pyでコマンドライン操作を行う"
    ), repr(text)


def test_editing_an_earlier_line_after_pasting_multiline_text():
    """複数行のテキストを貼り付けた(高速に連続入力された)後でも、
    上矢印で前の行に戻って編集できることを確認する(貼り付け特有の
    経路で壊れていないことの回帰検知)。

    仮の判断: 上矢印キーは(一般的なテキストエディタと同様に)直前の
    カーソル列を保ったまま1つ上の行へ移動するため、直前に確定した行の
    文字数が異なる場合、そのままでは行の途中に移動することがある。
    これは仕様通りの挙動であり、行末に確実に移動するにはEndキー
    (`\\x1b[F`)を使う。
    """
    keystrokes = (
        "storage.pyを実装" + _NEWLINE +
        "cli.pyを実装"
        + "\x1b[A"  # 上矢印で「storage.pyを実装」の行へ移動
        + "\x1b[F"  # Endキーでその行の行末へ
        + "(add_todo関数を含む)"
        + _SUBMIT
    )
    text, terminate = _read_with_keys(keystrokes)
    assert terminate is False
    assert text == "storage.pyを実装(add_todo関数を含む)\ncli.pyを実装", repr(text)


def main():
    tests = [
        test_continuation_lines_have_no_prefix,
        test_shift_enter_inserts_a_newline_instead_of_submitting,
        test_up_arrow_moves_cursor_to_the_previous_line_for_editing,
        test_down_arrow_moves_cursor_back_to_the_next_line,
        test_backspace_deletes_one_character_within_multiline_buffer,
        test_multiline_japanese_text_has_no_leaked_control_characters,
        test_editing_an_earlier_line_after_pasting_multiline_text,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:  # prompt_toolkitの環境依存の問題は明示的に報告する
            failures += 1
            print(f"ERROR: {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK:   {test.__name__}")
    if failures:
        print(f"\n{failures}件のテストが失敗しました。")
        sys.exit(1)
    print("\nすべてのテストが成功しました。")


if __name__ == "__main__":
    main()
