#!/usr/bin/env python3
"""実装済みサブタスクの事前確認(`_skip_already_satisfied_fix_subtasks`)を
検証する。

実機報告(projects/260917-cli-json)の再現: 要件(add_productの
on_conflict、update/deleteのignore_missing)がすでにコードに実装済みで、
検証コマンドも通っているのに、PROGRESS.mdに残っていた「未完了の修正
サブタスク」がそのまま再開され、実装済みの機能を複数台が再実装していた。
割り当て前に「すでに満たされているか」を確認し、満たされていれば実装
せず完了扱いにする(キューが縮む)こと、判定が不確実な場合は従来どおり
実行する側に倒すことを確認する。

LLMノード(/chat)は`yoriai._stream_chat_from_candidate`を差し替えて
モックし、組織への問い合わせは`yoriai._fetch_org_snapshot`を差し替えて
2台構成を模擬する。

使い方: python3 tests/test_skip_satisfied_subtasks.py
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
        "generated_at": "2026-09-24T00:00:00+0900",
    }


def _two_member_snapshot():
    peers = [{
        "card": _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20),
        "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0,
    }]
    return {"self": _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40), "peers": peers}


_MANAGER_SOURCE = '''def add_product(path, id, name, quantity, on_conflict="error"):
    if on_conflict not in ("error", "update", "skip"):
        raise ValueError(on_conflict)
    return []


def update_quantity(path, id, quantity, ignore_missing=False):
    return []
'''

_VERIFY_SOURCE = '''import inventory_manager

assert inventory_manager.add_product("p", 1, "A", 1, on_conflict="skip") == []
print("ok")
'''

_SUBTASK_ON_CONFLICT = "inventory_manager.py の add_product に on_conflict=\"error|update|skip\" を追加する"
_SUBTASK_IGNORE_MISSING = "inventory_manager.py の update_quantity に ignore_missing を追加する"
_SUBTASK_LIMITS = "inventory_manager.py に資源制限(max_depth)を追加する"
_PENDING_SUBTASKS = [_SUBTASK_ON_CONFLICT, _SUBTASK_LIMITS, _SUBTASK_IGNORE_MISSING]
_REQUEST = "在庫管理CLIツールを作って"


def _write_project_with_pending_queue(root, verify_command="python3 test_verify.py", verify_source=_VERIFY_SOURCE):
    """実装済みのコード + 未着手の修正サブタスクが残ったPROGRESS.mdを持つ
    プロジェクト(260917-cli-jsonの状況の縮小版)を作る。
    """
    tasks = [("inventory_manager.py", "在庫ロジック"), ("test_verify.py", "検証")]
    checklist = yoriai._build_task_checklist(tasks)
    for filename, _content in tasks:
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    project_dir = os.path.join(root, yoriai.PROJECTS_SUBDIR_NAME, "260917-cli-json")
    yoriai._write_progress_md(
        project_dir, _REQUEST, tasks, checklist, {}, language="Python",
        pending_fix_request=_REQUEST, pending_fix_subtasks=list(_PENDING_SUBTASKS),
        verify_command=verify_command,
    )
    with open(os.path.join(project_dir, "inventory_manager.py"), "w", encoding="utf-8") as f:
        f.write(_MANAGER_SOURCE)
    with open(os.path.join(project_dir, "test_verify.py"), "w", encoding="utf-8") as f:
        f.write(verify_source)
    return project_dir


def _prompt_text(messages):
    return messages[0]["content"] if messages else ""


class _FakeOrg:
    """事前確認・実装・レビューの各問い合わせを記録するフェイクの/chat。
    `check_answers`は「サブタスク文に含まれるキーワード → 事前確認への
    回答」の対応表。
    """

    def __init__(self, check_answers, check_error=None):
        self.check_answers = check_answers
        self.check_error = check_error
        self.checked_subtasks = []
        self.implemented_subtasks = []
        self.pending_seen_at_impl = None
        self.project_dir = None

    def stream(self, candidate, org_fingerprint, messages, offer_project_tools=False, offer_read_file_tool=False, **_kwargs):
        prompt = _prompt_text(messages)
        if offer_read_file_tool:
            subtask = next(st for st in _PENDING_SUBTASKS if st in prompt)
            self.checked_subtasks.append(subtask)
            if self.check_error:
                yield {"error": self.check_error}
                return
            answer = next((a for key, a in self.check_answers.items() if key in subtask), "判定: 未実装")
            yield {"content": answer}
            yield {"done": True}
            return
        if "改修レビュー担当" in prompt:
            yield {"content": "問題なし"}
            yield {"done": True}
            return
        # 実装担当: 最初のラウンドで1ファイル書き込み、次のラウンドで報告する。
        if not any(m.get("role") == "tool" for m in messages):
            subtask = next(st for st in _PENDING_SUBTASKS if st in prompt)
            self.implemented_subtasks.append(subtask)
            if self.pending_seen_at_impl is None and self.project_dir:
                parsed = yoriai._parse_progress_markdown(os.path.join(self.project_dir, yoriai.PROGRESS_FILENAME))
                self.pending_seen_at_impl = list(parsed["pending_fix_subtasks"])
            yield {"pending_tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "write_file",
                "arguments": {"filename": "inventory_manager.py", "content": _MANAGER_SOURCE + "\nMAX_DEPTH = 32\n"},
            }}]}
            return
        yield {"content": "実装しました。"}
        yield {"done": True}


def _run_resume(project_dir, fake):
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake.stream
    fake.project_dir = project_dir
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._resume_project(project_dir, 47120, "fingerprint")
        return buf.getvalue()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream


_SATISFIED_ANSWERS = {
    "on_conflict": "判定: 実装済み\n根拠: inventory_manager.py: on_conflict\n根拠: test_verify.py: on_conflict=\"skip\"",
    "ignore_missing": "判定: 実装済み\n根拠: inventory_manager.py: ignore_missing",
}


# ---------------------------------------------------------------------------
# 回答の解析・根拠の裏付け(純粋関数)
# ---------------------------------------------------------------------------

def test_parse_answer_extracts_verdict_and_evidence():
    satisfied, evidence = yoriai._parse_fix_subtask_satisfied_answer(
        "判定: 実装済み\n根拠: a.py: on_conflict\n- 根拠：`b.py`：`--on-conflict`",
    )
    assert satisfied is True
    assert evidence == [("a.py", "on_conflict"), ("b.py", "--on-conflict")], evidence


def test_parse_answer_treats_ambiguous_or_missing_verdict_as_unsatisfied():
    assert yoriai._parse_fix_subtask_satisfied_answer("判定: 未実装")[0] is False
    assert yoriai._parse_fix_subtask_satisfied_answer("判定: 実装済み または 未実装")[0] is False
    assert yoriai._parse_fix_subtask_satisfied_answer("実装済みだと思います")[0] is False
    assert yoriai._parse_fix_subtask_satisfied_answer("")[0] is False


def test_evidence_must_point_to_existing_file_and_text():
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        files = yoriai._list_project_files(project_dir)
        ok, _ = yoriai._fix_subtask_evidence_is_grounded(project_dir, files, [("inventory_manager.py", "on_conflict")])
        assert ok
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, [])[0]
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, [("missing.py", "on_conflict")])[0]
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, [("inventory_manager.py", "max_depth")])[0]
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, [("inventory_manager.py", "id")])[0]
        # 1件でも裏付けられない根拠があれば全体として認めない。
        assert not yoriai._fix_subtask_evidence_is_grounded(
            project_dir, files, [("inventory_manager.py", "on_conflict"), ("inventory_manager.py", "max_depth")],
        )[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_evidence_accepts_paraphrased_code_but_rejects_invented_identifiers():
    """実ノードは根拠を`add_product(..., on_conflict="error|update|skip")`の
    ように省略を交えて書く(実機計測で確認)。含まれる識別子がすべて実在
    すれば認め、実在しない識別子が1つでも混じれば認めない。
    """
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        files = yoriai._list_project_files(project_dir)
        paraphrased = [("inventory_manager.py", 'add_product(..., on_conflict="error|update|skip")')]
        assert yoriai._fix_subtask_evidence_is_grounded(project_dir, files, paraphrased)[0]
        invented = [("inventory_manager.py", "add_product(..., on_conflict) と _validate_on_conflict() を定義")]
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, invented)[0]
        no_identifier = [("inventory_manager.py", "重複時にエラーを出す")]
        assert not yoriai._fix_subtask_evidence_is_grounded(project_dir, files, no_identifier)[0]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 再開時のキューの縮小(実機報告の再現)
# ---------------------------------------------------------------------------

def test_resume_skips_already_implemented_subtasks_and_shrinks_queue():
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        fake = _FakeOrg(_SATISFIED_ANSWERS)
        output = _run_resume(project_dir, fake)

        assert sorted(fake.checked_subtasks) == sorted(_PENDING_SUBTASKS), fake.checked_subtasks
        # 実装済みの2件は割り当てられず、未実装の1件だけが実装される。
        assert fake.implemented_subtasks == [_SUBTASK_LIMITS], fake.implemented_subtasks
        assert "[🔎 事前確認の結果: 3件中2件を実装済みのためスキップし、残り1件をキューに入れます]" in output, output
        assert "は実装済みのためスキップします(根拠: inventory_manager.py: on_conflict" in output, output
        assert "サブタスク2" not in output, "キューには残りの1件だけが入るはずです: " + output

        # 実装が始まる時点で、PROGRESS.mdのキューはすでに縮んでいる
        # (この後タイムアウト等で打ち切られても、古いキューは復活しない)。
        assert fake.pending_seen_at_impl == [_SUBTASK_LIMITS], fake.pending_seen_at_impl

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["pending_fix_subtasks"] == [], parsed["pending_fix_subtasks"]
        skip_entries = [line for line in parsed["changelog"] if "実装済みのためスキップ" in line]
        assert len(skip_entries) == 2, parsed["changelog"]
        assert any("on_conflict" in line and "根拠: inventory_manager.py: on_conflict" in line for line in skip_entries)
        # 書き直しで検証コマンドの記録が消えない。
        assert parsed["verify_command"] == "python3 test_verify.py", parsed
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_all_subtasks_satisfied_means_nothing_is_implemented_and_queue_is_cleared():
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        answers = dict(_SATISFIED_ANSWERS)
        answers["max_depth"] = "判定: 実装済み\n根拠: test_verify.py: inventory_manager"
        fake = _FakeOrg(answers)
        output = _run_resume(project_dir, fake)

        assert fake.implemented_subtasks == [], fake.implemented_subtasks
        assert "[✅ すべてのサブタスクが実装済みだったため、実装は行いませんでした]" in output, output
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["pending_fix_subtasks"] == []
        assert yoriai._find_incomplete_projects(root) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 不確実な場合は実行する側に倒す
# ---------------------------------------------------------------------------

def test_ungrounded_evidence_is_not_skipped():
    """「実装済み」と答えても、根拠の文字列が実際のファイルに無ければ
    スキップしない。
    """
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        answers = {"max_depth": "判定: 実装済み\n根拠: inventory_manager.py: max_depth"}
        fake = _FakeOrg(answers)
        output = _run_resume(project_dir, fake)
        assert sorted(fake.implemented_subtasks) == sorted(_PENDING_SUBTASKS), fake.implemented_subtasks
        assert "実装済みのためスキップします" not in output, output
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_check_error_falls_back_to_executing_every_subtask():
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root)
        fake = _FakeOrg(_SATISFIED_ANSWERS, check_error="HTTPConnectionPool(host='100.91.10.76', port=47120): Read timed out.")
        output = _run_resume(project_dir, fake)
        assert sorted(fake.implemented_subtasks) == sorted(_PENDING_SUBTASKS), fake.implemented_subtasks
        assert "[🔎 事前確認の結果: 3件中0件を実装済みのためスキップし、残り3件をキューに入れます]" in output, output
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_failing_verify_command_disables_skipping():
    """検証コマンドが通らない状態では、事前確認そのものを行わず全件実行する。"""
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root, verify_source="raise SystemExit(1)\n")
        fake = _FakeOrg(_SATISFIED_ANSWERS)
        output = _run_resume(project_dir, fake)
        assert fake.checked_subtasks == [], fake.checked_subtasks
        assert sorted(fake.implemented_subtasks) == sorted(_PENDING_SUBTASKS), fake.implemented_subtasks
        assert "事前確認は行いません(検証コマンド(python3 test_verify.py)が成功しませんでした)" in output, output
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_syntax_error_disables_skipping():
    root = tempfile.mkdtemp(prefix="yoriai_skip_test_")
    try:
        project_dir = _write_project_with_pending_queue(root, verify_command="")
        with open(os.path.join(project_dir, "broken.py"), "w", encoding="utf-8") as f:
            f.write("def broken(:\n")
        fake = _FakeOrg(_SATISFIED_ANSWERS)
        output = _run_resume(project_dir, fake)
        assert fake.checked_subtasks == [], fake.checked_subtasks
        assert "事前確認は行いません(構文エラーが残っているファイルがあります: broken.py)" in output, output
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"OK {name}")
    print(f"{len(tests)} tests passed")
