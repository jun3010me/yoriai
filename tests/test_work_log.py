#!/usr/bin/env python3
"""`--chat`の起動中に画面へ流れた作業の流れそのもの(「[サブタスク6] [🔍 ...
を検索しています...]」のような進行ログ)が、会話ログと対になる作業ログ
ファイル(`chat_logs/work_<YYYYMMDD_HHMMSS>.log`)へ、1行ずつ時刻付きで
逐次記録されることを検証する。

使い方: python3 tests/test_work_log.py
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402
from _repl_test_support import run_full_repl_client_with_keys  # noqa: E402

_SUBMIT = "\r"
_TIMESTAMPED_LINE = re.compile(r"^\d{2}:\d{2}:\d{2} ")


def _read_body_lines(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    assert lines[0].startswith("# Yoriai 作業ログ"), lines[0]
    return lines[1:]


def test_work_log_writes_each_line_with_timestamp_and_flushes_immediately():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_work_log_test_")
    try:
        work_log = yoriai._create_work_log(tmp_dir, "20260101_000000")
        assert work_log.path == os.path.join(tmp_dir, "chat_logs", "work_20260101_000000.log")

        work_log.write("[サブタスク6] [🔍 test_verify.py 内で 'x' を検索しています...]\n")
        # close()前でも、改行まで届いた行はすでにファイルに書かれている
        # (実行中に`tail -f`で追いかけられる)はず。
        body = _read_body_lines(work_log.path)
        assert len(body) == 1, body
        assert _TIMESTAMPED_LINE.match(body[0]), body[0]
        assert body[0].endswith("[サブタスク6] [🔍 test_verify.py 内で 'x' を検索しています...]")
        work_log.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_work_log_joins_fragmented_writes_and_strips_ansi_escapes():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_work_log_test_")
    try:
        work_log = yoriai._create_work_log(tmp_dir, "20260101_000000")
        # print()は本文と改行を別々に書く。LLMの応答も細切れで届く。
        for chunk in ["\x1b[1m\x1b[36m", "こん", "にちは", "\x1b[0m", "\n", "2行目\n3行目"]:
            work_log.write(chunk)
        work_log.close()
        body = [line[9:] for line in _read_body_lines(work_log.path)]
        assert body == ["こんにちは", "2行目", "3行目"], body
        # 閉じた後の書き込みは無視される(例外にならない)。
        work_log.write("閉じた後\n")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_work_log_keeps_lines_from_concurrent_threads_separate():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_work_log_test_")
    try:
        work_log = yoriai._create_work_log(tmp_dir, "20260101_000000")
        barrier = threading.Barrier(2)

        def worker(tag):
            barrier.wait()
            for i in range(200):
                # print()と同じく、本文と改行を別々の書き込みにする。
                work_log.write(f"[{tag}] 行{i}")
                work_log.write("\n")

        threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("サブタスク4", "サブタスク6")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        work_log.close()

        body = [line[9:] for line in _read_body_lines(work_log.path)]
        assert len(body) == 400, len(body)
        for tag in ("サブタスク4", "サブタスク6"):
            assert [line for line in body if line.startswith(f"[{tag}]")] == [f"[{tag}] 行{i}" for i in range(200)]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_repl_session_records_printed_progress_to_work_log_paired_with_chat_log():
    original_ask = yoriai._ask_organization

    def stub_ask(port, org_fingerprint, messages, **kwargs):
        print("[サブタスク6] [🧪 python3 -m py_compile inventory_manager.py を実行しています...]")
        print("[サブタスク4] [📖 inventory_cli.py を読みに行っています...]")

    yoriai._ask_organization = stub_ask
    out_dir = tempfile.mkdtemp(prefix="yoriai_work_log_repl_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_full_repl_client_with_keys("こんにちは" + _SUBMIT + "exit" + _SUBMIT, 47120, "fingerprint", out_dir)

        log_dir = os.path.join(out_dir, yoriai._CHAT_LOG_SUBDIR_NAME)
        names = sorted(os.listdir(log_dir))
        assert len(names) == 2, names
        chat_name, work_name = names
        assert re.match(r"^chat_\d{8}_\d{6}\.md$", chat_name), chat_name
        # 会話ログと同じ起動時刻をファイル名に持つ(対になるファイル)。
        assert work_name == chat_name.replace("chat_", "work_").replace(".md", ".log"), names

        body = _read_body_lines(os.path.join(log_dir, work_name))
        assert all(_TIMESTAMPED_LINE.match(line) for line in body), body
        joined = "\n".join(body)
        assert "[サブタスク6] [🧪 python3 -m py_compile inventory_manager.py を実行しています...]" in joined
        assert "[サブタスク4] [📖 inventory_cli.py を読みに行っています...]" in joined
        assert "対話モードを終了します。" in joined
    finally:
        yoriai._ask_organization = original_ask
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"ok: {name}")
    print(f"{len(tests)} passed")
