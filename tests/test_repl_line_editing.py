#!/usr/bin/env python3
"""対話モードでの行編集(矢印キーによるカーソル移動、Backspace)を検証する。

実機で「矢印キー(左右)を押すと、実際に移動する代わりに`^[[D`のような
エスケープシーケンスの文字列がそのまま入力されてしまう」という不具合が
報告された。原因は`readline`モジュールが一度もimportされていなかったこと
だった(CPythonでは、`readline`をimportするだけで、以降の`input()`が
GNU readline/libeditによる行編集を使うようになる)。

矢印キー等の行編集は本物の擬似端末(pty)を通した対話でしか意味のある
検証ができないため、標準ライブラリの`pty`モジュールを使って実際に
サブプロセスへキー入力(エスケープシーケンスを含む)を送り込み、
`input()`が返す文字列を確認する。`readline`をimportしない場合(修正前の
状態を再現)と、importした場合(現在のyoriai.pyの状態)を両方実行し、
挙動の違いを比較することで、修正が実際に効いていることを検証する。

使い方: python3 tests/test_repl_line_editing.py
"""
import os
import pty
import subprocess
import sys

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _run_in_pty(script: str, keystrokes: bytes, timeout: float = 5) -> str:
    """指定したPythonスクリプト本体を擬似端末(pty)の下で実行し、
    キー入力(バイト列、エスケープシーケンスを含んでよい)を送り込んで、
    そのプロセスの標準出力(pty経由でエコーされた内容を含む)を返す。
    """
    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=slave_fd, stdout=slave_fd, stderr=subprocess.STDOUT,
        close_fds=True,
    )
    os.close(slave_fd)
    os.write(master_fd, keystrokes)
    proc.wait(timeout=timeout)
    output = b""
    try:
        while True:
            chunk = os.read(master_fd, 4096)
            if not chunk:
                break
            output += chunk
    except OSError:
        pass
    os.close(master_fd)
    return output.decode("utf-8", errors="replace")


def test_left_arrow_key_leaves_raw_escape_sequence_without_readline():
    """修正前の状態(`readline`未import)を再現し、実際に報告された不具合
    (矢印キーのエスケープシーケンスがそのまま入力される)を確認する。
    「helo」と入力し、左矢印キー(`\\x1b[D`)でカーソルを1つ戻してから
    「l」を挿入するつもりが、`readline`が無いと単なるバイト列として
    そのまま`input()`の戻り値に混入してしまう。
    """
    # 左矢印キーの入力シーケンス: ESC [ D
    keystrokes = b"helo\x1b[Dl\n"
    output = _run_in_pty("print(repr(input()))", keystrokes)
    assert "'helo\\x1b[Dl'" in output, (
        f"readline未importの場合、エスケープシーケンスがそのまま入力される"
        f"はずです(この結果自体は仕様ではなく、修正前の不具合の再現): {output!r}"
    )


def test_left_arrow_key_moves_cursor_with_readline_imported():
    """`readline`をimportすると、左矢印キーで正しくカーソルが移動し、
    意図通りの編集結果になることを確認する(修正後の挙動)。
    """
    keystrokes = b"helo\x1b[Dl\n"
    output = _run_in_pty("import readline; print(repr(input()))", keystrokes)
    assert "'hello'" in output, (
        f"readlineをimportすれば、左矢印キーで正しく編集できるはずです: {output!r}"
    )


def test_backspace_deletes_one_character_with_readline_imported():
    """Backspaceキー(DEL、0x7f)で正しく1文字ずつ削除できることを確認する。
    「hellox」と入力してBackspaceを押すと「hello」になるはず。
    """
    keystrokes = b"hellox\x7f\n"
    output = _run_in_pty("import readline; print(repr(input()))", keystrokes)
    assert "'hello'" in output, (
        f"Backspaceで最後の1文字が削除されるはずです: {output!r}"
    )


def test_multiline_input_still_works_with_readline_imported():
    """複数行入力機能(空行で送信)が、`readline`のimportによって壊れて
    いないことを、実際の擬似端末経由で確認する
    (`yoriai._read_multiline_input`を直接呼び出す)。
    """
    script = (
        "import sys\n"
        f"sys.path.insert(0, {_REPO_ROOT!r})\n"
        "import yoriai\n"
        "text, terminate = yoriai._read_multiline_input()\n"
        "print(repr((text, terminate)))\n"
    )
    # 1行入力してEnter、続けて空行でEnter(送信確定)。
    keystrokes = b"hello\r\r"
    output = _run_in_pty(script, keystrokes)
    assert "('hello', False)" in output, (
        f"複数行入力(1行+空行での送信)が、readline import後も従来通り"
        f"動作するはずです: {output!r}"
    )


def main():
    tests = [
        test_left_arrow_key_leaves_raw_escape_sequence_without_readline,
        test_left_arrow_key_moves_cursor_with_readline_imported,
        test_backspace_deletes_one_character_with_readline_imported,
        test_multiline_input_still_works_with_readline_imported,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:  # pty/subprocessの環境依存の問題は明示的に報告する
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
