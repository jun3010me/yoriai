#!/usr/bin/env python3
"""タスクキュー方式で複数のワーカースレッドが並行して実装・レビューを
進める際、画面への出力(ログ)が入り乱れて、どの行がどのタスクの結果か
分からなくなる不具合への対策(`_print_tagged`)を検証する。

実機で、以下のように別々のタスクのログが入り交じって表示され、
どちらの結果がどちらのレビューに対応するのか画面から読み取れなく
なる不具合が報告された:

    [🔎 修正後の再レビュー] junnoMac-mini が cli.py をレビューしています...
    [MacStudioがutils.pyをレビュー] 問題あり: ...

対策として、(1)関連する複数行の出力を1回のロック取得・1回のprint()に
まとめることで、あるタスクの出力ブロックが他スレッドの出力によって
分断されないようにし、(2)すべての行の先頭にそのタスクを識別する
ファイル名のタグを付けることで、たとえ別タスクのブロックが直後に
続いても、1行単位でもどのタスクの行かを判別できるようにした
(`_print_tagged`、および`_run_collaborative_task_queue`・
`_review_and_fix_one_file`一連の関数への適用)。

使い方: python3 tests/test_threaded_output_clarity.py
"""
import builtins
import contextlib
import io
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_print_tagged_prefixes_every_line_with_the_tag():
    lock = threading.Lock()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yoriai._print_tagged(lock, "cli.py", "1行目\n2行目\n3行目")
    output = buf.getvalue()
    assert output == "[cli.py] 1行目\n[cli.py] 2行目\n[cli.py] 3行目\n", output


def test_print_tagged_works_without_a_lock():
    """`_review_and_fix_one_file`等は、タスクキュー方式(複数スレッド)
    だけでなく既存のテストのように単体で直接呼び出されることもあるため、
    `print_lock=None`でも(ロックを取らずに)タグ付き出力自体は行われる
    ことを確認する。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yoriai._print_tagged(None, "storage.py", "問題なし")
    assert buf.getvalue() == "[storage.py] 問題なし\n", buf.getvalue()


def test_print_tagged_emits_multiline_text_as_a_single_print_call():
    """複数行にわたるテキスト(例: 実装結果のコードプレビュー)が、複数回の
    print()ではなく1回のprint()呼び出しでまとめて出力されることを確認する。
    他スレッドの出力によって分断されないようにするための設計の核心部分。
    """
    calls = []
    original_print = builtins.print
    builtins.print = lambda *args, **kwargs: calls.append(args[0] if args else "")
    try:
        lock = threading.Lock()
        yoriai._print_tagged(lock, "cli.py", "line1\nline2\nline3")
    finally:
        builtins.print = original_print

    assert len(calls) == 1, f"print()は1回だけ呼ばれるはずです: {calls}"
    assert calls[0] == "[cli.py] line1\n[cli.py] line2\n[cli.py] line3", calls[0]


def test_print_tagged_prevents_interleaving_under_concurrent_calls():
    """複数スレッドが同時に`_print_tagged`を呼んでも、1回の呼び出し分の
    出力(複数行になりうる)が他スレッドの出力によって分断されない
    ことを、実際にスレッドを使って検証する(バグ2の直接の回帰再現)。
    """
    lock = threading.Lock()
    n_threads = 8
    lines_per_call = 15

    def worker(n):
        text = "\n".join(f"line{n}-{i}" for i in range(lines_per_call))
        yoriai._print_tagged(lock, f"task{n}", text)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    output = buf.getvalue()

    lines = [line for line in output.split("\n") if line]
    assert len(lines) == n_threads * lines_per_call, (
        f"{n_threads}スレッド x {lines_per_call}行 = {n_threads * lines_per_call}行のはずです: {lines}"
    )

    # 各タスクのブロックが、他タスクの行に分断されずにひとまとまりで
    # 出現していることを確認する: 8ブロックそれぞれが、連続する
    # lines_per_call行としてきっちり出現し、途中に別タスクの行が
    # 混ざっていないはず。
    i = 0
    seen_tasks = set()
    while i < len(lines):
        block = lines[i:i + lines_per_call]
        first_tag = block[0].split("]", 1)[0][1:]  # "[task3] line3-0" -> "task3"
        assert first_tag not in seen_tasks, (
            f"タスク{first_tag}のブロックが複数回に分断されて出現しています: {lines}"
        )
        seen_tasks.add(first_tag)
        n = first_tag[len("task"):]
        expected_block = [f"[{first_tag}] line{n}-{j}" for j in range(lines_per_call)]
        assert block == expected_block, (
            f"タスク{first_tag}のブロックが他スレッドの出力で分断/入り乱れています: {block}"
        )
        i += lines_per_call

    assert seen_tasks == {f"task{n}" for n in range(n_threads)}, seen_tasks


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
    text = messages[0]["content"]
    if "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_queue_run_every_output_line_is_tagged_with_a_task_filename():
    """`_run_collaborative_task_queue`の実行中に出力されるすべての行
    (先頭の開始メッセージ1行を除く)が、いずれかのタスクのファイル名の
    タグで始まることを確認する(依頼の動作確認要件: どのログがどの
    ファイル・どの担当者の結果かが画面表示だけで明確に判別できること)。
    """
    import tempfile
    import shutil

    tasks = [
        ("storage.py", "データの永続化(add_todo, list_todos)"),
        ("cli.py", "コマンドライン操作(argparseでadd/listサブコマンド)"),
        ("utils.py", "共通処理(generate_id)"),
        ("config.py", "設定値(保存先ファイルパス)"),
    ]
    candidates = [
        _member("MacStudio(自分)", "qwen2.5-coder-32b"),
        _member("junnoMac-mini", "qwen2.5-coder-14b"),
    ]
    filenames = {fn for fn, _ in tasks}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_output_clarity_test_")
    try:
        checklist = yoriai._build_task_checklist(tasks)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_task_queue(tasks, candidates, "fingerprint", out_dir, checklist)
        output = buf.getvalue()
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    lines = [line for line in output.split("\n") if line]
    assert lines, "出力が空です"

    # 先頭の「タスクキュー方式で実装フェーズを開始します」だけは、
    # まだどのタスクにも属さない全体の開始メッセージのため例外的に許容する。
    startup_line = lines[0]
    assert "タスクキュー方式で実装フェーズを開始します" in startup_line, startup_line
    task_lines = lines[1:]
    assert task_lines, "タスクに関する出力がありません"

    untagged = []
    for line in task_lines:
        matched = any(line.startswith(f"[{fn}]") for fn in filenames)
        if not matched:
            untagged.append(line)
    assert not untagged, (
        f"以下の行がどのタスクのものか判別できるタグを持っていません: {untagged}\n"
        f"(全出力: {task_lines})"
    )


def main():
    tests = [
        test_print_tagged_prefixes_every_line_with_the_tag,
        test_print_tagged_works_without_a_lock,
        test_print_tagged_emits_multiline_text_as_a_single_print_call,
        test_print_tagged_prevents_interleaving_under_concurrent_calls,
        test_queue_run_every_output_line_is_tagged_with_a_task_filename,
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
