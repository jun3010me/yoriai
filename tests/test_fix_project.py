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


def test_syntax_check_all_files_reports_broken_files_only():
    """未対応の拡張子(notes.txt)はスキップされ、"broken"扱いにはならない
    ことも合わせて確認する(言語非依存化に伴う名称・挙動の更新)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "ok.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    return 1\n")
        with open(os.path.join(out_dir, "broken.py"), "w", encoding="utf-8") as f:
            f.write("def f(:\n    pass\n")
        with open(os.path.join(out_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("this is not python (:\n")
        broken = yoriai._syntax_check_all_files(out_dir)
        assert broken == ["broken.py"], broken
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# サブディレクトリ内のファイル操作(バグ報告への対応)
# ---------------------------------------------------------------------------
#
# 実機のバグ報告: make_directoryでtemplates・lessons等のサブディレクトリを
# 作成できるにもかかわらず、write_fileはディレクトリ区切りを含む名前を
# 一律拒否していたため、モデルがサブディレクトリ内にファイルを書き込もう
# とし続けても常に失敗し、結果としてディレクトリだけが空のまま残っていた。
# ログを見る限りモデル自身のコーディング能力に問題は無く、Yoriai側の
# ファイルパスの制約が原因だったため、書き込み側を緩和して修正する。

def test_resolve_safe_project_path_accepts_subdirectory_relative_path():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, "templates/base.html")
        assert error is None, error
        assert path == os.path.join(out_dir, "templates", "base.html")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_safe_project_path_still_rejects_parent_directory_reference_inside_subpath():
    """サブディレクトリ内の相対パスは許可するが、その途中に'..'が
    含まれる場合は引き続き拒否されることを確認する(依頼の項目5:
    パストラバーサル対策自体は緩めない)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, "templates/../../outside.py")
        assert path is None
        assert error is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resolve_safe_project_path_rejects_progress_md_inside_subdirectory():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        path, error = yoriai._resolve_safe_project_path(out_dir, f"sub/{yoriai.PROGRESS_FILENAME}")
        assert path is None
        assert error is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_write_project_file_creates_missing_subdirectory_automatically():
    """バグ再現の核心: make_directoryを呼ばなくても、write_fileだけで
    サブディレクトリごと自動的に作成され、実際にファイルが書き込まれる
    ことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        result = json.loads(yoriai._write_project_file(out_dir, "templates/base.html", "<html></html>\n"))
        assert result["ok"] is True, result
        assert result["filename"] == "templates/base.html", result
        with open(os.path.join(out_dir, "templates", "base.html"), encoding="utf-8") as f:
            assert f.read() == "<html></html>\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_write_project_file_populates_directory_created_via_make_directory():
    """バグの元シナリオそのもの: 先にmake_directoryでディレクトリを作り、
    その後write_fileでその中にファイルを書き込む、という順序でも
    正しく成功することを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        mkdir_result = json.loads(yoriai._make_project_directory(out_dir, "lessons"))
        assert mkdir_result["ok"] is True, mkdir_result
        write_result = json.loads(yoriai._write_project_file(out_dir, "lessons/lesson1.html", "<p>lesson1</p>\n"))
        assert write_result["ok"] is True, write_result
        with open(os.path.join(out_dir, "lessons", "lesson1.html"), encoding="utf-8") as f:
            assert f.read() == "<p>lesson1</p>\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_move_project_file_into_subdirectory_creates_it_automatically():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "base.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>\n")
        result = json.loads(yoriai._move_project_file(out_dir, "base.html", "templates/base.html"))
        assert result["ok"] is True, result
        assert result["new_filename"] == "templates/base.html", result
        assert not os.path.exists(os.path.join(out_dir, "base.html"))
        assert os.path.isfile(os.path.join(out_dir, "templates", "base.html"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_delete_project_file_from_subdirectory_records_relative_path_in_changelog():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        tasks = [("a.py", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        os.makedirs(os.path.join(out_dir, "lessons"))
        with open(os.path.join(out_dir, "lessons", "old.html"), "w", encoding="utf-8") as f:
            f.write("<p>old</p>\n")

        result = json.loads(yoriai._delete_project_file(out_dir, "lessons/old.html"))
        assert result["ok"] is True, result
        assert not os.path.exists(os.path.join(out_dir, "lessons", "old.html"))

        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
        assert "lessons/old.html" in parsed["changelog"][0], parsed["changelog"]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_list_project_files_includes_subdirectory_contents():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        os.makedirs(os.path.join(out_dir, "templates"))
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("x")
        with open(os.path.join(out_dir, "templates", "base.html"), "w", encoding="utf-8") as f:
            f.write("x")
        with open(os.path.join(out_dir, yoriai.PROGRESS_FILENAME), "w", encoding="utf-8") as f:
            f.write("x")
        assert yoriai._list_project_files(out_dir) == ["index.html", "templates/base.html"]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_syntax_check_all_files_checks_subdirectory_files_too():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        os.makedirs(os.path.join(out_dir, "sub"))
        with open(os.path.join(out_dir, "sub", "broken.py"), "w", encoding="utf-8") as f:
            f.write("def f(:\n    pass\n")
        broken = yoriai._syntax_check_all_files(out_dir)
        assert broken == ["sub/broken.py"], broken
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_reads_files_inside_subdirectories():
    """バグの裏返し: サブディレクトリに書き込んだファイルを、
    read_fileで正しく読み返せることを確認する(書き込み側だけを
    緩和して読み取り側が対応していないと、モデルは自分が書いた内容を
    確認できなくなってしまう)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        os.makedirs(os.path.join(out_dir, "templates"))
        with open(os.path.join(out_dir, "templates", "base.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "templates/base.html"))
        assert result == {"filename": "templates/base.html", "exists": True, "content": "<html></html>\n"}, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_search_in_project_file_searches_inside_subdirectories():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        os.makedirs(os.path.join(out_dir, "templates"))
        with open(os.path.join(out_dir, "templates", "base.html"), "w", encoding="utf-8") as f:
            f.write("<html>\n<title>target</title>\n</html>\n")
        result = json.loads(yoriai._search_in_project_file(out_dir, "templates/base.html", "target"))
        assert result["exists"] is True and result["match_count"] == 1, result
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


def test_execute_project_tool_call_writes_into_subdirectory_via_write_file():
    """バグ再現の全体的な結線確認: モデルがmake_directory→write_fileの
    順でツール呼び出しを行うシナリオを、_execute_project_tool_call経由で
    再現しても実際にファイルが保存されることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        mkdir_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "make_directory", "arguments": {"dirname": "templates"}},
        }
        mkdir_result = json.loads(yoriai._execute_project_tool_call(out_dir, mkdir_call))
        assert mkdir_result["ok"] is True, mkdir_result

        write_call = {
            "id": "call_2", "type": "function",
            "function": {
                "name": "write_file",
                "arguments": {"filename": "templates/base.html", "content": "<html></html>\n"},
            },
        }
        write_result = json.loads(yoriai._execute_project_tool_call(out_dir, write_call))
        assert write_result["ok"] is True, write_result
        with open(os.path.join(out_dir, "templates", "base.html"), encoding="utf-8") as f:
            assert f.read() == "<html></html>\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_execute_project_tool_call_dispatches_to_read_file_with_range():
    """依頼の項目2: //fixの修正担当がread_fileにstart_line/end_lineを
    指定した場合、_execute_project_tool_call経由でもその範囲だけが
    返されることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "a.py"), "w", encoding="utf-8") as f:
            f.write("\n".join(f"line{i}" for i in range(1, 11)) + "\n")
        tool_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "read_file", "arguments": {"filename": "a.py", "start_line": 3, "end_line": 5}},
        }
        result = json.loads(yoriai._execute_project_tool_call(out_dir, tool_call))
        assert result["exists"] is True, result
        assert result["content"] == "3: line3\n4: line4\n5: line5", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_execute_project_tool_call_dispatches_to_search_in_file():
    """依頼の項目1: //fixの修正担当がsearch_in_fileを呼んだ場合も、
    _execute_project_tool_call経由で一致行が返されることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        with open(os.path.join(out_dir, "a.py"), "w", encoding="utf-8") as f:
            f.write("def foo():\n    pass\n\ndef target_func():\n    pass\n")
        tool_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "search_in_file", "arguments": {"filename": "a.py", "query": "target_func"}},
        }
        result = json.loads(yoriai._execute_project_tool_call(out_dir, tool_call))
        assert result["exists"] is True and result["match_count"] == 1, result
        assert result["matched_lines"] == [4], result
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


def test_identify_target_project_ignores_incomplete_projects_as_a_fix_target():
    """未完了のプロジェクトは、たとえ依頼文と強く一致していても「//fixで
    直接修正してよい候補」には含めない(未完了プロジェクトは//resume-all
    の役割であり、修正依頼の対象は完成済みプロジェクトに限定するため)。

    仮の判断(バグ報告への対応): 以前はこの場合を一律"not_found"として
    扱っていたが、依頼文に最も近いのがまさにその未完了プロジェクトである
    場合、「見つかりませんでした。新規に//agreeで作ってください」という
    誤った案内になり、ユーザーが誤って重複したプロジェクトを作ってしまい
    かねなかった。この関数自体は"not_found"ではなく、区別可能な
    "pending_fix"を返す(そのプロジェクトが存在し、かつ十分一致することは
    正しく伝えつつ、修正対象としては選ばない)。呼び出し元
    (`_resolve_and_validate_fix_target`)はこれを見て
    `//resume-all`へ誘導する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_incomplete_project(
            projects_root, "todo-cli", [("utils.py", "IDの生成関数generate_idを実装する")],
        )

        status, dirs = yoriai._identify_target_project("ID生成のロジックにバグがあるので直して", out_dir)
        assert status == "pending_fix", (status, dirs)
        assert dirs == [project_dir], dirs
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


def test_classify_execution_mode_returns_fix_project_for_continuation_phrases():
    """「では作業してください」「充実させて」のような、修正キーワードには
    一致しない既存プロジェクトへの作業継続依頼も、完成済みプロジェクトが
    存在すればFIX_PROJECTと判定される(単発モードに落ちてread_file/
    write_fileが使えなくなる不具合の再発防止)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("a.py", "説明")])
        for text in ("では作業してください", "充実させて", "続けてください", "進めてください"):
            mode = yoriai._classify_execution_mode(text, out_dir)
            assert mode == yoriai.EXECUTION_MODE_FIX_PROJECT, (text, mode)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_classify_execution_mode_does_not_return_fix_project_for_continuation_phrases_without_project():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        mode = yoriai._classify_execution_mode("では作業してください", out_dir)
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
    run_command→最終報告、の順にツール呼び出しラウンドを進める。
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
            {"id": "call_4", "type": "function", "function": {"name": "run_command", "arguments": {"command": "pytest"}}},
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


def _fake_stream_fix_make_directory_then_write_into_it(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    """実機のバグ報告を再現するフェイク: make_directoryでtemplatesを
    作成した後、その中にwrite_fileでファイルを書き込もうとする(モデルが
    サブディレクトリに整理しようとする自然な振る舞いを模擬する)。
    """
    n = _tool_round(messages)
    if n == 0:
        yield {"pending_tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "make_directory", "arguments": {"dirname": "templates"}}},
        ]}
        return
    if n == 1:
        yield {"pending_tool_calls": [
            {"id": "call_2", "type": "function", "function": {
                "name": "write_file",
                "arguments": {"filename": "templates/base.html", "content": "<html><body>base</body></html>\n"},
            }},
        ]}
        return
    yield {"content": "templates/base.htmlを作成しました。"}
    yield {"done": True}


def test_fix_project_end_to_end_populates_subdirectory_created_via_make_directory():
    """バグ報告の再現と修正の確認: 「テンプレートファイルを作成」の
    ような依頼で、モデルがmake_directory→write_fileの順でサブ
    ディレクトリ内にファイルを作ろうとした場合、以前は書き込みが
    常に拒否されディレクトリだけが空のまま残っていたが、修正後は
    実際にファイルが保存されることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_make_directory_then_write_into_it

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(projects_root, "html-course", [("index.html", "トップページ")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "html-course: レッスンページのレイアウトを統一するためのテンプレートファイルを作成", out_dir,
            )
        output = buf.getvalue()

        assert "[✅ 修正が完了しました" in output, output
        assert os.path.isdir(os.path.join(project_dir, "templates")), "テンプレートディレクトリ自体は作成されるはずです"
        template_path = os.path.join(project_dir, "templates", "base.html")
        assert os.path.isfile(template_path), (
            f"サブディレクトリの中身が空のままになる不具合が再発しています: {output}"
        )
        with open(template_path, encoding="utf-8") as f:
            assert f.read() == "<html><body>base</body></html>\n"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert "templates/base.html" in parsed["changelog"][0], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_end_to_end_uses_tools_to_rename_edit_and_test():
    """依頼の動作確認: 完成済みプロジェクトに「ファイル名をutils.pyから
    helpers.pyに変更して、それに合わせてimportも直して、テストがあれば
    実行して確認して」のような依頼を送ると、正しいプロジェクトが特定され、
    複数のツール(read_file・move_file・write_file・run_command)を使って
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


def test_fix_project_auto_matched_target_with_pending_fix_subtasks_guides_to_resume_all():
    """実機のバグ報告の再現と修正の確認: プロジェクト名をコロン付きで
    明示せずに(自動判定に委ねる形で)//fixを送った場合、依頼文に最も
    近いプロジェクトに//fixのタスク分割由来の未完了サブタスクが残って
    いても、「見つかりませんでした。新規に//agreeで作ってください」という
    誤った案内(以前の不具合)ではなく、「未完了のタスクが残っています。
    先に//resume-allで完了させてから」という正しい案内になることを
    確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-css-html-css",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2")],
        )
        # //fixのタスク分割が一部未完了のまま終わった状態を直接再現する
        # (_run_fix_task_queueが実際に書き込むのと同じ形でPROGRESS.mdに
        # 記録する)。
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        yoriai._write_progress_md(
            project_dir, parsed["request"], parsed["tasks"], parsed["checklist"], {},
            pending_fix_request="HTMLエディタの改修をお願いします。初心者向けレッスン機能を追加してください。",
            pending_fix_subtasks=["レッスンページのレイアウトを統一するためのテンプレートファイルを作成"],
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # プロジェクト名の後にコロンを付けない、自動判定に頼る依頼文
            # (実機の再現: "//fix html-css-html-css <コロン無しの依頼文>")。
            yoriai._ask_organization_fix_project(
                47120, "fingerprint",
                "html-css-html-css HTMLエディタの改修をお願いします。初心者向けレッスン機能を追加してください。",
                out_dir,
            )
        output = buf.getvalue()

        assert "見つかりませんでした" not in output, (
            "未完了のプロジェクトが実在するのに「見つからない」と誤案内しています: " + output
        )
        assert project_dir in output, output
        assert "未完了のタスクが残っています" in output, output
        assert yoriai.RESUME_ALL_COMMAND in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_end_to_end_rejects_path_traversal_tool_calls():
    """モデルの応答(悪意ある、または壊れた応答)がプロジェクト外への
    パスを指定しても、ツール実行の安全対策(_resolve_safe_project_path)
    により実際には何も書き込まれないことを確認する(依頼の項目2・5)。

    重大なバグ報告への対応の検証も兼ねる(依頼の項目3): write_file自体は
    呼ばれたが、パストラバーサル対策により実際には失敗した場合、それが
    "修正完了"として誤報告されず、PROGRESS.mdにも記録されないことを
    確認する。
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
        project_dir = _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: 何かを直して", out_dir)
        output = buf.getvalue()

        assert not os.path.exists(os.path.join(out_dir, "etc", "passwd"))
        assert not os.path.exists(os.path.join(os.path.dirname(out_dir), "etc", "passwd"))

        assert "[✅ 修正が完了しました" not in output, (
            f"実際には何も書き込まれていないのに完了扱いになっています: {output}"
        )
        assert "実行されませんでした" in output, output

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["changelog"] == [], (
            f"何も変更されていないのにPROGRESS.mdへ更新履歴が記録されています: {parsed['changelog']}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_reports_honestly_when_model_never_calls_a_tool():
    """重大なバグ報告への対応(依頼の項目2): モデルが「修正しました」と
    いう文章だけを返し、write_file等のツールを一度も呼ばなかった場合に、
    "修正完了"ではなく正直に「実行されませんでした」と報告し、
    PROGRESS.mdにも記録しないことを確認する。ナッジ(1回だけ実際に
    ツールを呼ぶよう促す再試行)をしても改善しない場合の最終挙動であり、
    問い合わせ回数がナッジ1回分(合計2回)で頭打ちになり無限に促し続け
    ないことも合わせて確認する。

    追加のバグ報告(曖昧な依頼でファイルが特定できない場合)への対応:
    単に失敗を報告するだけでなく、「どのファイルを直せば良いか判断
    できませんでした」という確認を求める文面で、ユーザーに次の一手
    (ファイル名や直したい箇所を教える)を案内することも確認する。
    """
    call_count = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        # 依頼の規模判定ステップ(_decide_fix_task_split、offer_project_tools無しの
        # 素の会話)もこのフェイクを1回通るため、ナッジの回数は
        # offer_project_tools=Trueの呼び出しだけを数えて確認する。
        if offer_project_tools:
            call_count["n"] += 1
        yield {"content": "ID生成のロジックを確認しましたが、特に問題は見当たりませんでした。修正しました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli", [("utils.py", "説明")], files={"utils.py": "def generate_id():\n    return 1\n"},
        )
        original_content = open(os.path.join(project_dir, "utils.py"), encoding="utf-8").read()

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: バグを直して", out_dir)
        output = buf.getvalue()

        assert "[✅ 修正が完了しました" not in output, output
        assert "実行されませんでした" in output, output
        assert "どのファイルを直せば良いか判断できませんでした" in output, output
        assert call_count["n"] == 2, f"ナッジは1回だけのはずです(問い合わせ回数: {call_count['n']})"
        with open(os.path.join(project_dir, "utils.py"), encoding="utf-8") as f:
            assert f.read() == original_content, "ファイルの中身が変わってしまっています"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["changelog"] == [], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_recovers_when_model_actually_calls_tool_after_nudge():
    """ユーザーからの追加報告への対応: 実機(qwen3-235b)で、モデルが
    「editor.cssを修正しました」「write_fileツールを使用して上書き
    しました」と、あたかも実行したかのような説明文だけを返し、実際には
    write_fileを一度も呼んでいなかった事例が報告された。この場合、単に
    正直に「実行されませんでした」と報告するだけでなく、1回だけ実際に
    ツールを呼ぶよう促すことで、修正そのものが成功するようになることを
    確認する。
    """
    def _was_nudged(messages):
        return any(
            m.get("role") == "user" and "実際には呼び出されていません" in m.get("content", "")
            for m in messages
        )

    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        # 実機の再現: 1回目はwrite_fileを呼ばず、あたかも実行したかのような説明文だけを返す。
        if not _was_nudged(messages):
            yield {"content": (
                "HTMLとCSSのエディタの入力欄を広くするために、editor.cssファイルを修正しました。"
                "`textarea`セレクタにwidth: 90%;を追加しました。"
            )}
            yield {"done": True}
            return
        # ナッジを受けて、まだ実際にツールを呼んでいなければ、今度こそwrite_fileを呼ぶ。
        if _tool_round(messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": {"filename": "editor.css", "content": "textarea { width: 90%; height: 200px; }\n"},
                }},
            ]}
            return
        # write_file実行後、最終的な報告を返す。
        yield {"content": "editor.cssを実際に修正しました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-css-html-css", [("editor.css", "説明")],
            files={"editor.css": "textarea { width: 50%; }\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "html-css-html-css: HTMLとCSSのテキストエリアが狭いので広くして", out_dir,
            )
        output = buf.getvalue()

        assert "促して再試行しています" in output, output
        assert "[✅ 修正が完了しました" in output, output
        assert "実行されませんでした" not in output, output
        with open(os.path.join(project_dir, "editor.css"), encoding="utf-8") as f:
            content = f.read()
        assert "width: 90%" in content, f"ナッジ後の実際の書き込みが反映されていません: {content!r}"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        assert "editor.css" in parsed["changelog"][0], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_records_actually_modified_filenames_in_changelog():
    """依頼の動作確認: PROGRESS.mdの更新履歴が、実際に変更されたファイル名を
    含み、実態と一致していることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_simple_write

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli", [("utils.py", "説明")], files={"utils.py": "def generate_id():\n    return 1\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: バグを直して", out_dir)
        output = buf.getvalue()

        assert "utils.py" in output and "変更したファイル" in output, output
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        assert "utils.py" in parsed["changelog"][0], parsed["changelog"]
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_records_partial_progress_honestly_when_round_cap_is_hit():
    """依頼の動作確認: 「暴走・ツール呼び出し上限到達」に近い状況を作り、
    その場合に誤って"完了"と報告されないことを確認する。一部のファイルは
    実際に変更された状態でMAX_PROJECT_TOOL_ROUNDSに達した場合、"完了"では
    なく正直に「セッションが最後まで正常に完了しなかった」旨を報告し、
    かつ実際に変更されたファイルはPROGRESS.mdに記録される
    (変更そのものは無かったことにしない)ことを確認する。
    """
    def fake_stream_writes_then_loops_forever(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        if _tool_round(messages) == 0:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": {"filename": "utils.py", "content": "def generate_id():\n    return 'fixed-id'\n"},
                }},
            ]}
            return
        # 以降は無限にlist_dirを呼び続け、最終回答を一切返さない状況を模す。
        yield {"pending_tool_calls": [
            {"id": "call_loop", "type": "function", "function": {"name": "list_dir", "arguments": {}}},
        ]}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream_writes_then_loops_forever

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "todo-cli", [("utils.py", "説明")], files={"utils.py": "def generate_id():\n    return 1\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "todo-cli: バグを直して", out_dir)
        output = buf.getvalue()

        assert "[✅ 修正が完了しました" not in output, (
            f"往復回数の上限到達で異常終了しているのに完了扱いになっています: {output}"
        )
        assert "正常に完了しませんでした" in output, output
        assert "utils.py" in output, output

        with open(os.path.join(project_dir, "utils.py"), encoding="utf-8") as f:
            assert "fixed-id" in f.read(), "実際に成功した書き込みは反映されているはずです"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, (
            f"実際に変更が確認できた場合は、異常終了でもPROGRESS.mdに記録されるはずです: {parsed['changelog']}"
        )
        assert "utils.py" in parsed["changelog"][0], parsed["changelog"]
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
        content, error, truncated, modified_files = yoriai._collect_answer_with_project_tools(
            candidate, "fingerprint", [{"role": "user", "content": "何か直して"}], out_dir,
        )
        assert content == ""
        assert error is not None and "上限" in error, error
        assert truncated is False
        assert modified_files == [], modified_files
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


# ---------------------------------------------------------------------------
# //fixの規模判定・タスク分割(_decide_fix_task_split・_run_fix_task_queue)
# ---------------------------------------------------------------------------
#
# 大規模な修正依頼(教材コンテンツの創作を含む、複数ファイルにまたがる
# 改修)を//fixに送ると、1台のメンバーが丸ごと処理しようとして応答時間の
# 増大・タイムアウトにつながる不具合への対応。//agreeの合意フェーズ・
# タスクキュー方式と同じ発想を//fixにも導入し、規模が大きい場合のみ
# 複数のサブタスクに分割して複数メンバーで分担する。

def test_parse_fix_split_subtasks_extracts_bullet_lines():
    text = "- lesson1.htmlにレッスン1を追加する\n- lesson2.htmlにレッスン2を追加する\n- progress.jsに保存機能を追加する"
    subtasks = yoriai._parse_fix_split_subtasks(text)
    assert subtasks == [
        "lesson1.htmlにレッスン1を追加する", "lesson2.htmlにレッスン2を追加する", "progress.jsに保存機能を追加する",
    ], subtasks


def test_parse_fix_split_subtasks_returns_empty_for_no_split_answer():
    """「分割不要」という単純な回答、または箇条書きが1件しか無い回答は、
    分割不要の安全側フォールバックとして空リストを返すことを確認する。
    """
    assert yoriai._parse_fix_split_subtasks("分割不要") == []
    assert yoriai._parse_fix_split_subtasks("- 1箇所だけ直せば十分です") == []
    assert yoriai._parse_fix_split_subtasks("") == []


def test_decide_fix_task_split_returns_subtasks_when_model_recommends_split():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"content": "- レッスン1を追加する\n- レッスン2を追加する\n- 進捗保存機能を追加する"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            subtasks = yoriai._decide_fix_task_split(
                _member("MacStudio", "qwen2.5-coder-32b"), "fingerprint",
                "レッスンを2つ追加して進捗保存機能もつけて", "index.html: トップページ", ["index.html"], "HTML/CSS/JavaScript",
            )
        output = buf.getvalue()
    finally:
        yoriai._stream_chat_from_candidate = original_stream

    assert subtasks == ["レッスン1を追加する", "レッスン2を追加する", "進捗保存機能を追加する"], subtasks
    assert "[🔍 修正の規模を判定しています...]" in output, output


def test_decide_fix_task_split_falls_back_to_empty_on_error():
    """判定担当への問い合わせ自体が失敗した場合も、安全側(分割しない)に
    フォールバックすることを確認する。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"error": "接続に失敗しました"}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        subtasks = yoriai._decide_fix_task_split(
            _member("MacStudio", "qwen2.5-coder-32b"), "fingerprint", "直して", "a.py: 説明", ["a.py"], "Python",
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
    assert subtasks == []


# 分割ありのend-to-endテスト用フェイク。判定ステップ・実装ステップ・
# レビューステップを、プロンプト本文に含まれる目印(テンプレート内の
# 固定文言、サブタスクの説明に含めたファイル名)で区別する。
_SPLIT_SUBTASKS = [
    "lesson1.html に新しいレッスン1のコンテンツを追加する",
    "lesson2.html に新しいレッスン2のコンテンツを追加する",
    "progress.js に進捗保存機能を追加する",
]
_SPLIT_TARGET_FILES = ("lesson1.html", "lesson2.html", "progress.js")


def _fake_stream_fix_split(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    prompt = messages[0]["content"] if messages else ""
    tool_round = _tool_round(messages)

    if "1人のメンバーが一度に実行できる規模か" in prompt:
        yield {"content": "\n".join(f"- {s}" for s in _SPLIT_SUBTASKS)}
        yield {"done": True}
        return

    if "改修レビュー担当です" in prompt:
        # レビュー担当は常に「問題なし」とだけ答える単純なフェイク
        # (レビューが実装を上書きしないことの確認が目的のテストで使う)。
        yield {"content": "問題なし"}
        yield {"done": True}
        return

    # 仮の判断: full_plan・file_listにはプロジェクト内の全ファイル名が
    # 常に含まれるため、単純なファイル名の部分一致では、どのサブタスクの
    # プロンプトでも先頭のファイル名にマッチしてしまう(全サブタスクが
    # 同じファイルを書き込んでしまう不具合の原因になった)。「【ユーザー
    # からの修正依頼】」に埋め込まれるのはサブタスクの説明文そのもの
    # (ファイル名だけでなく「に新しい...を追加する」まで含む全文)なので、
    # サブタスク文全体との一致で判定することで、そのプロンプトが実際に
    # どのサブタスクの実装依頼なのかを一意に特定する。
    for filename, subtask_text in zip(_SPLIT_TARGET_FILES, _SPLIT_SUBTASKS):
        if subtask_text in prompt:
            if tool_round == 0:
                yield {"pending_tool_calls": [
                    {"id": "call_1", "type": "function", "function": {
                        "name": "write_file",
                        "arguments": {"filename": filename, "content": f"<!-- {filename} updated -->\n"},
                    }},
                ]}
                return
            yield {"content": f"{filename}を更新しました。"}
            yield {"done": True}
            return

    # マッチしなかった場合(想定外)は何もしない安全側の応答。
    yield {"content": "問題なし"}
    yield {"done": True}


def test_fix_project_splits_large_request_into_subtasks_and_completes_all():
    """依頼の動作確認: 大規模な修正依頼では、複数のサブタスクに分割され、
    タスクキュー方式で複数メンバーが分担して処理されることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_split

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-course",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2"), ("progress.js", "進捗保存")],
            files={fn: f"<!-- {fn} placeholder -->\n" for fn in _SPLIT_TARGET_FILES},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "レッスンを2つ追加して進捗保存機能もつけて", out_dir,
            )
        output = buf.getvalue()

        assert "[🔍 修正の規模を判定しています...]" in output, output
        assert "[📐 大規模な修正のため、3件のサブタスクに分割します]" in output, output
        assert "[✅ 修正が完了しました" in output, output

        for filename in _SPLIT_TARGET_FILES:
            with open(os.path.join(project_dir, filename), encoding="utf-8") as f:
                assert f.read() == f"<!-- {filename} updated -->\n", filename

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert len(parsed["changelog"]) == 1, parsed["changelog"]
        for filename in _SPLIT_TARGET_FILES:
            assert filename in parsed["changelog"][0], parsed["changelog"]
        # 修正はサブタスク単位の一時的なチェックリストであり、元のプロジェクトの
        # 実装計画チェックリスト(完了済み)には影響しないはずである。
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is False
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_split_shows_subtask_checklist_progress():
    """依頼の項目5: 画面表示で、規模判定・分割件数・サブタスクの
    チェックリスト進捗が分かることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_split

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(
            projects_root, "html-course",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2"), ("progress.js", "進捗保存")],
            files={fn: f"<!-- {fn} placeholder -->\n" for fn in _SPLIT_TARGET_FILES},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "レッスンを2つ追加して進捗保存機能もつけて", out_dir,
            )
        output = buf.getvalue()

        for i in range(1, 4):
            assert f"サブタスク{i} の実装" in output, output
            assert f"サブタスク{i} のレビュー" in output, output
        assert "[📋 タスクキュー:" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_split_works_with_single_member_and_skips_review():
    """メンバーが1台のみの場合、実装は行われるがレビュー担当を選べないため
    レビューはスキップされ、それでも実装した変更自体は反映されることを
    確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _one_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_split

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-course",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2"), ("progress.js", "進捗保存")],
            files={fn: f"<!-- {fn} placeholder -->\n" for fn in _SPLIT_TARGET_FILES},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "レッスンを2つ追加して進捗保存機能もつけて", out_dir,
            )
        output = buf.getvalue()

        assert "レビュー担当者がいません" in output, output
        assert "[✅ 修正が完了しました" in output, output
        for filename in _SPLIT_TARGET_FILES:
            with open(os.path.join(project_dir, filename), encoding="utf-8") as f:
                assert f.read() == f"<!-- {filename} updated -->\n", filename
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_project_small_request_stays_on_single_implementer_path():
    """依頼の動作確認: 小さな修正依頼では、規模判定は行われるが「分割不要」
    と判断され、これまで通り1人のメンバーへの直接依頼のまま処理される
    ことを確認する(既存の単一実装end-to-endテストへの追加確認)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_simple_write

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(
            projects_root, "todo-cli-1", [("utils.py", "IDの生成関数generate_idを実装する")],
            files={"utils.py": "def generate_id():\n    return 1\n"},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "todo-cli-1: テキストエリアを広くして", out_dir,
            )
        output = buf.getvalue()

        assert "[🔍 修正の規模を判定しています...]" in output, output
        assert "[📐" not in output, "小さな依頼では分割しないはずです"
        assert "[📋 タスクキュー:" not in output, "小さな依頼では分割しないはずです"
        assert "[✅ 修正が完了しました" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# //fixのタスク分割が一部だけ未完了で終わった場合の再開可能性
# ---------------------------------------------------------------------------
#
# 実機のバグ報告: 分割された//fixの一部のサブタスクがツール呼び出しの
# 往復回数の上限に達して未完了のまま終わった場合、//resume-allが
# 「未完了のプロジェクトは見つかりませんでした」と報告してしまい、
# 再開する手段が無かった。原因は、分割された修正のサブタスクの進捗が
# 一時的なチェックリスト(画面表示専用)としてしか存在せず、PROGRESS.md
# 側の「未完了プロジェクト判定」(モジュール分割案のチェックリストのみを
# 見る)に一切反映されていなかったこと。

def test_parse_bullet_lines_keeps_single_item_unlike_split_subtasks():
    """`_parse_fix_split_subtasks`は2件未満を安全側で「分割不要」に
    切り捨てるが、PROGRESS.mdへ書き戻した未完了サブタスクを読み戻す用途
    (`_parse_bullet_lines`)では、1件だけ残っているケースを消してはならない
    ことを確認する。
    """
    text = "- lesson2.htmlに新しいレッスン2のコンテンツを追加する"
    assert yoriai._parse_bullet_lines(text) == ["lesson2.htmlに新しいレッスン2のコンテンツを追加する"]
    assert yoriai._parse_fix_split_subtasks(text) == []


def test_progress_markdown_round_trips_pending_fix_subtasks():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        yoriai._write_progress_md(
            out_dir, "何か作って", tasks, checklist, {},
            pending_fix_request="バグを直して", pending_fix_subtasks=["lesson1.htmlを直す", "lesson2.htmlを直す"],
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    assert parsed["pending_fix_request"] == "バグを直して"
    assert parsed["pending_fix_subtasks"] == ["lesson1.htmlを直す", "lesson2.htmlを直す"]


def test_progress_markdown_defaults_pending_fix_fields_to_empty():
    """未完了の修正サブタスクが無い(または旧バージョンのPROGRESS.md)場合、
    後方互換のため空文字列・空リストになることを確認する。
    """
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    assert parsed["pending_fix_request"] == ""
    assert parsed["pending_fix_subtasks"] == []


def test_project_has_pending_work_detects_pending_fix_subtasks_even_when_module_checklist_is_complete():
    """バグ再現の核心: モジュール分割案のチェックリストは全項目完了済み
    でも、未完了の修正サブタスクが記録されていれば「再開すべき作業が
    残っている」と判定されることを確認する。
    """
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    yoriai._set_task_status(checklist, "a.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "a.py", "review", yoriai._TASK_STATUS_COMPLETED)
    parsed_without_pending = {"checklist": checklist, "pending_fix_subtasks": []}
    parsed_with_pending = {"checklist": checklist, "pending_fix_subtasks": ["lesson2.htmlを直す"]}
    assert yoriai._project_has_pending_work(parsed_without_pending) is False
    assert yoriai._project_has_pending_work(parsed_with_pending) is True


_SPLIT_FAILING_SUBTASK_INDEX = 1  # _SPLIT_SUBTASKS[1] == lesson2.html担当


def _fake_stream_fix_split_one_subtask_never_finishes(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
    """`_fake_stream_fix_split`と同じシナリオだが、lesson2.html担当の
    サブタスクだけは、実機の往復回数上限到達を再現するため、
    list_dirの呼び出しを無限に繰り返して終わらない(_collect_answer_
    with_project_toolsがMAX_PROJECT_TOOL_ROUNDSで打ち切るまで続く)。
    """
    prompt = messages[0]["content"] if messages else ""
    tool_round = _tool_round(messages)

    if "1人のメンバーが一度に実行できる規模か" in prompt:
        yield {"content": "\n".join(f"- {s}" for s in _SPLIT_SUBTASKS)}
        yield {"done": True}
        return

    if "改修レビュー担当です" in prompt:
        yield {"content": "問題なし"}
        yield {"done": True}
        return

    if _SPLIT_SUBTASKS[_SPLIT_FAILING_SUBTASK_INDEX] in prompt:
        yield {"pending_tool_calls": [
            {"id": f"call_{tool_round}", "type": "function", "function": {"name": "list_dir", "arguments": {}}},
        ]}
        return

    for filename, subtask_text in zip(_SPLIT_TARGET_FILES, _SPLIT_SUBTASKS):
        if subtask_text in prompt:
            if tool_round == 0:
                yield {"pending_tool_calls": [
                    {"id": "call_1", "type": "function", "function": {
                        "name": "write_file",
                        "arguments": {"filename": filename, "content": f"<!-- {filename} updated -->\n"},
                    }},
                ]}
                return
            yield {"content": f"{filename}を更新しました。"}
            yield {"done": True}
            return

    yield {"content": "問題なし"}
    yield {"done": True}


def test_fix_project_split_persists_pending_subtasks_so_resume_all_finds_it():
    """バグ報告の再現と修正の確認: 分割された//fixが一部のサブタスクを
    (往復回数の上限到達により)未完了のまま終えた場合、//resume-allが
    それを正しく「未完了のプロジェクト」として検出できることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_fix_split_one_subtask_never_finishes

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-course",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2"), ("progress.js", "進捗保存")],
            files={fn: f"<!-- {fn} placeholder -->\n" for fn in _SPLIT_TARGET_FILES},
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "レッスンを2つ追加して進捗保存機能もつけて", out_dir,
            )
        output = buf.getvalue()

        assert "[✅ 修正が完了しました" not in output, (
            "一部のサブタスクが未完了のまま終わったはずです: " + output
        )
        assert "[⚠️ 修正セッションが最後まで正常に完了しませんでした" in output, output
        assert f"[🔁 1件のサブタスクが未完了のため、{yoriai.RESUME_ALL_COMMAND}で再開できます]" in output, output

        # バグの核心: モジュール分割案のチェックリストは完了済みのままだが、
        # 未完了の修正サブタスクは検出できなければならない。
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is False, (
            "モジュールタスク自体は完了済みのままのはずです"
        )
        assert parsed["pending_fix_subtasks"] == [_SPLIT_SUBTASKS[_SPLIT_FAILING_SUBTASK_INDEX]], parsed
        assert parsed["pending_fix_request"] == "レッスンを2つ追加して進捗保存機能もつけて"

        incomplete = yoriai._find_incomplete_projects(out_dir)
        assert incomplete == [project_dir], (
            f"//resume-allは未完了のプロジェクトとしてこれを検出できるはずです: {incomplete}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_resume_all_completes_previously_pending_fix_subtask_and_clears_it():
    """//resume-allが実際に未完了の修正サブタスクを再開し、成功すれば
    PROGRESS.mdの未完了記録がクリアされる(=再度//resume-allしても
    「未完了のプロジェクトなし」に戻る)ことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()

    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-course",
            [("lesson1.html", "レッスン1"), ("lesson2.html", "レッスン2"), ("progress.js", "進捗保存")],
            files={fn: f"<!-- {fn} placeholder -->\n" for fn in _SPLIT_TARGET_FILES},
        )

        # 1回目: lesson2.html担当のサブタスクが未完了のまま終わる。
        yoriai._stream_chat_from_candidate = _fake_stream_fix_split_one_subtask_never_finishes
        with contextlib.redirect_stdout(io.StringIO()):
            yoriai._ask_organization_fix_project(
                47120, "fingerprint", "レッスンを2つ追加して進捗保存機能もつけて", out_dir,
            )
        assert yoriai._find_incomplete_projects(out_dir) == [project_dir]

        # 2回目: //resume-allでは、今度はlesson2.html担当も正常に完了する
        # フェイクに差し替える(実機で言えば、次のポーリングまでに応答が
        # 返るようになった状況を模擬)。
        yoriai._stream_chat_from_candidate = _fake_stream_fix_split
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_resume_all(47120, "fingerprint", out_dir)
        output = buf.getvalue()

        assert "未完了のプロジェクトは見つかりませんでした" not in output, output
        assert "[✅ 全プロジェクトの未完了タスクが完了しました]" in output, output

        with open(os.path.join(project_dir, "lesson2.html"), encoding="utf-8") as f:
            assert f.read() == "<!-- lesson2.html updated -->\n"

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["pending_fix_subtasks"] == [], parsed
        assert yoriai._find_incomplete_projects(out_dir) == []
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
        test_syntax_check_all_files_reports_broken_files_only,
        test_resolve_safe_project_path_accepts_subdirectory_relative_path,
        test_resolve_safe_project_path_still_rejects_parent_directory_reference_inside_subpath,
        test_resolve_safe_project_path_rejects_progress_md_inside_subdirectory,
        test_write_project_file_creates_missing_subdirectory_automatically,
        test_write_project_file_populates_directory_created_via_make_directory,
        test_move_project_file_into_subdirectory_creates_it_automatically,
        test_delete_project_file_from_subdirectory_records_relative_path_in_changelog,
        test_list_project_files_includes_subdirectory_contents,
        test_syntax_check_all_files_checks_subdirectory_files_too,
        test_read_project_file_fresh_reads_files_inside_subdirectories,
        test_search_in_project_file_searches_inside_subdirectories,
        test_execute_project_tool_call_dispatches_to_write_file,
        test_execute_project_tool_call_writes_into_subdirectory_via_write_file,
        test_execute_project_tool_call_dispatches_to_read_file_with_range,
        test_execute_project_tool_call_dispatches_to_search_in_file,
        test_fix_project_end_to_end_populates_subdirectory_created_via_make_directory,
        test_identify_target_project_matches_unique_high_scoring_project,
        test_identify_target_project_reports_ambiguous_on_tie,
        test_identify_target_project_reports_not_found_when_no_overlap,
        test_identify_target_project_ignores_incomplete_projects_as_a_fix_target,
        test_identify_target_project_not_found_when_no_projects_dir,
        test_resolve_explicit_fix_target_matches_existing_project_name,
        test_resolve_explicit_fix_target_falls_through_for_unknown_prefix,
        test_classify_execution_mode_without_out_dir_never_returns_fix_project,
        test_classify_execution_mode_returns_fix_project_when_project_exists,
        test_classify_execution_mode_does_not_return_fix_project_without_any_completed_project,
        test_classify_execution_mode_returns_fix_project_for_continuation_phrases,
        test_classify_execution_mode_does_not_return_fix_project_for_continuation_phrases_without_project,
        test_fix_project_end_to_end_uses_tools_to_rename_edit_and_test,
        test_fix_project_explicit_syntax_bypasses_auto_identification,
        test_fix_project_reports_ambiguous_candidates_without_modifying_anything,
        test_fix_project_reports_not_found_without_modifying_anything,
        test_fix_project_refuses_when_target_project_is_incomplete,
        test_fix_project_auto_matched_target_with_pending_fix_subtasks_guides_to_resume_all,
        test_fix_project_end_to_end_rejects_path_traversal_tool_calls,
        test_fix_project_reports_honestly_when_model_never_calls_a_tool,
        test_fix_project_recovers_when_model_actually_calls_tool_after_nudge,
        test_fix_project_records_actually_modified_filenames_in_changelog,
        test_fix_project_records_partial_progress_honestly_when_round_cap_is_hit,
        test_fix_project_reports_syntax_errors_remaining_after_fix,
        test_fix_project_works_with_single_member_present,
        test_fix_project_end_to_end_delete_records_two_changelog_entries,
        test_collect_answer_with_project_tools_stops_at_round_cap,
        test_resume_project_preserves_changelog_from_a_prior_fix,
        test_parse_fix_split_subtasks_extracts_bullet_lines,
        test_parse_fix_split_subtasks_returns_empty_for_no_split_answer,
        test_decide_fix_task_split_returns_subtasks_when_model_recommends_split,
        test_decide_fix_task_split_falls_back_to_empty_on_error,
        test_fix_project_splits_large_request_into_subtasks_and_completes_all,
        test_fix_project_split_shows_subtask_checklist_progress,
        test_fix_project_split_works_with_single_member_and_skips_review,
        test_fix_project_small_request_stays_on_single_implementer_path,
        test_parse_bullet_lines_keeps_single_item_unlike_split_subtasks,
        test_progress_markdown_round_trips_pending_fix_subtasks,
        test_progress_markdown_defaults_pending_fix_fields_to_empty,
        test_project_has_pending_work_detects_pending_fix_subtasks_even_when_module_checklist_is_complete,
        test_fix_project_split_persists_pending_subtasks_so_resume_all_finds_it,
        test_resume_all_completes_previously_pending_fix_subtask_and_clears_it,
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
