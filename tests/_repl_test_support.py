#!/usr/bin/env python3
"""対話モードのテスト用共通ヘルパー。

`_create_repl_prompt_session`が「対話モードの生存期間中、フルスクリーンの
`Application`を1つだけ動かし続ける」設計に変わったことに伴い(以前は
`session.prompt()`をメッセージごとに呼び直していたが、メッセージ送信の
たびに画面が明滅する不具合が実機で報告されたため撤回した)、テスト側も
同じ土台(本番と同じ`_create_repl_prompt_session()`)を使ってキーストローク
を送り込み、結果を検証する必要がある。各テストファイルが個別に同等の
ヘルパーを再実装するのを避けるため、ここに共通化した。

使い方の要点:
- `run_repl_session_with_keys`: `_read_multiline_input`を直接検証したい
  テスト向け。`_create_repl_prompt_session()`ベースの永続セッションを
  1つ作り、キーストロークを送り込んで、指定した件数(または`terminate=True`
  が返るまで)の`(text, terminate)`を集めて返す。
- `run_full_repl_client_with_keys`: `_run_repl_client`全体(コマンド判定・
  問い合わせ処理を含む)を検証したいテスト向け。`yoriai._create_repl_
  prompt_session`を差し替える必要はもう無く(本番の関数が`create_app_
  session`経由でpipe_input/DummyOutputを正しく引き継ぐため)、呼び出しを
  丸ごと`create_app_session(input=..., output=DummyOutput())`で包むだけで
  よい。「画面に表示された内容」(`sys.stdout`への直接出力+ログ欄
  バッファの内容)を1つの文字列にまとめて返す(理由は関数のdocstring
  参照)。
"""
import contextlib
import io
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402


def _drain_chat_input_queue() -> None:
    """前のテストの残骸(積まれたままの入力・EOF・Ctrl+C)を捨てる。"""
    while not yoriai._CHAT_INPUT_QUEUE.empty():
        try:
            yoriai._CHAT_INPUT_QUEUE.get_nowait()
        except queue.Empty:
            break


def _start_ui_thread(session) -> threading.Thread:
    """`session.app.run()`を専用スレッドで起動する。本番の`_run_repl_
    client`と同じく`contextvars.copy_context()`で現在の`AppSession`
    (`create_app_session()`で設定したもの)をこのスレッドへ引き継ぐ
    (`threading.Thread`は既定でcontextvarsを引き継がないため、これが
    無いと`get_app_session()`がスレッドごとに別々の`AppSession`を返して
    しまい、`_ChatOutputRouter`が「Applicationは動いていない」と誤判定
    する不具合が実機で再現した。詳細は`yoriai._run_repl_client`の
    コメント参照)。
    """
    import contextvars
    ctx = contextvars.copy_context()
    ui_thread = threading.Thread(
        target=ctx.run, args=(session.app.run,), kwargs={"handle_sigint": False}, daemon=True,
    )
    ui_thread.start()
    return ui_thread


def run_repl_session_with_keys(keystrokes: str, message_count: int = 1, timeout: float = 5.0):
    """`keystrokes`を仮想端末に送り込み、本番と同じ`_create_repl_prompt_
    session()`ベースの永続セッションで処理する。`_read_multiline_input`を
    (`message_count`回、または`terminate=True`が返るまで)繰り返し呼び、
    結果のリスト`[(text, terminate), ...]`を返す。呼び出し後はセッションを
    確実に終了させる。
    """
    _drain_chat_input_queue()
    with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
        session = yoriai._create_repl_prompt_session()
        ui_thread = _start_ui_thread(session)
        try:
            pipe_input.send_text(keystrokes)

            interrupt_guard = yoriai._DoubleInterruptGuard()
            results = []
            while len(results) < message_count:
                text, terminate = yoriai._read_multiline_input(interrupt_guard)
                results.append((text, terminate))
                if terminate:
                    break
        finally:
            session.app.exit()
            ui_thread.join(timeout=timeout)
    return results


def run_full_repl_client_with_keys(
    keystrokes: str, port: int, org_fingerprint: str, out_dir: str, stdout=None,
) -> str:
    """`yoriai._run_repl_client`本体を、仮想端末に送り込んだキーストローク
    で実際に走らせ、「画面に表示された内容」を1つの文字列として返す。
    呼び出し元は`sys.stdout`を自分で捕捉する必要はない(このヘルパーが
    内部で`contextlib.redirect_stdout`を行う)。`isatty()`の挙動など、
    捕捉先に特別な性質を持たせたいテスト向けに、`stdout`引数で捕捉先の
    オブジェクト(`.write()`・`.flush()`を持つもの)を差し替えられる
    (既定では`io.StringIO()`)。

    仮の判断(実機バグ修正により必要になった変更): 以前は`AppSession`の
    contextvarsが`ui_thread`(`session.app.run()`を動かす専用スレッド)へ
    伝播しないバグにより、フルスクリーンUIが実際には動いているのに
    `_ChatOutputRouter`が常に「Applicationは動いていない」と誤判定し、
    印字内容が(本来はログ欄バッファへ書かれるべきところ)素の出力先へ
    直接書き出されてしまっていた(実機で「Ctrl+C時に画面が崩れる」不具合
    として顕在化し、`ui_thread`にも`contextvars.copy_context()`を使う
    よう修正した)。この不具合の修正により、フルスクリーンUIが動いて
    いる間の印字内容(`[判断: ...]`等)は正しく`_CHAT_LOG_BUFFER`
    (ログ欄専用のバッファ)へ書き込まれるようになり、`sys.stdout`
    (呼び出し元が`contextlib.redirect_stdout`で捕捉するもの)には
    現れなくなった(本番の設計通りの正しい挙動)。既存のテスト群は
    「画面に表示された内容」を1つの文字列として検証する作りだったため、
    このヘルパー自身が`sys.stdout`の捕捉と`_CHAT_LOG_BUFFER`の内容を
    まとめて返すようにし、個々のテストが両方を意識せずに済むようにした。
    """
    buf = stdout if stdout is not None else io.StringIO()
    yoriai._reset_chat_log_buffer()
    with create_pipe_input() as pipe_input:
        pipe_input.send_text(keystrokes)
        with create_app_session(input=pipe_input, output=DummyOutput()):
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(port, org_fingerprint, out_dir)
    return buf.getvalue() + yoriai._CHAT_LOG_BUFFER.text
