#!/usr/bin/env python3
"""未完了で終わった協業モードを、ユーザーの確認を挟まずに(有限の上限
付きで)自動的に再開する仕組み(`_maybe_auto_resume`)を検証する。

既存の「1ファイルあたりレビュー往復は最大2回まで」という暴走防止の
仕組み(`_review_and_fix_one_file`、内側の歯止め)はそのまま維持し、
今回追加する自動再開にも、プロジェクト単位での外側の歯止め
(`_AUTO_RESUME_MAX_ATTEMPTS`、既定3回)を設ける。試行回数はPROGRESS.md
自身に記録し(read_fileツール・PROGRESS.md全体で踏襲している「ディスク上の
実際の状態を正として扱う」方針)、手動`//resume-all`はこのカウンタとは
無関係に、これまで通り何度でも実行できる。

使い方: python3 tests/test_auto_resume.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import progress  # noqa: E402
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"  # Enterキー単体(送信)


def _make_card(device_name, model, free_gb):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 64},
        "models": {"installed": [model], "loaded": [model], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _two_member_snapshot():
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peers = [{
        "card": _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20),
        "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0,
    }]
    return {"self": self_card, "peers": peers}


_TWO_FILE_BREAKDOWN_ANSWER = (
    "storage.py: データの永続化を担当\n"
    "cli.py: コマンドライン操作を担当\n"
)


def _fake_stream_always_ok(candidate, org_fingerprint, messages, **_kwargs):
    text = messages[0]["content"]
    if "ファイルに分割する実装計画" in text:
        yield {"content": _TWO_FILE_BREAKDOWN_ANSWER}
    elif "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def _fake_stream_cli_review_always_fails(candidate, org_fingerprint, messages, **_kwargs):
    """cli.pyのレビューだけが常に「問題あり」を返す(何度再開しても
    解決しない状況を再現する)。
    """
    text = messages[0]["content"]
    if "ファイルに分割する実装計画" in text:
        yield {"content": _TWO_FILE_BREAKDOWN_ANSWER}
    elif "レビュー対象: cli.py" in text:
        yield {"content": "問題あり\ncli.pyの引数の扱いが計画と一致していません。"}
    elif "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_parse_auto_resume_count_defaults_to_zero_when_section_missing():
    assert progress._parse_auto_resume_count("# プロジェクト進行状況\n") == 0


def test_parse_auto_resume_count_reads_recorded_value():
    text = "## 自動再開の試行回数\n\n2\n"
    assert progress._parse_auto_resume_count(text) == 2


def test_parse_auto_resume_count_defaults_to_zero_when_unparseable():
    text = "## 自動再開の試行回数\n\n(不明)\n"
    assert progress._parse_auto_resume_count(text) == 0


def test_progress_markdown_round_trips_auto_resume_count():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {}, auto_resume_count=2)
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(tempfile_dir, ignore_errors=True)

    assert parsed["auto_resume_count"] == 2


def test_resume_project_preserves_disk_count_by_default():
    """`auto_resume_count`を明示的に渡さない(既定の`None`のままの)場合、
    PROGRESS.mdに既に記録されている値がそのまま引き継がれる(勝手に
    増減しない)ことを確認する。手動`//resume-all`はこの既定のまま
    `_resume_project`を呼ぶ。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_always_ok

    out_dir = tempfile_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        tasks = [("a.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        # 既に3回試行済み(上限到達済み)という状況を模擬する。
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {}, auto_resume_count=3)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._resume_project(out_dir, 47120, "fingerprint")
        assert result is True

        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["auto_resume_count"] == 3, (
            f"手動再開(_resume_project、auto_resume_count未指定)は既存の値を変更しないはずです: {parsed}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(tempfile_dir, ignore_errors=True)


def test_manual_resume_all_works_even_after_auto_resume_limit_reached():
    """依頼の要件5: `//resume-all`をユーザーが手動で実行する場合は、自動
    再開の上限に達したプロジェクトであっても、これまで通り実行できる
    ことを確認する(上限は「自動で勝手に回る場合」にのみ適用される)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_always_ok

    out_dir = tempfile_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = os.path.join(projects_root, "stuck-project")
        tasks = [("a.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        # 自動再開の上限に既に達している(auto_resume_count=3)状態を模擬する。
        yoriai._write_progress_md(project_dir, "何か作って", tasks, checklist, {}, auto_resume_count=3)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_resume_all(47120, "fingerprint", out_dir)
        output = buf.getvalue()

        assert "[✅ 全プロジェクトの未完了タスクが完了しました]" in output, (
            f"上限到達済みでも手動//resume-allは完走できるはずです: {output}"
        )
        assert "自動再開の上限" not in output, (
            f"手動実行では上限メッセージが出ないはずです: {output}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(tempfile_dir, ignore_errors=True)


def test_maybe_auto_resume_does_nothing_when_project_already_complete():
    out_dir = tempfile_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        tasks = [("a.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._set_task_status(checklist, "a.py", "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, "a.py", "review", yoriai._TASK_STATUS_COMPLETED)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._maybe_auto_resume(out_dir, 47120, "fingerprint")
        assert buf.getvalue() == "", buf.getvalue()
    finally:
        shutil.rmtree(tempfile_dir, ignore_errors=True)


def test_collaborate_auto_resumes_and_eventually_completes_when_issue_gets_fixed():
    """依頼の動作確認: 1〜2回の自動再開で解決するケースでは、ユーザーが
    何もしなくても最終的に「✅ 全タスク完了」になることを確認する。
    1回目のcli.pyレビューだけ「問題あり」を返し、2回目(自動再開後)の
    レビューでは「問題なし」を返すようにする。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    cli_review_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "ファイルに分割する実装計画" in text:
            yield {"content": _TWO_FILE_BREAKDOWN_ANSWER}
        elif "レビュー対象: cli.py" in text:
            cli_review_calls["n"] += 1
            if cli_review_calls["n"] <= 2:
                # 初回実行時の(最大2回までの)レビュー往復はどちらも失敗させる。
                yield {"content": "問題あり\nまだ直っていません。"}
            else:
                # 自動再開後は「問題なし」に変わる(解決した状況を再現)。
                yield {"content": "問題なし"}
        elif "レビュー対象" in text:
            yield {"content": "問題なし"}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)
        output = buf.getvalue()

        assert "[🔁" in output and "自動的に再開します" in output, (
            f"自動再開が行われたことがログに残っているはずです: {output}"
        )
        assert "[✅ 全タスク完了" in output, output
        assert "自動再開の上限" not in output, output

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME,
            yoriai._project_name_with_date_prefix("ToDoリストのCLIツールを作って"),
        )
        assert yoriai._find_incomplete_projects(out_dir) == [], (
            "最終的に未完了プロジェクトが残っていないはずです"
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["auto_resume_count"] == 1, parsed
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collaborate_auto_resume_stops_at_limit_and_reports_clearly():
    """依頼の動作確認: 意図的に何度もレビューで問題が指摘され続ける状況を
    作り、3回の自動再開を試みた後、自動的に停止して「人間の確認が必要」
    という報告が出ることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_cli_review_always_fails

    out_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)
        output = buf.getvalue()

        assert (
            "[⛔ " in output and "自動再開の上限(3回)に達しました" in output and "人間の確認が必要です" in output
        ), output
        assert "[✅ 全タスク完了" not in output, output
        # 上限に達した後は、それ以上自動再開の試行が続かない
        # (試行ログの出現回数が上限のちょうど3回で止まっていること)。
        assert output.count("自動的に再開します") == 3, (
            f"自動再開の試行は3回で止まるはずです: {output.count('自動的に再開します')}回"
        )

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME,
            yoriai._project_name_with_date_prefix("ToDoリストのCLIツールを作って"),
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["auto_resume_count"] == 3, parsed
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is True
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_auto_resume_runs_in_background_without_blocking_next_input():
    """依頼の要件4: 自動再開もバックグラウンドで行われ、処理中もユーザーは
    他の依頼を入力できる状態を維持することを確認する。協業モード自体が
    既にバックグラウンドジョブとして実行され(tests/test_background_
    collaborate.py参照)、自動再開(`_maybe_auto_resume`)はその同じ
    バックグラウンドジョブの実行の一部として(追加のジョブ投入なしに)
    処理されるため、メインスレッドの入力ループは自動再開の完了を待たずに
    `exit`できるはずである。
    """
    release = threading.Event()
    call_count = {"n": 0}

    def slow_collaborate(*args, **kwargs):
        call_count["n"] += 1
        release.wait(timeout=5)

    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_ask_collaborate = yoriai._ask_organization_collaborate

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        yoriai._create_repl_prompt_session = fake_create_session
        yoriai._create_background_job_runner = fake_create_runner
        yoriai._ask_organization_collaborate = slow_collaborate

        pipe_input.send_text(f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT + "exit" + _SUBMIT)

        buf = io.StringIO()
        repl_out_dir = tempfile.mkdtemp(prefix="yoriai_auto_resume_repl_test_")
        try:
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", repl_out_dir)
        finally:
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._create_background_job_runner = original_create_runner
            shutil.rmtree(repl_out_dir, ignore_errors=True)

    output = buf.getvalue()
    release.set()
    runner_holder["runner"].join()
    yoriai._ask_organization_collaborate = original_ask_collaborate

    assert "対話モードを終了します。" in output, (
        f"(協業モード内で行われる)自動再開の完了を待たずに終了できるはずです: {output}"
    )
    assert call_count["n"] == 1


def main():
    tests = [
        test_parse_auto_resume_count_defaults_to_zero_when_section_missing,
        test_parse_auto_resume_count_reads_recorded_value,
        test_parse_auto_resume_count_defaults_to_zero_when_unparseable,
        test_progress_markdown_round_trips_auto_resume_count,
        test_resume_project_preserves_disk_count_by_default,
        test_manual_resume_all_works_even_after_auto_resume_limit_reached,
        test_maybe_auto_resume_does_nothing_when_project_already_complete,
        test_collaborate_auto_resumes_and_eventually_completes_when_issue_gets_fixed,
        test_collaborate_auto_resume_stops_at_limit_and_reports_clearly,
        test_auto_resume_runs_in_background_without_blocking_next_input,
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
