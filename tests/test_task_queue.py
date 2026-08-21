#!/usr/bin/env python3
"""協業モードの実装フェーズを「タスクの待ち行列(キュー)」として扱う
`_run_collaborative_task_queue`(および補助関数`_estimate_task_weight`)を
検証する。

以前は、合意フェーズが決めたファイル数がメンバー数を超える場合、
「メンバー数ぶんだけ実行し、残りは切り捨てる」実装だった(タスク
チェックリストの追加調査で見つかった不具合の温床でもあった)。
タスクを待ち行列として扱い、メンバーの実装+レビューが完了するたびに
次のタスクを割り当てる方式に変更したことで、メンバー数がタスク数より
少なくても最終的に全タスクが実装されるようにした。このファイルでは、
- 重さ(依頼文の長さ)の降順にキューへ並べること
- メンバーの手が空くたびに次のタスクが動的に再割り当てされること
  (依頼の動作確認シナリオである「2メンバー・4ファイル」を直接再現)
- レビュー担当が「自分以外の最初のメンバー」という固定ルールで選ばれること
- メンバーが1台しかいない場合はレビューがスキップされ、タスク
  チェックリスト上「未完了」のまま残ること
を検証する。実機のOllama/LM Studio/MLX-LMには接続できない環境のため、
`_stream_chat_from_candidate`を差し替えてメンバーを模擬する
(`_review_and_fix_one_file`などの下請け関数はすでに他のテストファイルで
個別に検証済みのため、ここでは差し替えない)。

使い方: python3 tests/test_task_queue.py
"""
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _candidate(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
    """実装依頼には空実装のコードを、レビュー依頼には常に「問題なし」を返す。"""
    text = messages[0]["content"]
    if "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def _run_queue(tasks, candidates, fake_stream=_fake_stream_ok):
    """テスト用に、合意フェーズ(_ask_organization_collaborate)を経由せず
    `_run_collaborative_task_queue`を直接呼び出すための共通処理。
    戻り値は (output_text, project_dir, checklist)。
    """
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream

    import contextlib
    import io

    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_task_queue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_task_queue(tasks, candidates, "fingerprint", out_dir, checklist)
        return buf.getvalue(), out_dir, checklist
    finally:
        yoriai._stream_chat_from_candidate = original_stream


def test_estimate_task_weight_is_content_length():
    """重さの推定は「依頼文の長さ」をそのまま使う簡易的な代理指標である
    ことを確認する(依頼「タスクの重さを簡易的に推定」への対応)。
    """
    assert yoriai._estimate_task_weight("") == 0
    assert yoriai._estimate_task_weight("短い依頼") == len("短い依頼")
    long_content = "storage.pyの永続化ロジックを実装。add_todo/list_todos/delete_todoを持つ。" * 3
    assert yoriai._estimate_task_weight(long_content) == len(long_content)
    assert yoriai._estimate_task_weight(long_content) > yoriai._estimate_task_weight("短い依頼")


def test_single_worker_processes_queue_in_descending_weight_order():
    """メンバーが1台の場合はスレッド間の競合が起きないため、キューの
    処理順序が「重さの降順」であることを確定的に検証できる。空きメモリの
    多いメンバーに重いタスクが優先的に割り当てられる、という依頼の要件は
    (`_select_chat_candidates`が候補を空きメモリ降順に並べていることと
    合わせて)キューの先頭が常に最も重いタスクであることに由来する。
    """
    tasks = [
        ("config.py", "設定値を1行返すだけ"),  # 最も軽い
        ("storage.py", "データの永続化を担当。add_todo/list_todos/delete_todoを実装し、JSON形式でファイルに保存する。並行アクセス時のロック処理も考慮する。"),  # 最も重い
        ("cli.py", "コマンドライン引数を解釈してstorageの関数を呼び出す。"),  # 中間
    ]
    candidate = _candidate("MacStudio(自分)", "qwen2.5-coder-32b")

    output, out_dir, checklist = _run_queue(tasks, [candidate])
    try:
        # メンバーが1台の場合、最初のタスクだけが「📋 ... を割り当てました」
        # 形式で、2件目以降は「🙌 ... 次のタスク{filename}を割り当てます」
        # 形式で表示される(is_firstがFalseになるため)。出現順を維持したまま
        # 両方の形式からファイル名を抽出する。
        assignment_order = re.findall(
            r"に (\S+) を割り当てました|次のタスク (\S+) を割り当てます", output,
        )
        assignment_order = [a or b for a, b in assignment_order]
        assert assignment_order == ["storage.py", "cli.py", "config.py"], (
            f"重い順(storage.py > cli.py > config.py)に処理されるはずです: {assignment_order}"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_two_members_four_tasks_queue_completes_all_files_via_reassignment():
    """依頼の動作確認シナリオ(実機と同じ2台構成で、storage.py・cli.py・
    utils.py・config.pyの4ファイル構成のToDo CLIを作る)を直接再現し、
    メンバー数(2台)がタスク数(4件)より少なくても、手が空いたメンバーに
    次のタスクが再割り当てされることで最終的に4ファイルすべてが実装・
    レビューされ、タスクチェックリストが「✅ 全タスク完了」になることを
    確認する。
    """
    tasks = [
        ("storage.py", "ToDoの永続化(JSON保存)を担当。add_todo/list_todos/delete_todoを実装する。"),
        ("cli.py", "コマンドライン操作を担当。argparseでadd/list/deleteサブコマンドを実装する。"),
        ("utils.py", "共通処理を担当。IDの生成関数generate_idを実装する。"),
        ("config.py", "設定値を担当。保存先ファイルパスの定数を定義する。"),
    ]
    candidates = [
        _candidate("MacStudio(自分)", "qwen2.5-coder-32b"),
        _candidate("junnoMac-mini", "qwen2.5-coder-14b"),
    ]

    output, out_dir, checklist = _run_queue(tasks, candidates)
    try:
        saved_files = set(os.listdir(out_dir))
        assert saved_files == {"storage.py", "cli.py", "utils.py", "config.py"}, (
            f"2台構成でも、キューにより4ファイルすべてが実装されるはずです: {saved_files}"
        )

        # メンバーは2台・タスクは4件なので、各メンバーは必ず2件ずつ処理する
        # ことになり、「最初の割り当て」(📋)は必ず2回、「手が空いての
        # 再割り当て」(🙌)も必ず2回になるはず(スレッドの実行順序に
        # よらず確定する不変条件)。
        first_assignments = output.count("[📋 タスクキュー:")
        reassignments = output.count("の手が空いたため、次のタスク")
        assert first_assignments == 2, f"最初の割り当ては2回のはずです: {output}"
        assert reassignments == 2, (
            f"手が空いた際の再割り当てが2回起きるはずです(タスクキュー方式が"
            f"機能していない可能性があります): {output}"
        )

        assert yoriai._incomplete_task_labels(checklist) == [], (
            f"全タスクが完了しているはずです: {checklist}"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_reviewer_is_first_other_candidate_following_fixed_rule():
    """レビュー担当は「自分以外の最初のメンバー」という固定ルールで選ばれる
    ことを確認する。実際にどのメンバーが実装を担当するかはスレッドの
    実行順序に左右されうるため、出力から実際の実装担当者を読み取り、
    ルールどおりのレビュー担当者が選ばれているかを照合する。
    """
    tasks = [("storage.py", "データの永続化を担当")]
    candidates = [
        _candidate("MacStudio(自分)", "qwen2.5-coder-32b"),
        _candidate("junnoMac-mini", "qwen2.5-coder-14b"),
        _candidate("raspi4", "qwen2.5-coder-7b"),
    ]

    output, out_dir, checklist = _run_queue(tasks, candidates)
    try:
        impl_match = re.search(r"\[📋 タスクキュー: (\S+) に storage.py を割り当てました", output)
        assert impl_match, output
        implementer_label = impl_match.group(1)

        expected_reviewer = next(c["label"] for c in candidates if c["label"] != implementer_label)

        review_match = re.search(r"(\S+) が storage\.py をレビューしています", output)
        assert review_match, f"レビューが行われていません: {output}"
        actual_reviewer_label = review_match.group(1)

        assert actual_reviewer_label == expected_reviewer, (
            f"レビュー担当は「自分以外の最初のメンバー」ルールに従うはずです "
            f"(実装: {implementer_label}, 期待するレビュー担当: {expected_reviewer}, "
            f"実際: {actual_reviewer_label})"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_single_candidate_skips_review_but_leaves_review_task_incomplete():
    """メンバーが1台しかいない場合、担当外のレビュー担当を選べないため、
    レビューはスキップされる(実装だけは行われる)。この場合、タスク
    チェックリスト上「レビュー」項目は未完了のまま残ることを確認する
    (最終チェックで正直に検出されるべきという既存のタスクチェック
    リスト機能の方針との整合性)。
    """
    tasks = [("storage.py", "データの永続化を担当")]
    candidate = _candidate("MacStudio(自分)", "qwen2.5-coder-32b")

    output, out_dir, checklist = _run_queue(tasks, [candidate])
    try:
        assert "レビュー担当者がいません" in output, output
        assert os.path.exists(os.path.join(out_dir, "storage.py")), "実装自体は行われるはずです"
        assert yoriai._incomplete_task_labels(checklist) == ["storage.py のレビュー"], checklist
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_estimate_task_weight_is_content_length,
        test_single_worker_processes_queue_in_descending_weight_order,
        test_two_members_four_tasks_queue_completes_all_files_via_reassignment,
        test_reviewer_is_first_other_candidate_following_fixed_rule,
        test_single_candidate_skips_review_but_leaves_review_task_incomplete,
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
