#!/usr/bin/env python3
"""統合検証ループ(全タスク完了後、プロジェクト全体を実際に動かして
確認し、落ちたら直す)の追加を検証するテスト。

背景: 構文チェック(_check_file_syntax、ファイル単位)とレビュー担当に
よる内容レビュー(最大2回、読むだけで実行はしない)だけでは、構文としては
正しいが実行すると落ちるコード(import漏れ・未定義の関数呼び出し・
モジュール間のシグネチャ不一致など)を誰も検出できないまま
「✅ 全タスク完了」になってしまう不具合が報告された。合意フェーズで
決めた検証コマンドを、全タスク完了後に実際に実行し、失敗したら担当
メンバー1名に修正を依頼して再実行する(最大MAX_VERIFY_ATTEMPTS回)ループを
追加した。

ここでは(1)`_run_integration_verification`の試行回数の挙動(1回目/2回目で
成功、3回とも失敗)、(2)`_run_collaborative_project`での未完了タスク・
検証コマンド未設定によるスキップ、(3)PROGRESS.mdへの結果の記録・
再読み込み、(4)`_project_has_pending_work`が失敗した統合検証を拾うこと、
(5)`_extract_verify_command`によるモジュール分割案からの検証コマンドの
抜き出し、を確認する。

実機のOllama/LM Studio/MLX-LMやシェルコマンドに依存しないよう、
`_run_project_command`・`_collect_answer_with_project_tools`を差し替えて
模擬する。

使い方: python3 tests/test_integration_verification.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import progress  # noqa: E402
import yoriai  # noqa: E402


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def _patched(obj, name, replacement):
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    return original


# ---------------------------------------------------------------------------
# _run_integration_verification: 試行回数の挙動
# ---------------------------------------------------------------------------

def test_succeeds_on_first_attempt_without_requesting_a_fix():
    run_calls = []
    fix_calls = []

    def fake_run(project_dir, command):
        run_calls.append(command)
        return json.dumps({"ok": True, "returncode": 0, "output": "OK"})

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "", None, False, []

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        success, output, attempts = yoriai._run_integration_verification(
            "python3 main.py", [_member("MacStudio", "qwen3-coder-30b")], "org-fp", "/tmp/proj",
            [("main.py", "...")],
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert success is True, (success, output, attempts)
    assert attempts == 1, attempts
    assert len(run_calls) == 1, run_calls
    assert len(fix_calls) == 0, "1回目で成功する場合、修正依頼は発生してはいけません"


def test_succeeds_on_second_attempt_after_one_fix():
    run_results = [
        json.dumps({"ok": False, "returncode": 1, "output": "Traceback: NameError"}),
        json.dumps({"ok": True, "returncode": 0, "output": "OK"}),
    ]
    run_calls = []
    fix_calls = []

    def fake_run(project_dir, command):
        run_calls.append(command)
        return run_results[len(run_calls) - 1]

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "直しました", None, False, ["main.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        success, output, attempts = yoriai._run_integration_verification(
            "python3 main.py", [_member("MacStudio", "qwen3-coder-30b")], "org-fp", "/tmp/proj",
            [("main.py", "...")],
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert success is True, (success, output, attempts)
    assert attempts == 2, attempts
    assert len(run_calls) == 2, run_calls
    assert len(fix_calls) == 1, "2回目で成功する場合、修正依頼は1回だけ発生するはずです"


def test_all_attempts_fail_without_infinite_retries():
    def fake_run(project_dir, command):
        return json.dumps({"ok": False, "returncode": 1, "output": "still broken"})

    fix_calls = []

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "直しました", None, False, ["main.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        success, output, attempts = yoriai._run_integration_verification(
            "python3 main.py", [_member("MacStudio", "qwen3-coder-30b")], "org-fp", "/tmp/proj",
            [("main.py", "...")], max_attempts=yoriai.MAX_VERIFY_ATTEMPTS,
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert success is False, "3回とも失敗した場合、成功として報告されてはいけません"
    assert attempts == yoriai.MAX_VERIFY_ATTEMPTS, attempts
    assert output == "still broken", output
    # MAX_VERIFY_ATTEMPTSを超えて無限に繰り返していないこと(修正依頼は
    # 試行回数-1回だけ発生する)。
    assert len(fix_calls) == yoriai.MAX_VERIFY_ATTEMPTS - 1, fix_calls


# ---------------------------------------------------------------------------
# _run_collaborative_project: スキップ条件・PROGRESS.mdへの記録
# ---------------------------------------------------------------------------

def _make_project_dir():
    return tempfile.mkdtemp(prefix="yoriai_integration_verification_test_")


def _complete_checklist(tasks):
    checklist = yoriai._build_task_checklist(tasks)
    for task in checklist:
        task["status"] = yoriai._TASK_STATUS_COMPLETED
    return checklist


def test_skips_verification_when_tasks_are_incomplete():
    tasks = [("main.py", "...")]
    checklist = yoriai._build_task_checklist(tasks)  # 生成直後は全てpending(未完了)
    project_dir = _make_project_dir()
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(1)
        return True, "OK", 1

    original = _patched(yoriai, "_run_integration_verification", fake_verify)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_project(
                "何か作って", tasks, checklist, [_member("MacStudio", "qwen3-coder-30b")], "org-fp",
                project_dir, tasks_to_queue=[], verify_command="python3 main.py",
            )
        output = buf.getvalue()
    finally:
        yoriai._run_integration_verification = original
        shutil.rmtree(project_dir, ignore_errors=True)

    assert len(verify_calls) == 0, "未完了タスクが残っている場合、統合検証は呼ばれてはいけません"
    assert "スキップ" in output, output


def test_skips_verification_when_no_verify_command():
    tasks = [("main.py", "...")]
    checklist = _complete_checklist(tasks)
    project_dir = _make_project_dir()
    verify_calls = []

    def fake_verify(*args, **kwargs):
        verify_calls.append(1)
        return True, "OK", 1

    original = _patched(yoriai, "_run_integration_verification", fake_verify)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_project(
                "何か作って", tasks, checklist, [_member("MacStudio", "qwen3-coder-30b")], "org-fp",
                project_dir, tasks_to_queue=[], verify_command="なし",
            )
        output = buf.getvalue()
    finally:
        yoriai._run_integration_verification = original
        shutil.rmtree(project_dir, ignore_errors=True)

    assert len(verify_calls) == 0, "検証コマンドが「なし」の場合、統合検証は呼ばれてはいけません"
    assert "スキップ" in output, output
    assert "[✅ 全タスク完了" in output, output


def test_records_successful_verification_in_progress_md_and_reports_completion():
    tasks = [("main.py", "...")]
    checklist = _complete_checklist(tasks)
    project_dir = _make_project_dir()

    original = _patched(yoriai, "_run_integration_verification", lambda *a, **k: (True, "OK", 2))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_project(
                "何か作って", tasks, checklist, [_member("MacStudio", "qwen3-coder-30b")], "org-fp",
                project_dir, tasks_to_queue=[], verify_command="python3 main.py",
            )
        output = buf.getvalue()
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
    finally:
        yoriai._run_integration_verification = original
        shutil.rmtree(project_dir, ignore_errors=True)

    assert "[✅ 全タスク完了" in output, output
    assert parsed is not None
    assert parsed["verify_command"] == "python3 main.py", parsed
    # 成功時は出力を残す実益が薄いため記録しない(_format_verification_result参照)。
    assert parsed["verification"] == {"success": True, "attempts": 2, "output": ""}, parsed


def test_records_failed_verification_and_reports_distinctly_not_as_all_complete():
    tasks = [("main.py", "...")]
    checklist = _complete_checklist(tasks)
    project_dir = _make_project_dir()

    original = _patched(yoriai, "_run_integration_verification", lambda *a, **k: (False, "still broken", 3))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_project(
                "何か作って", tasks, checklist, [_member("MacStudio", "qwen3-coder-30b")], "org-fp",
                project_dir, tasks_to_queue=[], verify_command="python3 main.py",
            )
        output = buf.getvalue()
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
    finally:
        yoriai._run_integration_verification = original
        shutil.rmtree(project_dir, ignore_errors=True)

    assert "[✅ 全タスク完了" not in output, output
    assert "統合検証に失敗しています" in output, output
    assert parsed is not None
    assert parsed["verification"]["success"] is False, parsed
    assert parsed["verification"]["attempts"] == 3, parsed
    assert "still broken" in parsed["verification"]["output"], parsed


# ---------------------------------------------------------------------------
# _project_has_pending_work: 失敗した統合検証を「未完了」として拾う
# ---------------------------------------------------------------------------

def test_project_has_pending_work_true_when_verification_failed():
    checklist = _complete_checklist([("main.py", "...")])
    parsed = {"checklist": checklist, "pending_fix_subtasks": [], "verification": {"success": False, "attempts": 3, "output": "x"}}
    assert yoriai._project_has_pending_work(parsed) is True


def test_project_has_pending_work_false_when_verification_succeeded():
    checklist = _complete_checklist([("main.py", "...")])
    parsed = {"checklist": checklist, "pending_fix_subtasks": [], "verification": {"success": True, "attempts": 1, "output": ""}}
    assert yoriai._project_has_pending_work(parsed) is False


def test_project_has_pending_work_false_when_verification_never_ran():
    checklist = _complete_checklist([("main.py", "...")])
    parsed = {"checklist": checklist, "pending_fix_subtasks": [], "verification": None}
    assert yoriai._project_has_pending_work(parsed) is False


# ---------------------------------------------------------------------------
# _extract_verify_command / _has_verify_command
# ---------------------------------------------------------------------------

def test_extract_verify_command_pulls_out_the_line():
    text = "main.py: エントリポイントを実装する。\n\n検証コマンド: python3 main.py\n"
    verify_command, remaining = yoriai._extract_verify_command(text)
    assert verify_command == "python3 main.py", verify_command
    assert "検証コマンド" not in remaining, remaining
    assert "main.py: エントリポイントを実装する。" in remaining, remaining


def test_extract_verify_command_returns_empty_when_absent():
    text = "main.py: エントリポイントを実装する。\n"
    verify_command, remaining = yoriai._extract_verify_command(text)
    assert verify_command == "", verify_command
    assert remaining.strip() == text.strip(), remaining


def test_has_verify_command_treats_none_variants_as_absent():
    assert yoriai._has_verify_command("python3 main.py") is True
    assert yoriai._has_verify_command("なし") is False
    assert yoriai._has_verify_command("") is False
    assert yoriai._has_verify_command(None) is False


# ---------------------------------------------------------------------------
# PROGRESS.mdの往復(フォーマット/パース)
# ---------------------------------------------------------------------------

def test_format_and_parse_verification_result_round_trip_success():
    verification = {"success": True, "attempts": 2, "output": "OK"}
    text = "\n".join(progress._format_verification_result(verification))
    parsed = progress._parse_verification_result(text)
    assert parsed == {"success": True, "attempts": 2, "output": ""}, parsed


def test_format_and_parse_verification_result_round_trip_failure():
    verification = {"success": False, "attempts": 3, "output": "Traceback (most recent call last):\nNameError"}
    text = "\n".join(progress._format_verification_result(verification))
    parsed = progress._parse_verification_result(text)
    assert parsed["success"] is False
    assert parsed["attempts"] == 3
    assert "NameError" in parsed["output"], parsed


def main():
    tests = [
        test_succeeds_on_first_attempt_without_requesting_a_fix,
        test_succeeds_on_second_attempt_after_one_fix,
        test_all_attempts_fail_without_infinite_retries,
        test_skips_verification_when_tasks_are_incomplete,
        test_skips_verification_when_no_verify_command,
        test_records_successful_verification_in_progress_md_and_reports_completion,
        test_records_failed_verification_and_reports_distinctly_not_as_all_complete,
        test_project_has_pending_work_true_when_verification_failed,
        test_project_has_pending_work_false_when_verification_succeeded,
        test_project_has_pending_work_false_when_verification_never_ran,
        test_extract_verify_command_pulls_out_the_line,
        test_extract_verify_command_returns_empty_when_absent,
        test_has_verify_command_treats_none_variants_as_absent,
        test_format_and_parse_verification_result_round_trip_success,
        test_format_and_parse_verification_result_round_trip_failure,
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
