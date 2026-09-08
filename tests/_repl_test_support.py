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
  よい。
"""
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
        ui_thread = threading.Thread(
            target=session.app.run, kwargs={"handle_sigint": False}, daemon=True,
        )
        ui_thread.start()
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


def run_full_repl_client_with_keys(keystrokes: str, port: int, org_fingerprint: str, out_dir: str) -> None:
    """`yoriai._run_repl_client`本体を、仮想端末に送り込んだキーストローク
    で実際に走らせる。呼び出し元は事前に`sys.stdout`を`contextlib.
    redirect_stdout`等で捕捉しておくこと。
    """
    with create_pipe_input() as pipe_input:
        pipe_input.send_text(keystrokes)
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yoriai._run_repl_client(port, org_fingerprint, out_dir)
