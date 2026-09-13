#!/usr/bin/env python3
"""Gap批評エージェント(成長するバックログ、収束判定つき)を検証する。

背景: 一通り完成した後、「もっと良くするには何が必要か」を問い直す振り
返り工程が存在しなかった。実装完了ごとに「Gap批評エージェント」を起動し、
実際にできあがった成果物を見た上で改善提案を洗い出す。「もっと」を無限に
続けさせないため、固定回数ではなく収束判定(新規性のある指摘が出なく
なったら終了)を基準にし、収束しなかった場合の保険として固定回数の上限も
別途持たせる。

重要: この工程はタスク単位の実行検証グラウンディングループ(PR1、
`_run_task_grounding_verification`、正しく動くかの判定)とは目的が異なる。
同じエージェント人格・同じプロンプトに両方をやらせないよう、プロンプト
テンプレート・関数を完全に分離している。

ここでは(1)新規性のある指摘が連続でN回出なくなった場合に収束と判定
されること、(2)収束前に固定回数の上限に達した場合に安全に終了する
こと、(3)Gap批評エージェントと実行検証エージェント(PR1)が別プロンプト・
別役割として分離されていることを確認する(同一プロンプトへの混入を
防ぐ意図)、を中心に、周辺の補助ロジック(指摘のタグ付き抽出・重複除外・
外部比較の間引き・成長するバックログの永続化)も検証する。

実機のOllama/LM Studio/MLX-LMやSearXNGインスタンスに依存しないよう、
`_collect_answer_from_candidate`・`web_search`・`_run_fix_task_queue`を
差し替えて模擬する。

使い方: python3 tests/test_gap_critique.py
"""
import inspect
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def _patched(obj, name, replacement):
    original = getattr(obj, name)
    setattr(obj, name, replacement)
    return original


# ---------------------------------------------------------------------------
# _parse_gap_critique_suggestions / _filter_novel_gap_suggestions
# ---------------------------------------------------------------------------

def test_parse_gap_critique_suggestions_extracts_tagged_lines():
    text = (
        "- [価値あり] エラー時にリトライする仕組みを追加する\n"
        "- [限界的] ログのフォーマットを統一する\n"
        "この行はタグが無いので無視されるはずです\n"
        "- [価値あり] 設定ファイルを外出しする\n"
    )
    suggestions = yoriai._parse_gap_critique_suggestions(text)
    assert suggestions == [
        ("価値あり", "エラー時にリトライする仕組みを追加する"),
        ("限界的", "ログのフォーマットを統一する"),
        ("価値あり", "設定ファイルを外出しする"),
    ], suggestions


def test_parse_gap_critique_suggestions_empty_when_no_more_suggestions():
    assert yoriai._parse_gap_critique_suggestions("追加の指摘はありません") == []
    assert yoriai._parse_gap_critique_suggestions("") == []


def test_filter_novel_gap_suggestions_excludes_known_texts_ignoring_whitespace():
    suggestions = [
        ("価値あり", "エラー時にリトライする"),
        ("価値あり", "  エラー時に   リトライする  "),  # 空白違いのみの重複
        ("限界的", "新しい指摘"),
    ]
    known = {yoriai._normalize_gap_suggestion_text("エラー時にリトライする")}
    novel = yoriai._filter_novel_gap_suggestions(suggestions, known)
    assert novel == [("限界的", "新しい指摘")], novel


# ---------------------------------------------------------------------------
# _run_gap_critique_loop: 収束判定(新規性のある指摘が出なくなったら終了)
# ---------------------------------------------------------------------------

def test_converges_after_novelty_free_streak_reaches_threshold():
    """新規性のある指摘が連続で2回(既定の収束しきい値)出なくなった時点で
    収束と判定されることを確認する。
    """
    answers = [
        "- [価値あり] エラーハンドリングを追加する",  # 1回目: 新規あり
        "追加の指摘はありません",  # 2回目: 新規なし(streak=1)
        "追加の指摘はありません",  # 3回目: 新規なし(streak=2) → 収束
        "- [価値あり] ここには到達しないはずの指摘",  # 4回目: 呼ばれてはいけない
    ]
    calls = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        calls.append(1)
        return answers[len(calls) - 1], None, False

    original = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        result = yoriai._run_gap_critique_loop(
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "ToDoアプリを作って", "成果物の中身",
        )
    finally:
        yoriai._collect_answer_from_candidate = original

    assert result["converged"] is True, result
    assert result["cycles"] == 3, result
    assert result["valuable_tasks"] == ["エラーハンドリングを追加する"], result
    assert len(calls) == 3, "収束後は問い合わせを続けてはいけません"


def test_converges_with_custom_streak_threshold():
    answers = ["追加の指摘はありません"] * 10

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        return answers.pop(0), None, False

    original = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        result = yoriai._run_gap_critique_loop(
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "依頼", "成果物", convergence_streak=3,
        )
    finally:
        yoriai._collect_answer_from_candidate = original

    assert result["converged"] is True
    assert result["cycles"] == 3, result


# ---------------------------------------------------------------------------
# _run_gap_critique_loop: 固定回数の上限(収束しなかった場合の保険)
# ---------------------------------------------------------------------------

def test_stops_safely_at_fixed_cycle_limit_when_never_converging():
    """毎回新しい(重複しない)指摘が出続け、収束条件を満たさない場合でも、
    固定回数の上限(`max_cycles`)で安全に打ち切られることを確認する
    (暴走防止)。
    """
    call_count = [0]

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        call_count[0] += 1
        return f"- [限界的] 提案{call_count[0]}", None, False

    original = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        result = yoriai._run_gap_critique_loop(
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "依頼", "成果物", max_cycles=5,
        )
    finally:
        yoriai._collect_answer_from_candidate = original

    assert result["converged"] is False, result
    assert result["cycles"] == 5, result
    assert call_count[0] == 5, "上限回数を超えて問い合わせを続けてはいけません"
    assert len(result["marginal_notes"]) == 5, result


def test_default_max_cycles_matches_runaway_prevention_safety_net():
    assert yoriai.MAX_GAP_CRITIQUE_CYCLES == 5


def test_stops_when_query_fails_without_infinite_retry():
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        return "", "接続に失敗しました", False

    original = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        result = yoriai._run_gap_critique_loop(
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "依頼", "成果物",
        )
    finally:
        yoriai._collect_answer_from_candidate = original

    assert result["converged"] is False
    assert result["cycles"] == 1
    assert result["valuable_tasks"] == []
    assert result["marginal_notes"] == []


def test_initial_known_suggestions_prevent_repeating_backlog_items():
    """既存のバックログ・過去の批評結果に既に含まれる指摘と実質的に同じ
    ものは、新規性なしとして扱われることを確認する。
    """
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        return "- [価値あり] エラーハンドリングを追加する", None, False

    original = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        result = yoriai._run_gap_critique_loop(
            _member("MacStudio", "qwen3-coder-30b"), "org-fp", "依頼", "成果物",
            initial_known_suggestions=["エラーハンドリングを追加する"], convergence_streak=1,
        )
    finally:
        yoriai._collect_answer_from_candidate = original

    assert result["converged"] is True, result
    assert result["valuable_tasks"] == [], result


# ---------------------------------------------------------------------------
# Gap批評エージェントと実行検証エージェント(PR1)の分離
# ---------------------------------------------------------------------------

def test_gap_critique_and_task_grounding_use_separate_prompt_templates():
    assert yoriai._GAP_CRITIQUE_PROMPT_TEMPLATE != yoriai._TASK_GROUNDING_FIX_PROMPT_TEMPLATE


def test_gap_critique_prompt_explicitly_disclaims_execution_verification():
    prompt = yoriai._build_gap_critique_prompt("依頼", "成果物", "", [])
    assert "動くかどうか" in prompt
    assert "検証することではありません" in prompt
    assert "もっと良くする" in prompt


def test_task_grounding_fix_prompt_has_no_gap_critique_vocabulary():
    run_result = {"ok": False, "returncode": 1, "output": "boom"}
    prompt = yoriai._build_task_grounding_fix_prompt("file.py", "plan", "pytest file.py", run_result)
    assert "もっと良くする" not in prompt
    assert "価値あり" not in prompt
    assert "限界的" not in prompt
    # 「テスト」は「実際に実行(テスト)したところ」という自然な言い回しの
    # 一部として登場するため対象外とする(Vision agentの観点タグとしての
    # 「テスト」ではない)。それ以外の観点タグは一切登場しないはずである。
    for category in yoriai._VISION_AGENT_REQUIRED_CATEGORIES:
        if category == "テスト":
            continue
        assert category not in prompt


def test_gap_critique_loop_source_never_calls_task_grounding_functions():
    source = inspect.getsource(yoriai._run_gap_critique_loop)
    assert "_run_task_grounding_verification" not in source
    assert "_run_project_command" not in source


def test_task_grounding_verification_source_never_calls_gap_critique_functions():
    source = inspect.getsource(yoriai._run_task_grounding_verification)
    assert "_run_gap_critique_loop" not in source
    assert "_build_gap_critique_prompt" not in source


# ---------------------------------------------------------------------------
# 外部比較の間引き(依頼の「実装N件ごとに1回」)
# ---------------------------------------------------------------------------

def test_refreshes_external_reference_every_time_without_prior_research():
    for count in range(1, 6):
        assert yoriai._gap_critique_should_refresh_external_reference(count, has_prior_research=False) is True


def test_thins_out_external_reference_refresh_when_prior_research_exists():
    decisions = [
        yoriai._gap_critique_should_refresh_external_reference(n, has_prior_research=True, refresh_interval=3)
        for n in range(1, 7)
    ]
    assert decisions == [False, False, True, False, False, True], decisions


# ---------------------------------------------------------------------------
# 成長するバックログ(BACKLOG.md)の永続化
# ---------------------------------------------------------------------------

def test_write_and_read_gap_backlog_round_trip():
    project_dir = tempfile.mkdtemp(prefix="yoriai_gap_backlog_test_")
    try:
        result = {
            "converged": True, "cycles": 2,
            "valuable_tasks": ["エラーハンドリングを追加する"],
            "marginal_notes": ["ログの粒度を細かくする"],
        }
        yoriai._write_gap_backlog_md(project_dir, result)
        backlog = yoriai._read_gap_backlog(project_dir)
        assert backlog == {
            "valuable": ["エラーハンドリングを追加する"],
            "marginal": ["ログの粒度を細かくする"],
        }, backlog
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_gap_backlog_grows_across_multiple_writes():
    """依頼の「成長するバックログ」への対応: 2回目の書き込みで、1回目の
    内容が消えず追記されることを確認する。
    """
    project_dir = tempfile.mkdtemp(prefix="yoriai_gap_backlog_growth_test_")
    try:
        yoriai._write_gap_backlog_md(project_dir, {
            "converged": True, "cycles": 1, "valuable_tasks": ["最初の指摘"], "marginal_notes": [],
        })
        yoriai._write_gap_backlog_md(project_dir, {
            "converged": True, "cycles": 1, "valuable_tasks": ["2回目の指摘"], "marginal_notes": ["限界的な指摘"],
        })
        backlog = yoriai._read_gap_backlog(project_dir)
        assert backlog["valuable"] == ["最初の指摘", "2回目の指摘"], backlog
        assert backlog["marginal"] == ["限界的な指摘"], backlog
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_read_gap_backlog_empty_when_file_absent():
    project_dir = tempfile.mkdtemp(prefix="yoriai_gap_backlog_absent_test_")
    try:
        assert yoriai._read_gap_backlog(project_dir) == {"valuable": [], "marginal": []}
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _run_gap_critique_phase: 価値ありの指摘を実装エージェントに戻す
# ---------------------------------------------------------------------------

def test_run_gap_critique_phase_forwards_valuable_tasks_to_fix_task_queue():
    project_dir = tempfile.mkdtemp(prefix="yoriai_gap_phase_test_")
    try:
        with open(os.path.join(project_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        answers = ["- [価値あり] リトライ処理を追加する", "追加の指摘はありません", "追加の指摘はありません"]

        def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
            return answers.pop(0), None, False

        fix_calls = []

        def fake_run_fix_task_queue(subtasks, candidates, org_fingerprint, project_dir_arg, request, parsed, full_plan, file_list, language):
            fix_calls.append(subtasks)

        original_collect = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
        original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
        original_fix_queue = _patched(yoriai, "_run_fix_task_queue", fake_run_fix_task_queue)
        try:
            result = yoriai._run_gap_critique_phase(
                "何か作って", [("main.py", "エントリポイント")],
                [_member("MacStudio", "qwen3-coder-30b")], "org-fp", project_dir,
            )
        finally:
            yoriai._collect_answer_from_candidate = original_collect
            yoriai.web_search = original_search
            yoriai._run_fix_task_queue = original_fix_queue

        assert result["valuable_tasks"] == ["リトライ処理を追加する"], result
        assert fix_calls == [["リトライ処理を追加する"]], fix_calls
        backlog = yoriai._read_gap_backlog(project_dir)
        assert backlog["valuable"] == ["リトライ処理を追加する"], backlog
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def test_run_gap_critique_phase_does_not_call_fix_queue_when_nothing_valuable():
    project_dir = tempfile.mkdtemp(prefix="yoriai_gap_phase_marginal_test_")
    try:
        with open(os.path.join(project_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        answers = ["- [限界的] ログを整える", "追加の指摘はありません", "追加の指摘はありません"]

        def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
            return answers.pop(0), None, False

        fix_calls = []

        def fake_run_fix_task_queue(*args, **kwargs):
            fix_calls.append(1)

        original_collect = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
        original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
        original_fix_queue = _patched(yoriai, "_run_fix_task_queue", fake_run_fix_task_queue)
        try:
            result = yoriai._run_gap_critique_phase(
                "何か作って", [("main.py", "エントリポイント")],
                [_member("MacStudio", "qwen3-coder-30b")], "org-fp", project_dir,
            )
        finally:
            yoriai._collect_answer_from_candidate = original_collect
            yoriai.web_search = original_search
            yoriai._run_fix_task_queue = original_fix_queue

        assert result["valuable_tasks"] == []
        assert fix_calls == [], "価値ありの指摘が無い場合、実装エージェントに戻してはいけません"
        backlog = yoriai._read_gap_backlog(project_dir)
        assert backlog["marginal"] == ["ログを整える"], backlog
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)


def main():
    tests = [
        test_parse_gap_critique_suggestions_extracts_tagged_lines,
        test_parse_gap_critique_suggestions_empty_when_no_more_suggestions,
        test_filter_novel_gap_suggestions_excludes_known_texts_ignoring_whitespace,
        test_converges_after_novelty_free_streak_reaches_threshold,
        test_converges_with_custom_streak_threshold,
        test_stops_safely_at_fixed_cycle_limit_when_never_converging,
        test_default_max_cycles_matches_runaway_prevention_safety_net,
        test_stops_when_query_fails_without_infinite_retry,
        test_initial_known_suggestions_prevent_repeating_backlog_items,
        test_gap_critique_and_task_grounding_use_separate_prompt_templates,
        test_gap_critique_prompt_explicitly_disclaims_execution_verification,
        test_task_grounding_fix_prompt_has_no_gap_critique_vocabulary,
        test_gap_critique_loop_source_never_calls_task_grounding_functions,
        test_task_grounding_verification_source_never_calls_gap_critique_functions,
        test_refreshes_external_reference_every_time_without_prior_research,
        test_thins_out_external_reference_refresh_when_prior_research_exists,
        test_write_and_read_gap_backlog_round_trip,
        test_gap_backlog_grows_across_multiple_writes,
        test_read_gap_backlog_empty_when_file_absent,
        test_run_gap_critique_phase_forwards_valuable_tasks_to_fix_task_queue,
        test_run_gap_critique_phase_does_not_call_fix_queue_when_nothing_valuable,
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
