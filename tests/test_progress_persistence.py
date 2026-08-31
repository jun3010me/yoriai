#!/usr/bin/env python3
"""協業モードの進行状況をPROGRESS.mdとして永続化する仕組みを検証する。

read_fileツール(実装依頼元がディスク上のファイルを都度読み直す設計)と
同じ「ディスク上の実際の状態を正として扱う」方針に基づき、協業モードの
進行中、プロジェクトディレクトリにPROGRESS.md(元の依頼・モジュール分割案・
タスクの状態・直近のレビュー指摘)を作成・更新し続ける。プロセスが途中で
終了しても、`//resume-all`(tests/test_resume_all.py参照)がこのファイルを
読み直して未完了タスクだけを対象に再開できるようにするのが目的。

使い方: python3 tests/test_progress_persistence.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import progress  # noqa: E402
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


_FOUR_FILE_BREAKDOWN_ANSWER = (
    "storage.py: データの永続化を担当\n"
    "cli.py: コマンドライン操作を担当\n"
    "utils.py: 共通のユーティリティ関数を担当\n"
    "config.py: 設定値の管理を担当\n"
)


def test_format_and_parse_progress_markdown_round_trips():
    """`_format_progress_markdown`で組み立てた内容を`_parse_progress_markdown`
    (`_extract_progress_section`・`_parse_checklist_markdown`経由)で
    読み戻すと、元の依頼・タスク一覧・チェックリストの状態が正しく
    復元できることを確認する。
    """
    request = "ToDoリストのCLIツールを作って"
    tasks = [("storage.py", "永続化を担当"), ("cli.py", "CLI操作を担当")]
    checklist = yoriai._build_task_checklist(tasks)
    yoriai._set_task_status(checklist, "storage.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "storage.py", "review", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "cli.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    # cli.pyのレビューはあえて未完了(⏳)のままにする。

    out_dir = tempfile.mkdtemp(prefix="yoriai_progress_test_")
    try:
        yoriai._write_progress_md(
            out_dir, request, tasks, checklist, review_feedback={"cli.py": "引数の扱いが計画と異なります。"},
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert parsed is not None
    assert parsed["request"] == request
    assert parsed["tasks"] == tasks
    assert len(parsed["checklist"]) == 4
    by_label = {t["label"]: t for t in parsed["checklist"]}
    assert by_label["storage.py の実装"]["status"] == yoriai._TASK_STATUS_COMPLETED
    assert by_label["storage.py のレビュー"]["status"] == yoriai._TASK_STATUS_COMPLETED
    assert by_label["cli.py の実装"]["status"] == yoriai._TASK_STATUS_COMPLETED
    assert by_label["cli.py のレビュー"]["status"] == progress._TASK_STATUS_PENDING


def test_progress_markdown_includes_review_feedback_section():
    content = progress._format_progress_markdown(
        "何か作って", [("a.py", "説明")], yoriai._build_task_checklist([("a.py", "説明")]),
        review_feedback={"a.py": "型が計画と一致していません。"},
    )
    assert "## 直近のレビュー指摘" in content
    assert "### a.py" in content
    assert "型が計画と一致していません。" in content


def test_progress_markdown_omits_review_feedback_section_when_empty():
    content = progress._format_progress_markdown(
        "何か作って", [("a.py", "説明")], yoriai._build_task_checklist([("a.py", "説明")]), review_feedback={},
    )
    assert "## 直近のレビュー指摘" not in content


def test_parse_review_feedback_markdown_reads_back_per_file_sections():
    """`_format_progress_markdown`が書き出した「## 直近のレビュー指摘」
    セクションを`_parse_review_feedback_markdown`に渡すと、元の
    `{filename: feedback}`辞書がそのまま復元できることを確認する
    (往復テスト)。複数行にまたがる指摘文言(実際のレビュー担当の
    返答は「問題あり」の行に続けて理由が書かれる)も崩れないことを
    合わせて確認する。
    """
    review_feedback = {
        "cli.py": "cli.pyの引数の扱いが計画と一致していません。",
        "config.py": "問題あり\nconfig.pyの定数名が計画と一致していません。",
    }
    content = progress._format_progress_markdown(
        "何か作って", [("cli.py", "説明"), ("config.py", "説明")],
        yoriai._build_task_checklist([("cli.py", "説明"), ("config.py", "説明")]),
        review_feedback=review_feedback,
    )
    assert progress._parse_review_feedback_markdown(content) == review_feedback


def test_parse_review_feedback_markdown_returns_empty_dict_when_section_missing():
    content = progress._format_progress_markdown(
        "何か作って", [("a.py", "説明")], yoriai._build_task_checklist([("a.py", "説明")]), review_feedback={},
    )
    assert progress._parse_review_feedback_markdown(content) == {}


def test_find_repeated_review_feedback_detects_unchanged_text_for_incomplete_file():
    checklist = yoriai._build_task_checklist([("cli.py", "説明")])
    # cli.pyのレビューは未完了のままにする。
    previous = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    current = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    assert progress._find_repeated_review_feedback(previous, current, checklist) == ["cli.py"]


def test_find_repeated_review_feedback_ignores_completed_files():
    checklist = yoriai._build_task_checklist([("cli.py", "説明")])
    yoriai._set_task_status(checklist, "cli.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "cli.py", "review", yoriai._TASK_STATUS_COMPLETED)
    previous = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    current = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    assert progress._find_repeated_review_feedback(previous, current, checklist) == []


def test_find_repeated_review_feedback_ignores_when_text_changed():
    checklist = yoriai._build_task_checklist([("cli.py", "説明")])
    previous = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    current = {"cli.py": "cli.pyの戻り値の型が計画と一致していません。"}
    assert progress._find_repeated_review_feedback(previous, current, checklist) == []


def test_find_repeated_review_feedback_ignores_first_attempt_with_no_previous_record():
    checklist = yoriai._build_task_checklist([("cli.py", "説明")])
    previous = {}
    current = {"cli.py": "cli.pyの引数の扱いが計画と一致していません。"}
    assert progress._find_repeated_review_feedback(previous, current, checklist) == []


def test_progress_checklist_is_incomplete_detects_pending_and_in_progress():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    assert yoriai._progress_checklist_is_incomplete(checklist) is True

    yoriai._set_task_status(checklist, "a.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "a.py", "review", yoriai._TASK_STATUS_COMPLETED)
    assert yoriai._progress_checklist_is_incomplete(checklist) is False


def test_pending_tasks_from_checklist_returns_only_incomplete_files():
    tasks = [("storage.py", "永続化"), ("cli.py", "CLI"), ("config.py", "設定")]
    checklist = yoriai._build_task_checklist(tasks)
    for filename in ("storage.py", "cli.py"):
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    # config.pyは実装のみ完了、レビューは未完了のままにする。
    yoriai._set_task_status(checklist, "config.py", "impl", yoriai._TASK_STATUS_COMPLETED)

    pending = yoriai._pending_tasks_from_checklist(tasks, checklist)
    assert pending == [("config.py", "設定")], pending


def test_parse_progress_markdown_returns_none_for_missing_file():
    assert yoriai._parse_progress_markdown("/does/not/exist/PROGRESS.md") is None


def test_find_incomplete_projects_scans_projects_subdir():
    out_dir = tempfile.mkdtemp(prefix="yoriai_progress_scan_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        os.makedirs(projects_root)

        done_tasks = [("a.py", "説明")]
        done_checklist = yoriai._build_task_checklist(done_tasks)
        yoriai._set_task_status(done_checklist, "a.py", "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(done_checklist, "a.py", "review", yoriai._TASK_STATUS_COMPLETED)
        done_dir = os.path.join(projects_root, "done-project")
        yoriai._write_progress_md(done_dir, "完了済みの依頼", done_tasks, done_checklist, {})

        pending_tasks = [("b.py", "説明")]
        pending_checklist = yoriai._build_task_checklist(pending_tasks)
        pending_dir = os.path.join(projects_root, "pending-project")
        yoriai._write_progress_md(pending_dir, "未完了の依頼", pending_tasks, pending_checklist, {})

        # PROGRESS.mdを持たないディレクトリ(生成物のみのプロジェクト等)は無視される。
        os.makedirs(os.path.join(projects_root, "no-progress-file"))

        incomplete = yoriai._find_incomplete_projects(out_dir)
        assert incomplete == [pending_dir], incomplete
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_find_incomplete_projects_returns_empty_list_when_no_projects_dir():
    out_dir = tempfile.mkdtemp(prefix="yoriai_progress_scan_test_")
    try:
        assert yoriai._find_incomplete_projects(out_dir) == []
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collaborate_writes_progress_md_reflecting_partial_completion():
    """依頼の動作確認: 4ファイル構成のタスクを、意図的に一部(config.py)を
    未完了のまま終了させ、PROGRESS.mdが正しく作成・更新されることを
    確認する。config.pyのレビューだけが常に「問題あり」を返すように
    モデルを模擬し、最終的にPROGRESS.mdに(1)元の依頼、(2)4ファイル分の
    モジュール分割案、(3)config.pyのレビューだけが未完了のチェック
    リスト、(4)config.pyについての直近のレビュー指摘、がすべて正しく
    記録されていることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    request = "4つのモジュールに分かれたツールを作って"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "ファイルに分割する実装計画" in text:
            yield {"content": _FOUR_FILE_BREAKDOWN_ANSWER}
        elif "レビュー対象: config.py" in text:
            yield {"content": "問題あり\nconfig.pyの定数名が計画と一致していません。"}
        elif "レビュー対象" in text:
            yield {"content": "問題なし"}
        elif "現在のコード" in text:
            # 修正を試みても直らない、という状況を再現する(常に同じコードを返す)。
            yield {"content": "```python\nDB_PATH = 'todo.json'\n```"}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_progress_integration_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", request, out_dir)
        output = buf.getvalue()

        project_dir = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._project_name_with_date_prefix(request))
        progress_path = os.path.join(project_dir, yoriai.PROGRESS_FILENAME)
        assert os.path.isfile(progress_path), "PROGRESS.mdが作成されていません"

        parsed = yoriai._parse_progress_markdown(progress_path)
        assert parsed is not None, "PROGRESS.mdを解析できませんでした"
        assert parsed["request"] == request
        assert {fn for fn, _ in parsed["tasks"]} == {"storage.py", "cli.py", "utils.py", "config.py"}

        by_label = {t["label"]: t["status"] for t in parsed["checklist"]}
        assert by_label["storage.py の実装"] == yoriai._TASK_STATUS_COMPLETED
        assert by_label["storage.py のレビュー"] == yoriai._TASK_STATUS_COMPLETED
        assert by_label["config.py の実装"] == yoriai._TASK_STATUS_COMPLETED
        assert by_label["config.py のレビュー"] != yoriai._TASK_STATUS_COMPLETED, (
            "config.pyのレビューは未完了のまま残るはずです"
        )

        with open(progress_path, encoding="utf-8") as f:
            progress_content = f.read()
        assert "### config.py" in progress_content
        assert "定数名が計画と一致していません" in progress_content

        assert "[⚠️ 未完了のタスクが残っています" in output
        assert "config.py のレビュー" in output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_format_and_parse_progress_markdown_round_trips,
        test_progress_markdown_includes_review_feedback_section,
        test_progress_markdown_omits_review_feedback_section_when_empty,
        test_parse_review_feedback_markdown_reads_back_per_file_sections,
        test_parse_review_feedback_markdown_returns_empty_dict_when_section_missing,
        test_find_repeated_review_feedback_detects_unchanged_text_for_incomplete_file,
        test_find_repeated_review_feedback_ignores_completed_files,
        test_find_repeated_review_feedback_ignores_when_text_changed,
        test_find_repeated_review_feedback_ignores_first_attempt_with_no_previous_record,
        test_progress_checklist_is_incomplete_detects_pending_and_in_progress,
        test_pending_tasks_from_checklist_returns_only_incomplete_files,
        test_parse_progress_markdown_returns_none_for_missing_file,
        test_find_incomplete_projects_scans_projects_subdir,
        test_find_incomplete_projects_returns_empty_list_when_no_projects_dir,
        test_collaborate_writes_progress_md_reflecting_partial_completion,
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
