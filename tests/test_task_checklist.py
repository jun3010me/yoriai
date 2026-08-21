#!/usr/bin/env python3
"""協業モードの「組織自身が立てた計画を最後まで完遂させるタスクチェック
リスト」を検証する。

実機で、合意フェーズが4ファイル構成(storage.py・cli.py・utils.py・
config.py)を自ら提案したにもかかわらず、実装フェーズで候補不足により
config.pyがスキップされたまま「✅ レビュー完了」と表示されてしまう不具合が
報告された。これを受けて、合意フェーズが確定した瞬間にその計画を「必ず
完遂すべきタスクリスト」として固定し、実装・レビューそれぞれのフェーズが
「自分の担当範囲では完了した」と判断していても、計画全体で見て未完了の
項目が残っていれば、最終的に正直にそれを報告する仕組みを検証する。

使い方: python3 tests/test_task_checklist.py
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
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _snapshot_with_n_members(n):
    names = ["MacStudio", "junnoMac-mini", "raspi4", "MacBookPro"]
    self_card = _make_card(names[0], "qwen2.5-coder-32b", free_gb=40)
    peers = []
    for i in range(1, n):
        peers.append({
            "card": _make_card(names[i], f"qwen2.5-coder-{14 - i}b", free_gb=20 - i),
            "address": "127.0.0.1", "port": 47120 + i, "via": "mdns", "last_seen": 0,
        })
    return {"self": self_card, "peers": peers}


_FOUR_FILE_BREAKDOWN_ANSWER = (
    "storage.py: データの永続化を担当\n"
    "cli.py: コマンドライン操作を担当\n"
    "utils.py: 共通のユーティリティ関数を担当\n"
    "config.py: 設定値の管理を担当\n"
)


def _fake_stream_ok(candidate, org_fingerprint, messages):
    request_text = messages[0]["content"]
    if "ファイルに分割する実装計画" in request_text:
        yield {"content": _FOUR_FILE_BREAKDOWN_ANSWER}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_queue_ensures_all_tasks_complete_even_with_fewer_members_than_tasks():
    """タスクキュー方式導入前は、4ファイル構成の計画に対して候補が3台しか
    無い場合、config.pyが実装フェーズで実際にスキップされたまま
    「✅ レビュー完了」と表示されてしまう不具合が実機で報告されていた
    (このテストは元々その不具合の再現用だった)。タスクキュー方式導入後は、
    手が空いたメンバーに次のタスクが割り当てられるため、候補が3台しか
    無くても最終的に4ファイルすべてが実装・レビューされ、タスク
    チェックリストが正しく「✅ 全タスク完了」になることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _snapshot_with_n_members(3)
    yoriai._stream_chat_from_candidate = _fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_checklist_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "4つのモジュールに分かれたツールを作って", out_dir)
        output = buf.getvalue()

        # タスクキュー方式により、3台のメンバーしかいなくても4ファイル
        # すべてが最終的に実装されるはず(切り捨てが起きない)。
        project_dir = os.path.join(out_dir, "projects", yoriai._generate_project_name("4つのモジュールに分かれたツールを作って"))
        saved_files = set(os.listdir(project_dir)) if os.path.isdir(project_dir) else set()
        assert saved_files == {"storage.py", "cli.py", "utils.py", "config.py"}, (
            f"候補が3台でも、キューにより4ファイルすべてが実装されるはずです: {saved_files}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert "⚠️ 未完了のタスクが残っています" not in output, output
    assert "[✅ 全タスク完了" in output, output


def test_complete_plan_reports_all_tasks_done():
    """依頼の動作確認(正常系): 4ファイル構成の依頼に対して、4台の候補が
    揃っている場合は、全タスクが完了し「✅ 全タスク完了」と表示される
    ことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _snapshot_with_n_members(4)
    yoriai._stream_chat_from_candidate = _fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_checklist_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "4つのモジュールに分かれたツールを作って", out_dir)
        output = buf.getvalue()

        project_dir = os.path.join(out_dir, "projects", yoriai._generate_project_name("4つのモジュールに分かれたツールを作って"))
        assert set(os.listdir(project_dir)) == {"storage.py", "cli.py", "utils.py", "config.py"}
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert "⚠️ 未完了のタスクが残っています" not in output, output
    assert "[✅ 全タスク完了" in output, output
    # 最終チェックリストが全項目✅で表示されていること。
    final_checklist = output.rsplit("[📝 タスク状況(組織が自ら立てた計画)]", 1)[-1]
    assert "⬜" not in final_checklist.split("[✅ 全タスク完了")[0], (
        f"最終チェックリストに未完了マークが残っています: {output}"
    )


def test_checklist_shown_at_each_phase_transition():
    """タスクリストの状態が、合意フェーズ直後(初期状態)と最終確認時に
    画面表示されることに加え、タスクキュー方式導入後は実装フェーズ中の
    進捗も(バッチでの全件再表示ではなく)タスクが割り当てられるたびに
    逐次表示されることを確認する(依頼の「都度表示」要件)。

    タスクキュー方式導入前は「実装フェーズ完了後」にチェックリスト全体を
    再表示していたため、このテストは表示回数が3回以上であることを
    確認していた。導入後は、実装フェーズ中の進捗はチェックリストの
    バッチ再表示ではなく[📋 タスクキュー: ...]/[🙌 ...の手が空いたため...]
    という個別のタスク割り当てメッセージで都度表示されるようになった
    ため、チェックリスト全体の表示回数は2回(初期状態・最終確認)になる。

    メンバーごとに1スレッドの並行処理のため、「誰の何番目の割り当てか」
    (📋 = そのメンバーにとって最初のタスク / 🙌 = 手が空いて次のタスクを
    受け取った)はスレッドのスケジューリングにより実行のたびに変わりうる。
    そのため両メッセージの内訳ではなく、合計件数(=タスク総数)が固定である
    ことだけを検証する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _snapshot_with_n_members(4)
    yoriai._stream_chat_from_candidate = _fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_checklist_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "4つのモジュールに分かれたツールを作って", out_dir)
        output = buf.getvalue()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    # 少なくとも2回(初期状態・最終確認)はチェックリスト全体が表示されるはず。
    assert output.count("[📝 タスク状況(組織が自ら立てた計画)]") >= 2, output
    # 実装フェーズ中は、タスクが割り当てられるたびに個別メッセージ
    # (📋 または 🙌)で進捗が都度表示されるはず(合計でタスク総数=4件ぶん)。
    assignment_messages = output.count("[📋 タスクキュー:") + output.count("の手が空いたため、次のタスク")
    assert assignment_messages == 4, output


def test_build_task_checklist_creates_two_tasks_per_file():
    tasks = [("storage.py", "..."), ("cli.py", "...")]
    checklist = yoriai._build_task_checklist(tasks)
    assert len(checklist) == 4, checklist
    labels = [t["label"] for t in checklist]
    assert labels == ["storage.py の実装", "storage.py のレビュー", "cli.py の実装", "cli.py のレビュー"], labels
    assert all(t["status"] == yoriai._TASK_STATUS_PENDING for t in checklist)


def test_incomplete_task_labels_only_returns_non_completed():
    tasks = [("storage.py", "..."), ("cli.py", "...")]
    checklist = yoriai._build_task_checklist(tasks)
    yoriai._set_task_status(checklist, "storage.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "storage.py", "review", yoriai._TASK_STATUS_COMPLETED)
    yoriai._set_task_status(checklist, "cli.py", "impl", yoriai._TASK_STATUS_COMPLETED)
    # cli.pyのレビューだけ未完了のまま。
    incomplete = yoriai._incomplete_task_labels(checklist)
    assert incomplete == ["cli.py のレビュー"], incomplete


def main():
    tests = [
        test_build_task_checklist_creates_two_tasks_per_file,
        test_incomplete_task_labels_only_returns_non_completed,
        test_queue_ensures_all_tasks_complete_even_with_fewer_members_than_tasks,
        test_complete_plan_reports_all_tasks_done,
        test_checklist_shown_at_each_phase_transition,
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
