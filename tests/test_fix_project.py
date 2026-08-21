#!/usr/bin/env python3
"""既存の完成済みプロジェクトへの修正依頼(`//fix`、自動判定される
`EXECUTION_MODE_FIX_PROJECT`)を検証する。

これまでの協業モードは「新規のプロジェクトをゼロから作る」ことしか
できず、既に完成したプロジェクトに対して「ここを直して」という修正
依頼を送る手段が無かった。Claude CodeにおけるRead/Glob/Grep/Editの
ような、既存ファイルに対する検索・部分修正の仕組みを導入した。

(1)プロジェクト特定(依頼文とPROGRESS.mdの照合、複数候補・未検出時の
確認)、(2)既存のread_fileツール(`_collect_review_answer_with_
read_file`)を流用したファイル一覧・内容確認、(3)対象ファイルだけを
書き換える軽量な修正モード、(4)既存の構文チェック・レビューフェーズ
(`_review_and_fix_one_file`)を通してからの反映、(5)PROGRESS.mdへの
更新履歴の追記、を検証する。

使い方: python3 tests/test_fix_project.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


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


def _one_member_snapshot():
    return {"self": _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40), "peers": []}


def _write_completed_project(projects_root, name, tasks, request="ToDoリストのCLIツールを作って", files=None):
    """テスト用に、全タスクが完了済みのプロジェクト(PROGRESS.md +
    実ファイル)を作る。
    """
    checklist = yoriai._build_task_checklist(tasks)
    for filename, _content in tasks:
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    project_dir = os.path.join(projects_root, name)
    yoriai._write_progress_md(project_dir, request, tasks, checklist, {})
    files = files or {filename: "pass\n" for filename, _content in tasks}
    for filename, content in files.items():
        with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as f:
            f.write(content)
    return project_dir


def _write_incomplete_project(projects_root, name, tasks, request="何かを作って"):
    checklist = yoriai._build_task_checklist(tasks)
    project_dir = os.path.join(projects_root, name)
    yoriai._write_progress_md(project_dir, request, tasks, checklist, {})
    return project_dir


# ---------------------------------------------------------------------------
# 純粋関数のテスト
# ---------------------------------------------------------------------------

def test_text_similarity_score_rewards_shared_substrings():
    high = yoriai._text_similarity_score("ID生成のロジックにバグがある", "IDの生成関数generate_idを実装する")
    low = yoriai._text_similarity_score("ID生成のロジックにバグがある", "天気予報を取得するAPIクライアント")
    assert high > low, (high, low)


def test_text_similarity_score_is_zero_for_empty_text():
    assert yoriai._text_similarity_score("", "何か") == 0


def test_parse_changelog_markdown_returns_nonempty_lines():
    lines = yoriai._parse_changelog_markdown("- 2026-08-21: 修正 (a.py)\n\n- 2026-08-22: 別の修正 (b.py)")
    assert lines == ["- 2026-08-21: 修正 (a.py)", "- 2026-08-22: 別の修正 (b.py)"], lines


def test_progress_markdown_round_trips_changelog():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        changelog = ["- 2026-08-21: IDの生成ロジックの修正 (a.py)"]
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {}, changelog=changelog)
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    assert parsed["changelog"] == changelog, parsed


def test_progress_markdown_changelog_defaults_to_empty_list():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    assert parsed["changelog"] == []


def test_list_project_files_excludes_progress_md():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        for name in ("a.py", "b.py", yoriai.PROGRESS_FILENAME):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                f.write("x")
        assert yoriai._list_project_files(out_dir) == ["a.py", "b.py"]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# プロジェクトファイル操作ツール(_resolve_safe_project_path等)
# ---------------------------------------------------------------------------

def test_resolve_safe_project_path_accepts_flat_filename():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, "utils.py")
        assert error is None, error
        assert path == os.path.join(out_dir, "utils.py")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_safe_project_path_rejects_path_traversal():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, "../../etc/passwd")
        assert path is None
        assert error is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_safe_project_path_rejects_absolute_path_to_yoriai_itself():
    """依頼の項目5(最優先の安全要件): Yoriai本体のファイルには絶対に
    アクセスできないことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        yoriai_py_path = os.path.abspath(yoriai.__file__)
        path, error = yoriai._resolve_safe_project_path(out_dir, yoriai_py_path)
        assert path is None
        assert error is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_safe_project_path_rejects_progress_md():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, yoriai.PROGRESS_FILENAME)
        assert path is None
        assert error is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_write_project_file_creates_file_and_reports_syntax_ok():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        result = json.loads(yoriai._write_project_file(out_dir, "utils.py", "def f():\n    return 1\n"))
        assert result["ok"] is True, result
        assert result["syntax_ok"] is True, result
        with open(os.path.join(out_dir, "utils.py"), encoding="utf-8") as f:
            assert f.read() == "def f():\n    return 1\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_write_project_file_reports_syntax_error_but_still_writes():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        result = json.loads(yoriai._write_project_file(out_dir, "utils.py", "def f(:\n    pass\n"))
        assert result["ok"] is True, result
        assert result["syntax_ok"] is False, result
        assert "syntax_error" in result, result
        assert os.path.isfile(os.path.join(out_dir, "utils.py"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_write_project_file_rejects_path_traversal_and_writes_nothing():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        result = json.loads(yoriai._write_project_file(out_dir, "../outside.py", "malicious = True\n"))
        assert result["ok"] is False, result
        assert not os.path.exists(os.path.join(os.path.dirname(out_dir), "outside.py"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_move_project_file_renames_within_project_dir():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "utils.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    pass\n")
        result = json.loads(yoriai._move_project_file(out_dir, "utils.py", "helpers.py"))
        assert result["ok"] is True, result
        assert not os.path.exists(os.path.join(out_dir, "utils.py"))
        assert os.path.isfile(os.path.join(out_dir, "helpers.py"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_move_project_file_rejects_traversal_in_either_name():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "utils.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    pass\n")
        result = json.loads(yoriai._move_project_file(out_dir, "utils.py", "../../outside.py"))
        assert result["ok"] is False, result
        assert os.path.isfile(os.path.join(out_dir, "utils.py")), "拒否された場合、元のファイルはそのまま残るはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_delete_project_file_logs_to_progress_md_before_deleting():
    """依頼の項目4: 削除は実行前にPROGRESS.mdへ記録される(=削除後の
    PROGRESS.mdには必ず記録が残っている)ことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        tasks = [("utils.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        with open(os.path.join(out_dir, "utils.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    pass\n")

        result = json.loads(yoriai._delete_project_file(out_dir, "utils.py"))
        assert result["ok"] is True, result
        assert not os.path.exists(os.path.join(out_dir, "utils.py"))

        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        assert "utils.py" in parsed["changelog"][0] and "削除" in parsed["changelog"][0]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_delete_project_file_rejects_progress_md_itself():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        tasks = [("a.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        result = json.loads(yoriai._delete_project_file(out_dir, yoriai.PROGRESS_FILENAME))
        assert result["ok"] is False, result
        assert os.path.isfile(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_make_project_directory_creates_subdirectory():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        result = json.loads(yoriai._make_project_directory(out_dir, "sub"))
        assert result["ok"] is True, result
        assert os.path.isdir(os.path.join(out_dir, "sub"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_list_project_directory_excludes_progress_md():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        for name in ("a.py", "b.py", yoriai.PROGRESS_FILENAME):
            with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
                f.write("x")
        result = json.loads(yoriai._list_project_directory(out_dir))
        assert result["files"] == ["a.py", "b.py"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_parse_run_test_command_allows_only_whitelisted_forms():
    assert yoriai._parse_run_test_command("python3 test_utils.py") == (["python3", "test_utils.py"], None)
    assert yoriai._parse_run_test_command("pytest") == (["pytest"], None)
    assert yoriai._parse_run_test_command("pytest test_utils.py") == (["pytest", "test_utils.py"], None)

    argv, error = yoriai._parse_run_test_command("rm -rf /")
    assert argv is None
    assert error is not None

    argv, error = yoriai._parse_run_test_command("python3 a.py; rm -rf /")
    assert argv is None, "セミコロンを含む文字列はホワイトリストの2トークン形式に一致しないため拒否されるはずです"


def test_run_project_test_command_executes_passing_test():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "test_ok.py"), "w", encoding="utf-8") as f:
            f.write("assert 1 + 1 == 2\nprint('OK')\n")
        result = json.loads(yoriai._run_project_test_command(out_dir, "python3 test_ok.py"))
        assert result["ok"] is True, result
        assert "OK" in result["output"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_reports_failure_output():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "test_fail.py"), "w", encoding="utf-8") as f:
            f.write("assert 1 + 1 == 3, 'broken math'\n")
        result = json.loads(yoriai._run_project_test_command(out_dir, "python3 test_fail.py"))
        assert result["ok"] is False, result
        assert "broken math" in result["output"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_rejects_disallowed_command_without_executing():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        marker = os.path.join(out_dir, "should_not_exist")
        result = json.loads(yoriai._run_project_test_command(out_dir, f"touch {marker}"))
        assert result["ok"] is False, result
        assert not os.path.exists(marker), "ホワイトリスト外のコマンドは実行されないはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_syntax_check_all_python_files_reports_broken_files_only():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "ok.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        with open(os.path.join(out_dir, "broken.py"), "w", encoding="utf-8") as f:
            f.write("def f(:\n    pass\n")
        with open(os.path.join(out_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("this is not python (:\n")
        broken = yoriai._syntax_check_all_python_files(out_dir)
        assert broken == ["broken.py"], broken
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_execute_project_tool_call_dispatches_to_write_file():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        tool_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "write_file", "arguments": {"filename": "a.py", "content": "x = 1\n"}},
        }
        result = json.loads(yoriai._execute_project_tool_call(out_dir, tool_call))
        assert result["ok"] is True, result
        with open(os.path.join(out_dir, "a.py"), encoding="utf-8") as f:
            assert f.read() == "x = 1\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# プロジェクト特定(_identify_target_project)
# ---------------------------------------------------------------------------

def test_identify_target_project_matches_unique_high_scoring_project():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        target = _write_completed_project(
            projects_root, "todo-cli", [
                ("storage.py", "永続化を担当"),
                ("utils.py", "共通処理を担当。IDの生成関数generate_idを実装する。"),
            ],
        )
        _write_completed_project(
            projects_root, "weather-cli", [("api.py", "天気予報を取得するAPIクライアント")],
            request="天気予報を調べるCLIツールを作って",
        )

        status, dirs = yoriai._identify_target_project("ID生成のロジックにバグがあるので直して", out_dir)
        assert status == "matched", (status, dirs)
        assert dirs == [target], dirs
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_identify_target_project_reports_ambiguous_on_tie():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli-1", [("a.py", "ToDoリストのCLIツールを担当")])
        _write_completed_project(projects_root, "todo-cli-2", [("a.py", "ToDoリストのCLIツールを担当")])

        status, dirs = yoriai._identify_target_project("ToDoリストのCLIツールを直して", out_dir)
        assert status == "ambiguous", (status, dirs)
        assert len(dirs) == 2, dirs
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_identify_target_project_reports_not_found_when_no_overlap():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("a.py", "ToDoリストを担当")])

        status, dirs = yoriai._identify_target_project("xyzzy plugh quux", out_dir)
        assert status == "not_found", (status, dirs)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_identify_target_project_ignores_incomplete_projects():
    """未完了のプロジェクトは、たとえ依頼文と強く一致していても候補に
    含めない(未完了プロジェクトは//resume-allの役割であり、修正依頼の
    対象は完成済みプロジェクトに限定するため)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_incomplete_project(
            projects_root, "todo-cli", [("utils.py", "IDの生成関数generate_idを実装する")],
        )

        status, dirs = yoriai._identify_target_project("ID生成のロジックにバグがあるので直して", out_dir)
        assert status == "not_found", (status, dirs)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_identify_target_project_not_found_when_no_projects_dir():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        status, dirs = yoriai._identify_target_project("何かを直して", out_dir)
        assert status == "not_found"
        assert dirs == []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_explicit_fix_target_matches_existing_project_name():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("a.py", "説明")])

        project_dir, rest = yoriai._resolve_explicit_fix_target("todo-cli: ID生成を直して", out_dir)
        assert project_dir == os.path.join(projects_root, "todo-cli"), project_dir
        assert rest == "ID生成を直して", repr(rest)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_explicit_fix_target_falls_through_for_unknown_prefix():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        project_dir, rest = yoriai._resolve_explicit_fix_target("ID生成のロジックにバグがあるので直して", out_dir)
        assert project_dir is None
        assert rest == "ID生成のロジックにバグがあるので直して"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 実行モードの自動判定への統合
# ---------------------------------------------------------------------------

def test_classify_execution_mode_without_out_dir_never_returns_fix_project():
    """既存の呼び出し元・テストとの後方互換の確認: `out_dir`を渡さない
    場合、修正依頼キーワードが含まれていてもFIX_PROJECTにはならない。
    """
    assert yoriai._classify_execution_mode("バグがあるので直して") == yoriai.EXECUTION_MODE_SINGLE


def test_classify_execution_mode_returns_fix_project_when_project_exists():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("a.py", "説明")])
        mode = yoriai._classify_execution_mode("ID生成のロジックにバグがあるので直して", out_dir)
        assert mode == yoriai.EXECUTION_MODE_FIX_PROJECT, mode
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_classify_execution_mode_does_not_return_fix_project_without_any_completed_project():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        mode = yoriai._classify_execution_mode("バグがあるので直して", out_dir)
        assert mode != yoriai.EXECUTION_MODE_FIX_PROJECT, mode
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _ask_organization_fix_project(統合テスト)
# ---------------------------------------------------------------------------

def _tool_round(messages):
    """会話履歴中のtoolメッセージ数(=これまでに完了したツール呼び出し
    ラウンド数)を返す。フェイクの/chat応答が「次に何をするか」を、
    単純な会話の長さの数え上げで判断するために使う。
    """
    return len([m for m in messages if m.get("role") == "tool"])


def _fake_stream_fix_with_tools(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    """依頼の動作確認のシナリオ(リネーム→import修正→テスト実行)を
    模したフェイクの/chat応答。read_file→move_file→write_file→
    run_test→最終報告、の順にツール呼び出しラウンドを進める。
    """
    n = _tool_round(messages)
    if n == 0:
        yield {"pending_tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "cli.py"}}},
        ]}
        return
    if n == 1:
        yield {"pending_tool_calls": [
            {"id": "call_2", "type": "function", "function": {
                "name": "move_file", "arguments": {"old_filename": "utils.py", "new_filename": "helpers.py"},
            }},
        ]}
        return
    if n == 2:
        yield {"pending_tool_calls": [
            {"id": "call_3", "type": "function", "function": {
                "name": "write_file",
                "arguments": {
                    "filename": "cli.py",
                    "content": "from helpers import generate_id\n\ndef main():\n    return generate_id()\n",
                },
            }},
        ]}
        return
    if n == 3:
        yield {"pending_tool_calls": [
            {"id": "call_4", "type": "function", "function": {"name": "run_test", "arguments": {"command": "pytest"}}},
        ]}
        return
    yield {"content": "utils.pyをhelpers.pyにリネームし、cli.pyのimportを修正してテストを実行しました。"}
    yield {"done": True}


def _fake_stream_fix_simple_write(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    if _tool_round(messages) == 0:
        yield {"pending_tool_calls": [
            {"id": "call_1", "type": "function", "function": {
                "name": "write_file",
                "arguments": {"filename": "utils.py", "content": "def generate_id():\n    return 'fixed-id'\n"},
            }},
        ]}
        return
    yield {"content": "utils.pyのID生成ロジックを修正しました。"}
    yield {"done": True}


def test_fix_project_end_to_end_uses_tools_to_rename_edit_and_test():
    """依頼の動作確認: 完成済みプロジェクトに「ファイル名をutils.pyから
    helpers.pyに変更して、それに合わせてimportも直して、テストがあれば
    実行して確認して」のような依頼を送ると、正しいプロジェクトが特定され、
    複数のツール(read_file・move_file・write_file・run_test)を使って
    修正が行われ、無関係なファイルには影響が出ないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_with_tools

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli-storage-py-2",
            [("storage.py", "永続化を担当"), ("utils.py", "IDの生成を担当"), ("cli.py", "コマンドライン操作を担当")],
            files={
                "storage.py": "def load():\n    pass\n",
                "utils.py": "def generate_id():\n    return 1\n",
                "cli.py": "from utils import generate_id\n\ndef main():\n    return generate_id()\n",
            },
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint",
                "ファイル名をutils.pyからhelpers.pyに変更して、それに合わせてimportも直して、テストがあれば実行して確認して",
                out_dir,
            )
        output = buf.getvalue()

        assert project_dir in output, output
        assert "[✅ 修正が完了しました" in output, output

        assert not os.path.exists(os.path.join(project_dir, "utils.py")), "リネーム元は残らないはずです"
        assert os.path.isfile(os.path.join(project_dir, "helpers.py"))
        with open(os.path.join(project_dir, "cli.py"), encoding="utf-8") as f:
            cli_content = f.read()
        assert "from helpers import generate_id" in cli_content, cli_content

        with open(os.path.join(project_dir, "storage.py"), encoding="utf-8") as f:
            storage_content = f.read()
        assert storage_content == "def load():\n    pass\n", (
            f"無関係なファイル(storage.py)には影響が出ないはずです: {storage_content!r}"
        )

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        assert "utils.py" in parsed["changelog"][0], parsed["changelog"]
        # 修正がチェックリスト(元の実装計画の完了状態)には影響しないこと。
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is False
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_explicit_syntax_bypasses_auto_identification():
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_simple_write

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli-1", [("utils.py", "IDの生成関数generate_idを実装する")],
            files={"utils.py": "def generate_id():\n    return 1\n"},
        )
        _write_completed_project(
            projects_root, "todo-cli-2", [("utils.py", "IDの生成関数generate_idを実装する")],
            files={"utils.py": "def generate_id():\n    return 1\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "todo-cli-1: ID生成のロジックにバグがあるので直して", out_dir,
            )
        output = buf.getvalue()

        assert project_dir in output, output
        assert "[✅ 修正が完了しました" in output, output
        with open(os.path.join(project_dir, "utils.py"), encoding="utf-8") as f:
            assert "fixed-id" in f.read()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_reports_ambiguous_candidates_without_modifying_anything():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli-1", [("a.py", "ToDoリストのCLIツールを担当")])
        _write_completed_project(projects_root, "todo-cli-2", [("a.py", "ToDoリストのCLIツールを担当")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "ToDoリストのCLIツールを直して", out_dir)
        output = buf.getvalue()

        assert "複数のプロジェクトが候補に挙がりました" in output, output
        assert "todo-cli-1" in output and "todo-cli-2" in output, output
        assert f"{yoriai.FIX_PROJECT_COMMAND} " in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_reports_not_found_without_modifying_anything():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "xyzzy plugh quuxを直して", out_dir)
        output = buf.getvalue()
        assert "見つかりませんでした" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_refuses_when_target_project_is_incomplete():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_incomplete_project(projects_root, "todo-cli", [("a.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: 何かを直して", out_dir)
        output = buf.getvalue()
        assert "未完了のタスクが残っています" in output, output
        assert yoriai.RESUME_ALL_COMMAND in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_end_to_end_rejects_path_traversal_tool_calls():
    """モデルの応答(悪意ある、または壊れた応答)がプロジェクト外への
    パスを指定しても、ツール実行の安全対策(_resolve_safe_project_path)
    により実際には何も書き込まれないことを確認する(依頼の項目2・5)。
    """
    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        if _tool_round(messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": {"filename": "../../etc/passwd", "content": "malicious\n"},
                }},
            ]}
            return
        yield {"content": "書き込みを試みました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: 何かを直して", out_dir)

        assert not os.path.exists(os.path.join(out_dir, "etc", "passwd"))
        assert not os.path.exists(os.path.join(os.path.dirname(out_dir), "etc", "passwd"))
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_reports_syntax_errors_remaining_after_fix():
    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        if _tool_round(messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": {"filename": "utils.py", "content": "def generate_id(:\n    pass\n"},
                }},
            ]}
            return
        yield {"content": "utils.pyを修正しました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: バグを直して", out_dir)
        output = buf.getvalue()

        assert "[⚠️ 修正は保存されましたが、構文エラーが残っているファイルがあります: utils.py]" in output, output
        assert os.path.isfile(os.path.join(project_dir, "utils.py")), "構文エラーがあってもファイルへの反映は行われる"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1
        assert "構文エラー" in parsed["changelog"][0], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_works_with_single_member_present():
    """依頼のコマンド不要化・単純化の一環として、修正依頼はレビュー
    担当の有無に関係なく1台構成でも動作することを確認する(以前の
    LLMレビューによる相互チェックは廃止し、機械的な構文チェックのみに
    一本化したため、担当メンバーが1台か複数台かで挙動が分岐しない)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _one_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_simple_write

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: バグを直して", out_dir)
        output = buf.getvalue()

        assert "[✅ 修正が完了しました" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_end_to_end_delete_records_two_changelog_entries():
    """delete_fileツールが実行前にPROGRESS.mdへ記録するエントリ(依頼の
    項目4)と、_ask_organization_fix_project側が最後に記録する修正依頼
    全体のまとめエントリの、両方が失われず残ることを確認する。
    """
    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        if _tool_round(messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "delete_file", "arguments": {"filename": "legacy.py"},
                }},
            ]}
            return
        yield {"content": "不要になったlegacy.pyを削除しました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli", [("utils.py", "説明"), ("legacy.py", "説明")],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: legacy.pyを削除して", out_dir)

        assert not os.path.exists(os.path.join(project_dir, "legacy.py"))
        assert os.path.isfile(os.path.join(project_dir, "utils.py")), "無関係なファイルは残るはずです"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 2, parsed["changelog"]
        assert "削除" in parsed["changelog"][0], parsed["changelog"]
        assert "legacy.pyを削除して" in parsed["changelog"][1], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collect_answer_with_project_tools_stops_at_round_cap():
    """モデルが延々とツール呼び出しを繰り返すループに陥っても、
    MAX_PROJECT_TOOL_ROUNDSで確実に打ち切られることを確認する
    (暴走防止)。
    """
    def fake_stream_never_stops(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        yield {"pending_tool_calls": [
            {"id": "call_x", "type": "function", "function": {"name": "list_dir", "arguments": {}}},
        ]}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream_never_stops

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        candidate = {"label": "MacStudio", "model": "m", "address": "127.0.0.1", "port": 47120}
        content, error = yoriai._collect_answer_with_project_tools(
            candidate, "fingerprint", [{"role": "user", "content": "何か直して"}], out_dir,
        )
        assert content == ""
        assert error is not None and "上限" in error, error
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resume_project_preserves_changelog_from_a_prior_fix():
    """回帰検知: 修正依頼で追記した更新履歴が、その後の(無関係な)
    `//resume-all`によるPROGRESS.mdの書き換えで消えないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()

    def fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            yield {"content": "問題なし"}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._stream_chat_from_candidate = fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        tasks = [("a.py", "説明A"), ("b.py", "説明B")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._set_task_status(checklist, "a.py", "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, "a.py", "review", yoriai._TASK_STATUS_COMPLETED)
        # b.pyは未完了のまま(//resume-allで再開する対象を作る)。
        project_dir = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME, "todo-cli")
        changelog = ["- 2026-08-20: 過去の修正 (a.py)"]
        yoriai._write_progress_md(project_dir, "何か作って", tasks, checklist, {}, changelog=changelog)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._resume_project(project_dir, 47120, "fingerprint")

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["changelog"] == changelog, (
            f"//resume-all経由の更新でも、過去の修正履歴が保持されるはずです: {parsed['changelog']}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_text_similarity_score_rewards_shared_substrings,
        test_text_similarity_score_is_zero_for_empty_text,
        test_parse_changelog_markdown_returns_nonempty_lines,
        test_progress_markdown_round_trips_changelog,
        test_progress_markdown_changelog_defaults_to_empty_list,
        test_list_project_files_excludes_progress_md,
        test_resolve_safe_project_path_accepts_flat_filename,
        test_resolve_safe_project_path_rejects_path_traversal,
        test_resolve_safe_project_path_rejects_absolute_path_to_yoriai_itself,
        test_resolve_safe_project_path_rejects_progress_md,
        test_write_project_file_creates_file_and_reports_syntax_ok,
        test_write_project_file_reports_syntax_error_but_still_writes,
        test_write_project_file_rejects_path_traversal_and_writes_nothing,
        test_move_project_file_renames_within_project_dir,
        test_move_project_file_rejects_traversal_in_either_name,
        test_delete_project_file_logs_to_progress_md_before_deleting,
        test_delete_project_file_rejects_progress_md_itself,
        test_make_project_directory_creates_subdirectory,
        test_list_project_directory_excludes_progress_md,
        test_parse_run_test_command_allows_only_whitelisted_forms,
        test_run_project_test_command_executes_passing_test,
        test_run_project_test_command_reports_failure_output,
        test_run_project_test_command_rejects_disallowed_command_without_executing,
        test_syntax_check_all_python_files_reports_broken_files_only,
        test_execute_project_tool_call_dispatches_to_write_file,
        test_identify_target_project_matches_unique_high_scoring_project,
        test_identify_target_project_reports_ambiguous_on_tie,
        test_identify_target_project_reports_not_found_when_no_overlap,
        test_identify_target_project_ignores_incomplete_projects,
        test_identify_target_project_not_found_when_no_projects_dir,
        test_resolve_explicit_fix_target_matches_existing_project_name,
        test_resolve_explicit_fix_target_falls_through_for_unknown_prefix,
        test_classify_execution_mode_without_out_dir_never_returns_fix_project,
        test_classify_execution_mode_returns_fix_project_when_project_exists,
        test_classify_execution_mode_does_not_return_fix_project_without_any_completed_project,
        test_fix_project_end_to_end_uses_tools_to_rename_edit_and_test,
        test_fix_project_explicit_syntax_bypasses_auto_identification,
        test_fix_project_reports_ambiguous_candidates_without_modifying_anything,
        test_fix_project_reports_not_found_without_modifying_anything,
        test_fix_project_refuses_when_target_project_is_incomplete,
        test_fix_project_end_to_end_rejects_path_traversal_tool_calls,
        test_fix_project_reports_syntax_errors_remaining_after_fix,
        test_fix_project_works_with_single_member_present,
        test_fix_project_end_to_end_delete_records_two_changelog_entries,
        test_collect_answer_with_project_tools_stops_at_round_cap,
        test_resume_project_preserves_changelog_from_a_prior_fix,
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
