#!/usr/bin/env python3
"""チェックポイント運用によるセッション再開(PR0)を検証する。

背景: 現状、ネットワーク障害・ノードのスリープ・タイムアウトなどで
セッションが中断すると、それまでの進捗を保持したまま続きから再開する
のではなく、元の依頼文をまるごと新しい試行として再投入していた
(`_resolve_project_dir`が常に未使用のディレクトリ名を割り当てるため)。
実際の運用ログで、同一の依頼が3回丸ごと再実行され2日を消費した不具合が
確認されている。この問題への対応として、

1. 検証済み(タスク単位の実行検証グラウンディング・プロジェクト全体の
   統合検証)になった時点で、`project_dir`自体をgitリポジトリとして
   コミットする(`_git_commit_checkpoint`)。
2. 同じ依頼文が未完了プロジェクトが残っている状態で再投入された場合、
   新規プロジェクトとしてではなく既存の未完了プロジェクトを
   チェックポイントから再開する(`_find_matching_incomplete_project`・
   `_ask_organization_collaborate`)。

を実装した。なお、既存の「同じツール呼び出しが3回繰り返されたら打ち切り」
という堂々巡り検出ロジック(`PROJECT_TOOL_LOOP_ERROR_MARKER`)が、打ち切り
後もセッション全体を巻き込まず該当タスクのみを一時保留する(バックログに
残す)ことは、`tests/test_fix_project.py`の
`test_fix_project_loop_failed_subtask_gets_resplit_into_finer_subtasks_end_to_end`
等で既に確認済みのため、ここでは重複させない。

使い方: python3 tests/test_checkpoint_resume.py
"""
import contextlib
import io
import os
import shutil
import subprocess
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


def _git_log_messages(project_dir):
    result = subprocess.run(
        ["git", "log", "--pretty=%s"], cwd=project_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


# ---------------------------------------------------------------------------
# _ensure_project_git_repo / _git_commit_checkpoint
# ---------------------------------------------------------------------------

def test_ensure_project_git_repo_initializes_and_is_idempotent():
    project_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        assert not os.path.isdir(os.path.join(project_dir, ".git"))
        yoriai._ensure_project_git_repo(project_dir)
        assert os.path.isdir(os.path.join(project_dir, ".git"))
        # 2回目は既に初期化済みのため何もしない(例外を出さずに成功する)。
        yoriai._ensure_project_git_repo(project_dir)
        assert os.path.isdir(os.path.join(project_dir, ".git"))
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_git_commit_checkpoint_commits_with_task_content_in_message():
    project_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        with open(os.path.join(project_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")

        ok = yoriai._git_commit_checkpoint(project_dir, "checkpoint: storage.py (実行検証OK)")
        assert ok is True

        messages = _git_log_messages(project_dir)
        assert len(messages) == 1, messages
        assert "storage.py" in messages[0], messages
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_git_commit_checkpoint_is_noop_when_nothing_changed():
    """変更が無い状態で再度呼んでも、新しいコミットは作られない
    (空コミットで履歴を無意味に汚さない)ことを確認する。
    """
    project_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        with open(os.path.join(project_dir, "a.py"), "w", encoding="utf-8") as f:
            f.write("pass\n")
        assert yoriai._git_commit_checkpoint(project_dir, "checkpoint: a.py") is True
        assert len(_git_log_messages(project_dir)) == 1

        # ファイルを変更せずにもう一度呼ぶ。
        assert yoriai._git_commit_checkpoint(project_dir, "checkpoint: a.py (2回目)") is True
        assert len(_git_log_messages(project_dir)) == 1, "変更が無い場合は新しいコミットを作らないはずです"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# タスク完了・検証成功後のチェックポイントコミット(タスクキュー方式への組み込み)
# ---------------------------------------------------------------------------

def _fake_stream_for_two_file_project(candidate, org_fingerprint, messages, **_kwargs):
    request_text = messages[0]["content"]
    if "ファイルに分割する実装計画" in request_text:
        yield {"content": "storage.py: add_todo(text: str) -> int を実装\ncli.py: storage.add_todoを呼び出すCLI\n"}
    elif "レビュー対象" in request_text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_collaborate_commits_checkpoint_after_each_completed_task():
    """依頼の完了条件: 各タスクが検証済みになった時点で、そのタスク単位で
    `git commit`が実行され、コミットメッセージにタスク内容(ファイル名)が
    含まれることを確認する(`//agree`の実行フェーズ全体を通した統合確認)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_for_two_file_project

    out_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME,
            yoriai._project_name_with_date_prefix("ToDoリストのCLIツールを作って"),
        )
        assert os.path.isdir(os.path.join(project_dir, ".git")), "project_dir がgitリポジトリとして初期化されているはずです"
        messages = _git_log_messages(project_dir)
        assert any("storage.py" in m for m in messages), messages
        assert any("cli.py" in m for m in messages), messages
        assert any(m.startswith("checkpoint:") for m in messages), messages
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 同一の依頼文の再投入 → 新規プロジェクトではなくチェックポイントから再開
# ---------------------------------------------------------------------------

_REQUEST = "ToDoリストのCLIツールを作って"
_TASKS = [
    ("storage.py", "ToDoの永続化を担当。add_todo/list_todosを実装する。"),
    ("cli.py", "コマンドライン操作を担当。add/listサブコマンドを実装する。"),
]


def _seed_interrupted_project(out_dir):
    """`storage.py`だけが完了し、`cli.py`が未完了のまま(セッションが
    中断した想定)のプロジェクトディレクトリを用意する。
    """
    projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
    project_name = yoriai._project_name_with_date_prefix(_REQUEST)
    project_dir = os.path.join(projects_root, project_name)

    checklist = yoriai._build_task_checklist(_TASKS)
    yoriai._set_task_status(checklist, "storage.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "storage.py", "review", yoriai._TASK_STATUS_COMPLETED)

    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "storage.py"), "w", encoding="utf-8") as f:
        f.write("def add_todo(text):\n    pass\n")
    yoriai._write_progress_md(project_dir, _REQUEST, _TASKS, checklist, {})
    # 中断前の最後のチェックポイントも再現しておく。
    yoriai._git_commit_checkpoint(project_dir, "checkpoint: storage.py (レビューOK)")
    return project_dir


def test_resubmitting_identical_request_resumes_existing_project_instead_of_creating_new_one():
    """依頼の中核シナリオ: セッションが中断し、同じ依頼文が新しい試行として
    再投入されても、`<name>-2`のような新規プロジェクトを作らず、既存の
    未完了プロジェクトの続き(未完了だった`cli.py`だけ)から再開すること、
    かつ既に完了していた`storage.py`の実装依頼が再送されないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()

    implementation_requests = []

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "あなたが実装を担当するファイル" in text:
            implementation_requests.append(text)
        if "レビュー対象" in text:
            yield {"content": "問題なし"}
        else:
            yield {"content": "```python\ndef list_todos():\n    pass\n```"}
        yield {"done": True}

    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        project_dir = _seed_interrupted_project(out_dir)
        commits_before = len(_git_log_messages(project_dir))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", _REQUEST, out_dir)
        output = buf.getvalue()

        assert "チェックポイントから再開" in output, output

        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_name = yoriai._project_name_with_date_prefix(_REQUEST)
        assert set(os.listdir(projects_root)) == {project_name}, (
            f"新規の連番プロジェクトが作られてはいけません: {os.listdir(projects_root)}"
        )

        # storage.py(既に完了済み)への実装依頼は再送されず、
        # 未完了だったcli.pyだけが実装されるはずです。
        assert not any("storage.py" in req for req in implementation_requests), implementation_requests
        assert any("cli.py" in req for req in implementation_requests), implementation_requests

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert yoriai._progress_checklist_is_incomplete(parsed["checklist"]) is False, parsed

        # 中断前のチェックポイント以降、cli.py用の新しいコミットが
        # 追加されているはずです(履歴が失われず積み上がる)。
        assert len(_git_log_messages(project_dir)) > commits_before
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_find_matching_incomplete_project_ignores_non_matching_request_text():
    """依頼文が異なる未完了プロジェクトは、たとえ未完了であっても
    再開対象として誤って拾わないことを確認する(依頼文の完全一致のみを
    対象とする、という`_find_matching_incomplete_project`の仮の判断)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        _seed_interrupted_project(out_dir)
        assert progress._find_matching_incomplete_project(out_dir, "全く別の依頼文") is None
        assert progress._find_matching_incomplete_project(out_dir, _REQUEST) is not None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_list_project_files_excludes_git_directory():
    """`.git`はチェックポイント用のリポジトリの内部データであり、生成物
    でもモデルが編集すべきファイルでもないため、`_list_project_files`
    (list_dirツール・構文チェック・//fixのファイル一覧が共通して使う)の
    結果には含まれないことを確認する。
    """
    project_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        with open(os.path.join(project_dir, "a.py"), "w", encoding="utf-8") as f:
            f.write("pass\n")
        yoriai._git_commit_checkpoint(project_dir, "checkpoint: a.py")
        assert os.path.isdir(os.path.join(project_dir, ".git"))

        files = yoriai._list_project_files(project_dir)
        assert files == ["a.py"], files
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_resubmitting_request_creates_new_project_when_no_incomplete_project_matches():
    """未完了プロジェクトが存在しない(依頼文が異なる、または既に完了して
    いる)場合は、従来通り新規プロジェクトとして扱われることを確認する
    (依頼の要件5と同じ発想: 中断からの再開と、単に同じ依頼をもう一度
    頼みたい場合とを、`_find_matching_incomplete_project`は「未完了の
    プロジェクトが残っているか」で区別する)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_checkpoint_test_")
    try:
        assert progress._find_matching_incomplete_project(out_dir, _REQUEST) is None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_ensure_project_git_repo_initializes_and_is_idempotent,
        test_git_commit_checkpoint_commits_with_task_content_in_message,
        test_git_commit_checkpoint_is_noop_when_nothing_changed,
        test_collaborate_commits_checkpoint_after_each_completed_task,
        test_resubmitting_identical_request_resumes_existing_project_instead_of_creating_new_one,
        test_find_matching_incomplete_project_ignores_non_matching_request_text,
        test_list_project_files_excludes_git_directory,
        test_resubmitting_request_creates_new_project_when_no_incomplete_project_matches,
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
