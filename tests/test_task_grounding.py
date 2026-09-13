#!/usr/bin/env python3
"""実行検証グラウンディングループ(タスクキュー方式のレビュー完了直後、
実際にそのタスクを実行/テストして機械的に成否を判定する)を検証する。

背景: `//agree`のタスクキュー方式は、内容レビュー(最大2回、読むだけで
実行はしない)が「問題なし」と判定した時点でそのタスクを完了扱いに
していた。しかしローカルLLM同士のレビューの「合意」は、実際に動くかどうか
とは無関係に、もっともらしい無難な判定に収束しがちである。判定基準を
「モデルの合意・自己申告」から「実際の実行結果(終了コード)」に置き換える
ため、レビュー完了直後にそのタスクの担当ファイルを実際に実行し、失敗すれば
生のエラー内容をそのまま担当メンバーに渡して修正させ、再実行する
(`_run_task_grounding_verification`)。

ここでは(1)検証コマンドをタスク単位に絞り込めるかどうかの判定
(`_is_test_file`・`_pytest_invocation_prefix`・`_build_task_grounding_
commands`、「1関数・1テストケース」単位まで細分化するオプションを含む)、
(2)`_run_task_grounding_verification`の試行回数の挙動(実行結果のみで
成否を機械的に判定し、失敗時は生のエラーをそのまま次ラウンドのプロンプトに
含める。暴走防止のため上限回数で打ち切る)、(3)`_run_collaborative_task_
queue`へのタスクキュー方式への組み込み(レビュー完了ごとに実行し、
`grounding_results`に未解決分を記録する)、(4)`--fine-grained`フラグの
解釈(`_extract_task_granularity_flag`)、(5)PROGRESS.mdへの記録の往復
(`_format_progress_markdown`/`_parse_progress_markdown`)、を確認する。

実機のOllama/LM Studio/MLX-LMやシェルコマンドに依存しないよう、
`_run_project_command`・`_collect_answer_with_project_tools`・
`_stream_chat_from_candidate`を差し替えて模擬する。

使い方: python3 tests/test_task_grounding.py
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
# _is_test_file / _pytest_invocation_prefix / _extract_test_function_names
# ---------------------------------------------------------------------------

def test_is_test_file_matches_common_naming_patterns():
    assert yoriai._is_test_file("test_calculator.py") is True
    assert yoriai._is_test_file("tests/test_calculator.py") is True
    assert yoriai._is_test_file("calculator_test.py") is True
    assert yoriai._is_test_file("calculator.py") is False
    assert yoriai._is_test_file("tests/helpers.py") is False


def test_pytest_invocation_prefix_extracts_bare_pytest():
    assert yoriai._pytest_invocation_prefix("pytest") == "pytest"
    assert yoriai._pytest_invocation_prefix("pytest tests/") == "pytest"


def test_pytest_invocation_prefix_extracts_python_module_invocation():
    assert yoriai._pytest_invocation_prefix("python3 -m pytest -q") == "python3 -m pytest"


def test_pytest_invocation_prefix_returns_none_for_non_pytest_command():
    assert yoriai._pytest_invocation_prefix("python3 main.py") is None
    assert yoriai._pytest_invocation_prefix("cargo test") is None
    assert yoriai._pytest_invocation_prefix("") is None


def test_extract_test_function_names_dedup_and_preserve_order():
    code = (
        "def test_add():\n    pass\n\n"
        "def helper():\n    pass\n\n"
        "def test_subtract():\n    pass\n\n"
        "def test_add():\n    pass\n"  # 重複(再定義)は1件のみ扱う
    )
    assert yoriai._extract_test_function_names(code) == ["test_add", "test_subtract"]


def test_extract_test_function_names_empty_when_no_test_functions():
    assert yoriai._extract_test_function_names("def helper():\n    pass\n") == []


# ---------------------------------------------------------------------------
# _build_task_grounding_commands
# ---------------------------------------------------------------------------

def test_build_task_grounding_commands_file_granularity_targets_whole_file():
    commands = yoriai._build_task_grounding_commands(
        "pytest", "tests/test_calculator.py", "def test_add():\n    pass\n", yoriai.TASK_GRANULARITY_FILE,
    )
    assert commands == ["pytest tests/test_calculator.py"], commands


def test_build_task_grounding_commands_function_granularity_targets_each_function():
    code = "def test_add():\n    pass\n\ndef test_subtract():\n    pass\n"
    commands = yoriai._build_task_grounding_commands(
        "pytest", "tests/test_calculator.py", code, yoriai.TASK_GRANULARITY_FUNCTION,
    )
    assert commands == [
        "pytest tests/test_calculator.py::test_add",
        "pytest tests/test_calculator.py::test_subtract",
    ], commands


def test_build_task_grounding_commands_function_granularity_falls_back_to_file_without_functions():
    commands = yoriai._build_task_grounding_commands(
        "pytest", "tests/test_calculator.py", "x = 1\n", yoriai.TASK_GRANULARITY_FUNCTION,
    )
    assert commands == ["pytest tests/test_calculator.py"], commands


def test_build_task_grounding_commands_empty_when_not_test_file():
    commands = yoriai._build_task_grounding_commands(
        "pytest", "calculator.py", "def add(a, b):\n    return a + b\n", yoriai.TASK_GRANULARITY_FILE,
    )
    assert commands == [], commands


def test_build_task_grounding_commands_empty_when_no_verify_command():
    commands = yoriai._build_task_grounding_commands(
        "なし", "tests/test_calculator.py", "def test_add():\n    pass\n", yoriai.TASK_GRANULARITY_FILE,
    )
    assert commands == [], commands


def test_build_task_grounding_commands_empty_when_verify_command_is_not_pytest():
    commands = yoriai._build_task_grounding_commands(
        "python3 main.py", "tests/test_calculator.py", "def test_add():\n    pass\n", yoriai.TASK_GRANULARITY_FILE,
    )
    assert commands == [], commands


# ---------------------------------------------------------------------------
# _run_task_grounding_verification: 試行回数・成否判定の挙動
# ---------------------------------------------------------------------------

def test_run_task_grounding_verification_skips_when_not_applicable():
    ran, ok, output, attempts = yoriai._run_task_grounding_verification(
        "calculator.py", "def add(a, b):\n    return a + b\n", "plan", "pytest",
        _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj",
    )
    assert (ran, ok, output, attempts) == (False, True, "", 0)


def test_run_task_grounding_verification_succeeds_on_first_attempt_without_requesting_a_fix():
    run_calls = []
    fix_calls = []

    def fake_run(project_dir, command):
        run_calls.append(command)
        return json.dumps({"ok": True, "returncode": 0, "output": "1 passed"})

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "", None, False, []

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        ran, ok, output, attempts = yoriai._run_task_grounding_verification(
            "tests/test_calculator.py", "def test_add():\n    pass\n", "plan", "pytest",
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj",
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert (ran, ok, output, attempts) == (True, True, "", 1)
    assert len(run_calls) == 1, run_calls
    assert len(fix_calls) == 0, "1回目で成功する場合、修正依頼は発生してはいけません"


def test_run_task_grounding_verification_success_is_judged_by_exit_code_not_self_report():
    """モデルの自己申告(「これで動くはずです」等)では成否は判定されず、
    実行結果(`ok`/終了コード)のみで機械的に判定されることを確認する。
    """
    def fake_run(project_dir, command):
        return json.dumps({"ok": False, "returncode": 1, "output": "AssertionError: 1 != 2"})

    def fake_fix(*args, **kwargs):
        # 実際には直っていないのに、モデルが自己申告で「直しました。動作
        # するはずです」と主張しても、それだけでは成功と判定されない。
        return "直しました。これで動作するはずです。", None, False, ["tests/test_calculator.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        ran, ok, output, attempts = yoriai._run_task_grounding_verification(
            "tests/test_calculator.py", "def test_add():\n    pass\n", "plan", "pytest",
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj",
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert ran is True
    assert ok is False, "自己申告に関わらず、実行が失敗し続ける限り成功と判定してはいけません"
    assert "AssertionError" in output, output


def test_run_task_grounding_verification_succeeds_after_one_fix_with_raw_error_passed_through():
    run_results = [
        json.dumps({"ok": False, "returncode": 1, "output": "Traceback (most recent call last):\nAssertionError: boom"}),
        json.dumps({"ok": True, "returncode": 0, "output": "1 passed"}),
    ]
    run_calls = []
    fix_prompts = []

    def fake_run(project_dir, command):
        run_calls.append(command)
        return run_results[len(run_calls) - 1]

    def fake_fix(owner, org_fingerprint, messages, out_dir):
        fix_prompts.append(messages[0]["content"])
        return "直しました", None, False, ["tests/test_calculator.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        ran, ok, output, attempts = yoriai._run_task_grounding_verification(
            "tests/test_calculator.py", "def test_add():\n    pass\n", "plan", "pytest",
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj",
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert (ran, ok, output, attempts) == (True, True, "", 2)
    assert len(run_calls) == 2, run_calls
    assert len(fix_prompts) == 1, fix_prompts
    # 依頼への対応: 失敗時は要約・言い換えではない生のエラー内容
    # (スタックトレース)がそのまま次ラウンドのプロンプトに含まれること。
    assert "Traceback (most recent call last):" in fix_prompts[0], fix_prompts[0]
    assert "AssertionError: boom" in fix_prompts[0], fix_prompts[0]


def test_run_task_grounding_verification_stays_on_same_task_while_failing():
    """失敗時は同じタスクに留まり(次のタスクへは進まず)、実行→修正依頼→
    再実行が繰り返されることを確認する(呼び出し元は戻り値のokを見て
    次のタスクに進むかどうかを判断する設計のため、ここでは
    `_run_task_grounding_verification`自身がタスクを進めないこと、
    つまり同一ファイルに対して繰り返し実行・修正依頼が行われることを
    確認する)。
    """
    run_calls = []
    fix_calls = []

    def fake_run(project_dir, command):
        run_calls.append(command)
        return json.dumps({"ok": False, "returncode": 1, "output": "still broken"})

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "直しました", None, False, ["tests/test_calculator.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        ran, ok, output, attempts = yoriai._run_task_grounding_verification(
            "tests/test_calculator.py", "def test_add():\n    pass\n", "plan", "pytest",
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj", max_attempts=3,
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert ok is False
    assert attempts == 3
    assert run_calls == [f"pytest tests/test_calculator.py"] * 3, run_calls


def test_run_task_grounding_verification_all_attempts_fail_without_infinite_retries():
    def fake_run(project_dir, command):
        return json.dumps({"ok": False, "returncode": 1, "output": "still broken"})

    fix_calls = []

    def fake_fix(*args, **kwargs):
        fix_calls.append(1)
        return "直しました", None, False, ["tests/test_calculator.py"]

    original_run = _patched(yoriai, "_run_project_command", fake_run)
    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        ran, ok, output, attempts = yoriai._run_task_grounding_verification(
            "tests/test_calculator.py", "def test_add():\n    pass\n", "plan", "pytest",
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "/tmp/proj",
            max_attempts=yoriai.MAX_TASK_GROUNDING_ATTEMPTS,
        )
    finally:
        yoriai._run_project_command = original_run
        yoriai._collect_answer_with_project_tools = original_fix

    assert ran is True
    assert ok is False, "上限回数まで失敗し続けた場合、成功として報告されてはいけません"
    assert attempts == yoriai.MAX_TASK_GROUNDING_ATTEMPTS, attempts
    assert output == "still broken", output
    # 暴走防止: 修正依頼は「試行回数-1」回だけ発生し、無限に繰り返さない。
    assert len(fix_calls) == yoriai.MAX_TASK_GROUNDING_ATTEMPTS - 1, fix_calls


def test_default_max_task_grounding_attempts_matches_existing_runaway_prevention_pattern():
    # 依頼への対応: 既存の「レビューフェーズは最大2回まで」という暴走防止
    # 設計、および同じ発想を踏襲したMAX_CONTENT_VOLUME_FIX_ATTEMPTS・
    # MAX_BROWSER_FRONTEND_FIX_ATTEMPTSと同じデフォルト値(2回)にする。
    assert yoriai.MAX_TASK_GROUNDING_ATTEMPTS == 2
    assert yoriai.MAX_TASK_GROUNDING_ATTEMPTS == yoriai.MAX_CONTENT_VOLUME_FIX_ATTEMPTS
    assert yoriai.MAX_TASK_GROUNDING_ATTEMPTS == yoriai.MAX_BROWSER_FRONTEND_FIX_ATTEMPTS


# ---------------------------------------------------------------------------
# _extract_task_granularity_flag
# ---------------------------------------------------------------------------

def test_extract_task_granularity_flag_detects_trailing_flag_and_strips_it():
    granularity, cleaned = yoriai._extract_task_granularity_flag("ToDoアプリを作って --fine-grained")
    assert granularity == yoriai.TASK_GRANULARITY_FUNCTION
    assert cleaned == "ToDoアプリを作って", repr(cleaned)


def test_extract_task_granularity_flag_defaults_to_file_when_absent():
    granularity, cleaned = yoriai._extract_task_granularity_flag("ToDoアプリを作って")
    assert granularity == yoriai.TASK_GRANULARITY_FILE
    assert cleaned == "ToDoアプリを作って"


def test_extract_task_granularity_flag_handles_flag_only_request():
    granularity, cleaned = yoriai._extract_task_granularity_flag("--fine-grained")
    assert granularity == yoriai.TASK_GRANULARITY_FUNCTION
    assert cleaned == ""


# ---------------------------------------------------------------------------
# _run_collaborative_task_queue への組み込み
# ---------------------------------------------------------------------------

def _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
    text = messages[0]["content"]
    if "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\ndef test_add():\n    pass\n```"}
    yield {"done": True}


def _run_queue_with_grounding(tasks, candidates, verify_command, task_granularity=yoriai.TASK_GRANULARITY_FILE, fake_run_project_command=None):
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_ok
    original_run = yoriai._run_project_command
    if fake_run_project_command is not None:
        yoriai._run_project_command = fake_run_project_command

    checklist = yoriai._build_task_checklist(tasks)
    grounding_results = {}
    project_dir = tempfile.mkdtemp(prefix="yoriai_task_grounding_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_task_queue(
                tasks, candidates, "org-fp", project_dir, checklist,
                verify_command=verify_command, task_granularity=task_granularity,
                grounding_results=grounding_results,
            )
        return buf.getvalue(), project_dir, grounding_results
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        yoriai._run_project_command = original_run
        shutil.rmtree(project_dir, ignore_errors=True)


def test_task_queue_runs_grounding_after_review_and_clears_result_on_success():
    tasks = [("tests/test_calculator.py", "test_addを持つ")]
    candidates = [_member("MacStudio", "qwen3-coder-30b"), _member("Mini", "qwen3-14b")]

    def fake_run_project_command(project_dir, command):
        return json.dumps({"ok": True, "returncode": 0, "output": "1 passed"})

    output, project_dir, grounding_results = _run_queue_with_grounding(
        tasks, candidates, "pytest", fake_run_project_command=fake_run_project_command,
    )

    assert grounding_results == {}, grounding_results


def test_task_queue_records_unresolved_grounding_failure():
    tasks = [("tests/test_calculator.py", "test_addを持つ")]
    candidates = [_member("MacStudio", "qwen3-coder-30b"), _member("Mini", "qwen3-14b")]

    def fake_run_project_command(project_dir, command):
        return json.dumps({"ok": False, "returncode": 1, "output": "AssertionError: boom"})

    def fake_fix(*args, **kwargs):
        return "直しました", None, False, ["tests/test_calculator.py"]

    original_fix = _patched(yoriai, "_collect_answer_with_project_tools", fake_fix)
    try:
        output, project_dir, grounding_results = _run_queue_with_grounding(
            tasks, candidates, "pytest", fake_run_project_command=fake_run_project_command,
        )
    finally:
        yoriai._collect_answer_with_project_tools = original_fix

    assert "tests/test_calculator.py" in grounding_results, grounding_results
    assert "AssertionError: boom" in grounding_results["tests/test_calculator.py"]["output"]
    assert grounding_results["tests/test_calculator.py"]["attempts"] == yoriai.MAX_TASK_GROUNDING_ATTEMPTS


def test_task_queue_skips_grounding_when_no_verify_command():
    tasks = [("tests/test_calculator.py", "test_addを持つ")]
    candidates = [_member("MacStudio", "qwen3-coder-30b"), _member("Mini", "qwen3-14b")]

    calls = []

    def fake_run_project_command(project_dir, command):
        calls.append(command)
        return json.dumps({"ok": True, "returncode": 0, "output": "OK"})

    output, project_dir, grounding_results = _run_queue_with_grounding(
        tasks, candidates, "", fake_run_project_command=fake_run_project_command,
    )

    assert calls == [], "検証コマンドが無い場合、実行検証は呼ばれてはいけません"
    assert grounding_results == {}


# ---------------------------------------------------------------------------
# PROGRESS.mdへの記録の往復(フォーマット/パース)
# ---------------------------------------------------------------------------

def test_format_and_parse_grounding_results_round_trip():
    grounding_results = {
        "tests/test_calculator.py": {"attempts": 2, "output": "AssertionError: boom\nline 2"},
    }
    text = progress._format_progress_markdown(
        "何か作って", [("tests/test_calculator.py", "...")],
        yoriai._build_task_checklist([("tests/test_calculator.py", "...")]),
        {}, grounding_results=grounding_results,
    )
    parsed_dir = tempfile.mkdtemp(prefix="yoriai_grounding_progress_test_")
    try:
        path = os.path.join(parsed_dir, "PROGRESS.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        parsed = yoriai._parse_progress_markdown(path)
    finally:
        shutil.rmtree(parsed_dir, ignore_errors=True)

    assert parsed is not None
    assert parsed["grounding_results"] == grounding_results, parsed["grounding_results"]


def test_format_and_parse_grounding_results_empty_when_absent():
    text = progress._format_progress_markdown(
        "何か作って", [("main.py", "...")], yoriai._build_task_checklist([("main.py", "...")]), {},
    )
    assert progress._PROGRESS_SECTION_GROUNDING not in text
    assert progress._parse_grounding_results_markdown(text) == {}


def main():
    tests = [
        test_is_test_file_matches_common_naming_patterns,
        test_pytest_invocation_prefix_extracts_bare_pytest,
        test_pytest_invocation_prefix_extracts_python_module_invocation,
        test_pytest_invocation_prefix_returns_none_for_non_pytest_command,
        test_extract_test_function_names_dedup_and_preserve_order,
        test_extract_test_function_names_empty_when_no_test_functions,
        test_build_task_grounding_commands_file_granularity_targets_whole_file,
        test_build_task_grounding_commands_function_granularity_targets_each_function,
        test_build_task_grounding_commands_function_granularity_falls_back_to_file_without_functions,
        test_build_task_grounding_commands_empty_when_not_test_file,
        test_build_task_grounding_commands_empty_when_no_verify_command,
        test_build_task_grounding_commands_empty_when_verify_command_is_not_pytest,
        test_run_task_grounding_verification_skips_when_not_applicable,
        test_run_task_grounding_verification_succeeds_on_first_attempt_without_requesting_a_fix,
        test_run_task_grounding_verification_success_is_judged_by_exit_code_not_self_report,
        test_run_task_grounding_verification_succeeds_after_one_fix_with_raw_error_passed_through,
        test_run_task_grounding_verification_stays_on_same_task_while_failing,
        test_run_task_grounding_verification_all_attempts_fail_without_infinite_retries,
        test_default_max_task_grounding_attempts_matches_existing_runaway_prevention_pattern,
        test_extract_task_granularity_flag_detects_trailing_flag_and_strips_it,
        test_extract_task_granularity_flag_defaults_to_file_when_absent,
        test_extract_task_granularity_flag_handles_flag_only_request,
        test_task_queue_runs_grounding_after_review_and_clears_result_on_success,
        test_task_queue_records_unresolved_grounding_failure,
        test_task_queue_skips_grounding_when_no_verify_command,
        test_format_and_parse_grounding_results_round_trip,
        test_format_and_parse_grounding_results_empty_when_absent,
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
