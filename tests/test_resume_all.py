#!/usr/bin/env python3
"""未完了プロジェクトの再開(`_resume_project`)と、それを一括で行う
「巡回モード」(`_run_resume_all`、`//resume-all`コマンド)を検証する。

PROGRESS.md(tests/test_progress_persistence.py参照)により、途中で
終了したプロジェクトの状態がディスク上に残るようになった。この状態を
使って、未完了のタスクだけを対象にタスクキュー方式・レビューフェーズを
再開できることを確認する。

使い方: python3 tests/test_resume_all.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


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


def _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
    text = messages[0]["content"]
    if "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def _write_project_progress(projects_root, project_name, tasks, completed_filenames):
    """テスト用に、`completed_filenames`だけが実装・レビューとも完了した
    状態のPROGRESS.mdを持つプロジェクトディレクトリを作る。
    """
    checklist = yoriai._build_task_checklist(tasks)
    for filename in completed_filenames:
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    project_dir = os.path.join(projects_root, project_name)
    yoriai._write_progress_md(project_dir, f"{project_name}を作って", tasks, checklist, {})
    return project_dir


def test_resume_project_skips_when_progress_md_missing():
    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._resume_project(os.path.join(out_dir, "no-such-project"), 47120, "fingerprint")
        assert result is False
        assert "読み取れなかった" in buf.getvalue(), buf.getvalue()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resume_project_reports_success_when_nothing_pending():
    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_test_")
    try:
        tasks = [("a.py", "説明")]
        project_dir = _write_project_progress(out_dir, "already-done", tasks, completed_filenames=["a.py"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._resume_project(project_dir, 47120, "fingerprint")
        assert result is True
        assert "未完了のタスクは見つかりませんでした" in buf.getvalue(), buf.getvalue()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resume_project_only_queues_incomplete_files():
    """依頼の要件: 再開時は未完了だったファイルだけを対象に処理する
    ことを確認する。既に完了していたstorage.pyは実装依頼が飛ばず、
    未完了だったcli.pyだけが実装・レビューされることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    implementation_requests = []

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "あなたが実装を担当するファイル" in text:
            implementation_requests.append(text)
        yield from _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs)

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_test_")
    try:
        tasks = [("storage.py", "永続化を担当"), ("cli.py", "CLI操作を担当")]
        project_dir = _write_project_progress(out_dir, "todo-cli", tasks, completed_filenames=["storage.py"])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._resume_project(project_dir, 47120, "fingerprint")
        output = buf.getvalue()

        assert result is True
        assert "未完了1件" in output, output
        assert any("cli.py" in req for req in implementation_requests), implementation_requests
        assert not any("storage.py" in req.split("あなたが実装を担当するファイル")[-1] for req in implementation_requests), (
            f"既に完了しているstorage.pyは再実装されないはずです: {implementation_requests}"
        )

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is False, parsed["checklist"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resume_project_skips_when_no_candidates_available():
    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_test_")
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: {"self": {}, "peers": []}
    try:
        tasks = [("a.py", "説明")]
        project_dir = _write_project_progress(out_dir, "no-candidates", tasks, completed_filenames=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._resume_project(project_dir, 47120, "fingerprint")
        assert result is False
        assert "組織内にロード済みモデルを持つメンバーがいない" in buf.getvalue(), buf.getvalue()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_resume_all_reports_when_nothing_incomplete():
    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_all_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_resume_all(47120, "fingerprint", out_dir)
        assert "未完了のプロジェクトは見つかりませんでした" in buf.getvalue(), buf.getvalue()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_resume_all_resumes_multiple_projects_and_reports_success():
    """依頼の動作確認: `//resume-all`を実行し、複数の未完了プロジェクトが
    順番に自動で完走することを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_all_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_a = _write_project_progress(
            projects_root, "project-a", [("a.py", "説明A")], completed_filenames=[],
        )
        project_b = _write_project_progress(
            projects_root, "project-b", [("b.py", "説明B")], completed_filenames=[],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_resume_all(47120, "fingerprint", out_dir)
        output = buf.getvalue()

        assert "未完了のプロジェクトが2件見つかりました" in output, output
        assert project_a in output and project_b in output
        assert "[✅ 全プロジェクトの未完了タスクが完了しました]" in output, output
        assert yoriai._find_incomplete_projects(out_dir) == []
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_resume_all_reports_remaining_incomplete_projects_honestly():
    """依頼の動作確認外(既存の「✅完了を安易に名乗らない」方針の踏襲):
    一部のプロジェクトが再開できなかった場合、「全て完了しました」とは
    名乗らず、正直に未完了のプロジェクトを報告することを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: {"self": {}, "peers": []}

    out_dir = tempfile.mkdtemp(prefix="yoriai_resume_all_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_project_progress(
            projects_root, "stuck-project", [("a.py", "説明")], completed_filenames=[],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_resume_all(47120, "fingerprint", out_dir)
        output = buf.getvalue()

        assert "[✅ 全プロジェクトの未完了タスクが完了しました]" not in output, output
        assert "[⚠️ 巡回モード終了: 1件のプロジェクトが未完了のまま残っています]" in output, output
        assert project_dir in output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_resume_project_skips_when_progress_md_missing,
        test_resume_project_reports_success_when_nothing_pending,
        test_resume_project_only_queues_incomplete_files,
        test_resume_project_skips_when_no_candidates_available,
        test_run_resume_all_reports_when_nothing_incomplete,
        test_run_resume_all_resumes_multiple_projects_and_reports_success,
        test_run_resume_all_reports_remaining_incomplete_projects_honestly,
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
