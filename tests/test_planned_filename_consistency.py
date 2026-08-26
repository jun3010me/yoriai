#!/usr/bin/env python3
"""バグ報告への対応を検証する: 合意フェーズで決めたファイル名と、実際に
実装されたファイル名が食い違い、存在しないファイルへの依存が永久に
解決しない不具合。

実機で、「高校生向けのHTML/CSS学習サイトを作って」という依頼に対し、
設計担当がeditor.js/preview.jsという2ファイルを計画に挙げていながら、
index.htmlの説明文の中では両方をまとめて「script.js」と呼んでしまい、
実装されたindex.htmlがscript.jsを読み込むコードになった。script.js
自体が最初から作られる予定のないファイルのため、レビュー→修正→
再レビュー、その後の//resume-all(自動再開含む)を何度繰り返しても
論理的に解決不可能な状態になっていた。

タスクキューへの割り当て自体は常に計画通りのファイル名で行われる
(_run_collaborative_task_queueのworkerはtasksタプルのファイル名をそのまま
dest_pathに使う)ことをコードから確認済みであり、Yoriai側の処理での
ファイル名の取り違えではなく、設計担当(LLM)の1回の応答内での自己矛盾が
原因と判断した。このファイルでは、(1)予防策(プロンプトへの明示的な
指示追加)、(2)検出・警告(_find_undeclared_file_references、合意フェーズ
直後の機械的チェック)、(3)保険(read_fileが「未実装」と「計画に無い」を
区別して報告する)を検証する。

使い方: python3 tests/test_planned_filename_consistency.py
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


# ---------------------------------------------------------------------------
# 予防: プロンプトへの明示的な指示
# ---------------------------------------------------------------------------

def test_module_breakdown_prompt_forbids_undeclared_file_references():
    prompt = yoriai._build_module_breakdown_prompt(
        "高校生向けのHTML/CSS学習サイトを作って", yoriai.AGREE_REQUEST_TYPE_SOFTWARE,
    )
    assert "一字一句完全に一致" in prompt, prompt


# ---------------------------------------------------------------------------
# 検出: _find_undeclared_file_references
# ---------------------------------------------------------------------------

def test_find_undeclared_file_references_reproduces_reported_bug():
    """実機で報告された状況をそのまま再現する: editor.js/preview.jsが
    計画に挙げられているのに、index.htmlの説明文が別名の"script.js"に
    言及しているケースを検出できることを確認する。
    """
    tasks = [
        ("index.html", "画面を構成する。scriptタグでscript.jsを読み込む。"),
        ("style.css", "見た目を整える。"),
        ("editor.js", "エディタ部分の処理を実装する。"),
        ("preview.js", "プレビュー部分の処理を実装する。"),
    ]
    issues = yoriai._find_undeclared_file_references(tasks)
    assert ("index.html", "script.js") in issues, issues


def test_find_undeclared_file_references_ignores_legitimate_cross_references():
    """他のファイルへの正当な言及(実際に計画に含まれているファイル名)は
    誤検出しないことを確認する。
    """
    tasks = [
        ("storage.py", "ToDoをJSONで管理する。"),
        ("cli.py", "storage.pyのadd_todo/list_todosを呼び出すCLI。"),
    ]
    assert yoriai._find_undeclared_file_references(tasks) == []


def test_find_undeclared_file_references_ignores_unrelated_extensions():
    """このプロジェクトが使っていない拡張子の"x.y"風の文字列
    (関数呼び出しの一部等)を誤ってファイル名として検出しないことを
    確認する。
    """
    tasks = [
        ("storage.py", "add_todo(text: str) -> int を実装する。storage.load_todos()を呼ぶ。"),
        ("cli.py", "storage.add_todoを呼び出すCLI。"),
    ]
    assert yoriai._find_undeclared_file_references(tasks) == []


def test_find_undeclared_file_references_returns_empty_for_consistent_plan():
    tasks = [("calc.c", "四則演算を行うmain関数を実装する。")]
    assert yoriai._find_undeclared_file_references(tasks) == []


# ---------------------------------------------------------------------------
# 保険: read_fileが「未実装」と「計画に無い」を区別する
# ---------------------------------------------------------------------------

def test_planned_filenames_from_progress_reads_current_plan():
    tasks = [("index.html", "..."), ("editor.js", "..."), ("preview.js", "...")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        planned = yoriai._planned_filenames_from_progress(out_dir)
        assert planned == {"index.html", "editor.js", "preview.js"}, planned
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_planned_filenames_from_progress_returns_empty_set_without_progress_md():
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        assert yoriai._planned_filenames_from_progress(out_dir) == set()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_reports_undeclared_file_distinctly():
    """依頼の項目3(保険): 存在しないファイルが、計画にすら含まれていない
    (script.jsのような架空のファイル名)場合、通常の「まだ実装されて
    いません」とは異なる、計画に実在するファイル一覧を含むメッセージが
    返ることを確認する。
    """
    tasks = [("index.html", "..."), ("editor.js", "..."), ("preview.js", "...")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "script.js"))
        assert result["exists"] is False, result
        assert "計画" in result["message"], result
        assert "editor.js" in result["message"] and "preview.js" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_still_reports_pending_planned_file_as_not_yet_implemented():
    """回帰確認: 計画に含まれてはいるが、まだディスクに保存されていない
    (実装がこれから行われる)ファイルについては、これまで通り
    「まだ実装されていません」という通常のメッセージが返ることを確認する
    (「計画に無い」という新しい警告に誤って巻き込まれないことの確認)。
    """
    tasks = [("index.html", "..."), ("editor.js", "..."), ("preview.js", "...")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        # editor.jsはまだディスクに保存されていない(実装前)状態を模す。
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "editor.js"))
        assert result["exists"] is False, result
        assert result["message"] == "そのファイルはまだ存在しません(未実装です)。", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_falls_back_to_generic_message_without_progress_md():
    """回帰確認: PROGRESS.mdが存在しないディレクトリ(既存のread_file
    テストの多くがこの形)では、これまで通りの汎用メッセージのままである
    ことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "config.py"))
        assert result["message"] == "そのファイルはまだ存在しません(未実装です)。", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 統合テスト: _ask_organization_collaborate
# ---------------------------------------------------------------------------

def test_collaborate_warns_about_undeclared_file_reference_from_architect():
    """依頼の動作確認に近い統合テスト: 設計担当がscript.jsという架空の
    ファイルに言及した場合、実装・レビューを始める前の時点で警告が
    表示されることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    architect_answer = (
        "index.html: 画面を構成する。scriptタグでscript.jsを読み込む。\n"
        "style.css: 見た目を整える。\n"
        "editor.js: エディタ部分の処理を実装する。\n"
        "preview.js: プレビュー部分の処理を実装する。\n"
    )

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        request_text = messages[0]["content"]
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": architect_answer}
        else:
            yield {"content": "```html\n<html></html>\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(
                47120, "fingerprint", "高校生向けのHTML/CSS学習サイトを作って", out_dir,
            )
        output = buf.getvalue()
        assert "script.js" in output and "計画に無いファイル" in output, output
        assert "index.html" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collaborate_does_not_warn_for_consistent_plan():
    """回帰確認: 計画が自己矛盾していない通常のケース(既存のToDoリスト
    依頼)では、この新しい警告が誤って表示されないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        request_text = messages[0]["content"]
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": (
                "storage.py: ToDoをJSONで管理する。add_todo(text: str) -> int を実装する。\n"
                "cli.py: storage.pyのadd_todoを呼び出すCLI。\n"
            )}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)
        output = buf.getvalue()
        assert "計画に無いファイル" not in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_module_breakdown_prompt_forbids_undeclared_file_references,
        test_find_undeclared_file_references_reproduces_reported_bug,
        test_find_undeclared_file_references_ignores_legitimate_cross_references,
        test_find_undeclared_file_references_ignores_unrelated_extensions,
        test_find_undeclared_file_references_returns_empty_for_consistent_plan,
        test_planned_filenames_from_progress_reads_current_plan,
        test_planned_filenames_from_progress_returns_empty_set_without_progress_md,
        test_read_project_file_fresh_reports_undeclared_file_distinctly,
        test_read_project_file_fresh_still_reports_pending_planned_file_as_not_yet_implemented,
        test_read_project_file_fresh_falls_back_to_generic_message_without_progress_md,
        test_collaborate_warns_about_undeclared_file_reference_from_architect,
        test_collaborate_does_not_warn_for_consistent_plan,
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
