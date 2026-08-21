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


def test_parse_fix_response_extracts_filename_and_code():
    answer = "FILE: utils.py\n```python\ndef generate_id():\n    return 1\n```"
    filename, code = yoriai._parse_fix_response(answer)
    assert filename == "utils.py"
    assert code == "def generate_id():\n    return 1\n"


def test_parse_fix_response_returns_none_filename_when_missing():
    filename, code = yoriai._parse_fix_response("```python\npass\n```")
    assert filename is None


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

def _fake_stream_fix_then_review_ok(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
    first_content = messages[0]["content"]
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    if "改修担当です" in first_content:
        if len(tool_messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "utils.py"}}},
            ]}
            return
        yield {"content": "FILE: utils.py\n```python\ndef generate_id():\n    return 'fixed-id'\n```"}
        yield {"done": True}
        return
    if "レビュー対象" in first_content:
        yield {"content": "問題なし"}
        yield {"done": True}
        return
    yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_fix_project_end_to_end_success_updates_file_and_changelog():
    """依頼の動作確認: 完成済みプロジェクトに修正依頼を送ると、正しい
    プロジェクトが特定され、対象ファイルだけが修正され、他のファイルには
    影響が出ないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_then_review_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli-storage-py-2",
            [("storage.py", "永続化を担当"), ("utils.py", "共通処理を担当。IDの生成関数generate_idを実装する。")],
            files={"storage.py": "def load():\n    pass\n", "utils.py": "def generate_id():\n    return 1\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "ID生成のロジックにバグがあるので直して", out_dir,
            )
        output = buf.getvalue()

        assert project_dir in output, output
        assert "[✅ utils.py の修正が完了しました" in output, output

        with open(os.path.join(project_dir, "utils.py"), encoding="utf-8") as f:
            utils_content = f.read()
        assert "fixed-id" in utils_content, utils_content

        with open(os.path.join(project_dir, "storage.py"), encoding="utf-8") as f:
            storage_content = f.read()
        assert storage_content == "def load():\n    pass\n", (
            f"他のファイル(storage.py)には影響が出ないはずです: {storage_content!r}"
        )

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        assert "utils.py" in parsed["changelog"][0]
        assert "ID生成のロジックにバグがあるので直して" in parsed["changelog"][0]
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
    yoriai._stream_chat_from_candidate = _fake_stream_fix_then_review_ok

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
        assert "[✅ utils.py の修正が完了しました" in output, output
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


def test_fix_project_rejects_filename_not_in_project():
    """モデルが存在しない/プロジェクト外のファイル名を返した場合、安全に
    拒否し、何も書き換えないことを確認する。
    """
    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        yield {"content": "FILE: ../../etc/passwd\n```\nmalicious\n```"}
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
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: 何かを直して", out_dir)
        output = buf.getvalue()

        assert "修正対象のファイルを特定できませんでした" in output, output
        assert not os.path.exists(os.path.join(out_dir, "etc", "passwd"))
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["changelog"] == [], "書き換えが拒否された場合、更新履歴も追記されないはずです"
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_records_review_failure_in_changelog_but_still_saves_file():
    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        first_content = messages[0]["content"]
        if "改修担当です" in first_content:
            yield {"content": "FILE: utils.py\n```python\ndef generate_id():\n    return 1\n```"}
            yield {"done": True}
            return
        if "レビュー対象" in first_content:
            yield {"content": "問題あり\nまだ直っていません。"}
            yield {"done": True}
            return
        # 修正依頼(_request_fix)への応答: 何度直しても解決しない状況を再現する。
        yield {"content": "```python\ndef generate_id():\n    return 1\n```"}
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

        assert "[⚠️ utils.py の修正はレビューを通過しませんでした" in output, output
        assert os.path.isfile(os.path.join(project_dir, "utils.py")), "レビューに通らなくてもファイルへの反映は行われる"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1
        assert "レビューを通過しませんでした" in parsed["changelog"][0], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_skips_llm_review_but_runs_syntax_check_with_single_member():
    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        yield {"content": "FILE: utils.py\n```python\ndef generate_id(:\n    pass\n```"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _one_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: 構文が壊れているので直して", out_dir)
        output = buf.getvalue()

        assert "レビュー担当者がいません" in output, output
        assert "[🔍 構文チェック:" in output, output
        assert "[⚠️ utils.py の修正はレビューを通過しませんでした" in output, (
            f"意図的に構文エラーのコードを与えたので、構文チェックで弾かれるはずです: {output}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
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
        test_parse_fix_response_extracts_filename_and_code,
        test_parse_fix_response_returns_none_filename_when_missing,
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
        test_fix_project_end_to_end_success_updates_file_and_changelog,
        test_fix_project_explicit_syntax_bypasses_auto_identification,
        test_fix_project_reports_ambiguous_candidates_without_modifying_anything,
        test_fix_project_reports_not_found_without_modifying_anything,
        test_fix_project_refuses_when_target_project_is_incomplete,
        test_fix_project_rejects_filename_not_in_project,
        test_fix_project_records_review_failure_in_changelog_but_still_saves_file,
        test_fix_project_skips_llm_review_but_runs_syntax_check_with_single_member,
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
