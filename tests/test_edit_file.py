#!/usr/bin/env python3
"""edit_fileツール(部分置換)の追加を検証するテスト。

背景: これまでプロジェクトファイルへの書き込み手段はwrite_file(全文
上書き)のみだった。300行のファイルの3行を直すために毎回300行を出力
させるため、CHAT_MAX_OUTPUT_TOKENSに達して途中で切れるとファイルが
壊れたまま書き込まれる、ローカルLLMが全文を通すたびに触る必要のない
箇所まで書き換えてしまう、という2つの実害があった。Claude Codeの
Edit(一意な文字列の置換)と同じ設計方針で、old_stringがファイル内で
ちょうど1箇所に一致する場合のみ置換する安全なツールとして追加した。

ここでは(1)一意にマッチしたときに置換されること、(2)0件・複数件の
ときはファイルが一切変更されず、複数件の場合は行番号がエラーに含まれる
こと、(3)new_string=""で削除になること、(4)パストラバーサル・
PROGRESS.mdの保護が既存ツールと同じく効くこと、(5)置換後に構文エラーに
なる場合はsyntax_ok: falseと詳細が返ること、(6)truncatedなターンの
write_fileが実行されないこと、を確認する。

使い方: python3 tests/test_edit_file.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tools  # noqa: E402
import yoriai  # noqa: E402


def _make_project(files: dict) -> str:
    out_dir = tempfile.mkdtemp(prefix="yoriai_edit_file_test_")
    for filename, content in files.items():
        path = os.path.join(out_dir, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    return out_dir


# ---------------------------------------------------------------------------
# 基本の置換動作
# ---------------------------------------------------------------------------

def test_edit_replaces_unique_match():
    out_dir = _make_project({"app.py": "def add(a, b):\n    return a - b\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "return a - b", "return a + b"))
        assert result["ok"] is True, result
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            content = f.read()
        assert content == "def add(a, b):\n    return a + b\n", content
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_reports_syntax_ok_field():
    out_dir = _make_project({"app.py": "x = 1\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "x = 1", "x = 2"))
        assert result["ok"] is True and result.get("syntax_ok") is True, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_with_empty_new_string_deletes():
    out_dir = _make_project({"app.py": "import os\nimport sys\n\nprint('hi')\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "import sys\n", ""))
        assert result["ok"] is True, result
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            content = f.read()
        assert content == "import os\n\nprint('hi')\n", content
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 一意マッチの強制(0件・複数件)
# ---------------------------------------------------------------------------

def test_edit_fails_with_no_match_and_leaves_file_untouched():
    original = "def add(a, b):\n    return a - b\n"
    out_dir = _make_project({"app.py": original})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "return x + y", "return a + b"))
        assert result["ok"] is False, result
        assert "見つかりませんでした" in result["message"], result
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            content = f.read()
        assert content == original, "0件一致のときファイルが変更されてはいけません"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_fails_with_multiple_matches_and_reports_line_numbers():
    original = "x = 1\ny = 1\nz = 1\n"
    out_dir = _make_project({"app.py": original})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "= 1", "= 2"))
        assert result["ok"] is False, result
        assert "3箇所" in result["message"], result
        assert result["matched_lines"] == [1, 2, 3], result
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            content = f.read()
        assert content == original, "複数件一致のときファイルが変更されてはいけません"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# パス保護(既存ツールと共通の_resolve_safe_project_path経由)
# ---------------------------------------------------------------------------

def test_edit_rejects_parent_directory_traversal():
    out_dir = _make_project({"app.py": "x = 1\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "../outside.py", "x", "y"))
        assert result["ok"] is False, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_rejects_absolute_path():
    out_dir = _make_project({"app.py": "x = 1\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "/etc/passwd", "x", "y"))
        assert result["ok"] is False, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_rejects_progress_md():
    out_dir = _make_project({yoriai.PROGRESS_FILENAME: "# プロジェクト進行状況\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, yoriai.PROGRESS_FILENAME, "進行状況", "状況"))
        assert result["ok"] is False, result
        assert "PROGRESS.md" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 存在しないファイル(計画上は存在するが未実装 / 計画に無い)
# ---------------------------------------------------------------------------

def test_edit_missing_planned_file_reports_not_yet_implemented():
    out_dir = tempfile.mkdtemp(prefix="yoriai_edit_file_test_")
    try:
        tasks = [("app.py", "..."), ("utils.py", "...")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        result = json.loads(tools._edit_project_file(out_dir, "utils.py", "x", "y"))
        assert result["ok"] is False, result
        assert result["message"] == "そのファイルはまだ存在しません(未実装です)。新規作成の場合はwrite_fileを使ってください。", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_edit_missing_undeclared_file_reports_not_in_plan():
    out_dir = tempfile.mkdtemp(prefix="yoriai_edit_file_test_")
    try:
        tasks = [("app.py", "...")]
        checklist = yoriai._build_task_checklist(tasks)
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        result = json.loads(tools._edit_project_file(out_dir, "script.js", "x", "y"))
        assert result["ok"] is False, result
        assert "計画" in result["message"] and "app.py" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 構文エラーの即時フィードバック
# ---------------------------------------------------------------------------

def test_edit_reports_syntax_error_after_producing_invalid_code():
    out_dir = _make_project({"app.py": "def add(a, b):\n    return a + b\n"})
    try:
        result = json.loads(tools._edit_project_file(out_dir, "app.py", "def add(a, b):", "def add(a, b)"))
        assert result["ok"] is True, result  # 書き込み自体は成功する
        assert result.get("syntax_ok") is False, result
        assert result.get("syntax_error"), result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# ツール登録(スキーマ・実行ディスパッチ・modified_filesへの反映)
# ---------------------------------------------------------------------------

def test_edit_file_tool_registered_in_project_tools():
    assert tools.EDIT_FILE_TOOL_SCHEMA in yoriai.PROJECT_TOOLS_SCHEMAS
    assert tools.EDIT_FILE_TOOL_NAME in yoriai.PROJECT_TOOLS_CLIENT_NAMES


def test_execute_project_tool_call_dispatches_edit_file():
    out_dir = _make_project({"app.py": "x = 1\n"})
    try:
        tool_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "edit_file", "arguments": {"filename": "app.py", "old_string": "x = 1", "new_string": "x = 2"}},
        }
        result = json.loads(tools._execute_project_tool_call(out_dir, tool_call))
        assert result["ok"] is True, result
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            assert f.read() == "x = 2\n"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_mutated_filename_from_tool_result_recognizes_successful_edit_file():
    tool_call = {"function": {"name": "edit_file"}}
    success_result = json.dumps({"ok": True, "filename": "app.py"})
    assert tools._mutated_filename_from_tool_result(tool_call, success_result) == "app.py"

    failure_result = json.dumps({"ok": False, "message": "見つかりませんでした"})
    assert tools._mutated_filename_from_tool_result(tool_call, failure_result) == ""


# ---------------------------------------------------------------------------
# 応答切れによるファイル破壊のガード(_collect_answer_with_project_tools)
# ---------------------------------------------------------------------------

def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def test_truncated_round_skips_write_file_but_not_read_file():
    """truncatedが立ったラウンドにwrite_file・read_fileが両方含まれていた
    場合、write_fileだけが実行されず、read_fileは通常どおり実行される
    ことを確認する。実機のOllama/LM Studio/MLX-LMに接続できない環境の
    ため、`_stream_chat_from_candidate`を差し替えて模擬する。
    """
    out_dir = _make_project({"app.py": "x = 1\n"})
    try:
        calls = {"round": 0}

        def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False):
            calls["round"] += 1
            if calls["round"] == 1:
                yield {
                    "pending_tool_calls": [
                        {
                            "id": "call_read", "type": "function",
                            "function": {"name": "read_file", "arguments": {"filename": "app.py"}},
                        },
                        {
                            "id": "call_write", "type": "function",
                            "function": {"name": "write_file", "arguments": {"filename": "app.py", "content": "x = 999"}},
                        },
                    ],
                    "truncated": True,
                }
                return
            yield {"content": "修正しました"}
            yield {"done": True, "truncated": False}

        original = yoriai._stream_chat_from_candidate
        yoriai._stream_chat_from_candidate = fake_stream
        try:
            answer, error, truncated, modified_files = yoriai._collect_answer_with_project_tools(
                _member("MacStudio", "qwen3-coder-30b"), "org-fp",
                [{"role": "user", "content": "app.pyを直して"}], out_dir,
            )
        finally:
            yoriai._stream_chat_from_candidate = original

        assert error is None, error
        assert "app.py" not in modified_files, (
            f"truncatedなラウンドのwrite_fileは実行されず、modified_filesに含まれてはいけません: {modified_files}"
        )
        with open(os.path.join(out_dir, "app.py"), encoding="utf-8") as f:
            content = f.read()
        assert content == "x = 1\n", f"write_fileが実行されファイルが書き換えられてしまいました: {content!r}"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_edit_replaces_unique_match,
        test_edit_reports_syntax_ok_field,
        test_edit_with_empty_new_string_deletes,
        test_edit_fails_with_no_match_and_leaves_file_untouched,
        test_edit_fails_with_multiple_matches_and_reports_line_numbers,
        test_edit_rejects_parent_directory_traversal,
        test_edit_rejects_absolute_path,
        test_edit_rejects_progress_md,
        test_edit_missing_planned_file_reports_not_yet_implemented,
        test_edit_missing_undeclared_file_reports_not_in_plan,
        test_edit_reports_syntax_error_after_producing_invalid_code,
        test_edit_file_tool_registered_in_project_tools,
        test_execute_project_tool_call_dispatches_edit_file,
        test_mutated_filename_from_tool_result_recognizes_successful_edit_file,
        test_truncated_round_skips_write_file_but_not_read_file,
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
