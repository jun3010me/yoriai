#!/usr/bin/env python3
"""対話的な修正セッション(`_FixSession`)を検証する。

これまでの`//fix`は常に単発の処理で、1回の依頼ごとに対象プロジェクトを
独立して特定し直していた。実際の運用では「まだ狭い」「今度は逆に」の
ように、コマンドを付けない自然な発言で少しずつ調整しながら何度も
`//fix`を打ち直す使い方が多く発生していたため、一度`//fix`で対象
プロジェクトを指定したら、その後はコマンド無しの発言も継続してその
プロジェクトへの修正依頼として扱う「修正セッション」を導入した。

(1)セッション開始時の状態(`_FixSession`)とその判定関数群、
(2)`_begin_fix_session`によるセッション開始・切り替え、
(3)`_run_repl_client`全体を通した、コマンド無しの継続的な修正依頼・
明示的な終了・新規制作依頼や新規`//agree`による自動終了・無関係な
話題が連続した場合の自動終了、を検証する。

使い方: python3 tests/test_fix_session.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"


def _write_completed_project(projects_root, name, tasks, request="ToDoリストのCLIツールを作って"):
    checklist = yoriai._build_task_checklist(tasks)
    for filename, _content in tasks:
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    project_dir = os.path.join(projects_root, name)
    yoriai._write_progress_md(project_dir, request, tasks, checklist, {})
    for filename, _content in tasks:
        with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as f:
            f.write("pass\n")
    return project_dir


# ---------------------------------------------------------------------------
# _FixSession・判定関数群
# ---------------------------------------------------------------------------

def test_is_fix_session_end_phrase_matches_short_exact_phrases():
    for phrase in ("終了", "セッション終了", "やめる", "おわり", "終了。", "終了!"):
        assert yoriai._is_fix_session_end_phrase(phrase), phrase


def test_is_fix_session_end_phrase_does_not_match_ordinary_requests_mentioning_the_word():
    """「終了」を含む通常の修正依頼まで誤って終了コマンド扱いしないことを
    確認する(依頼内容によっては"終了"という語が含まれうるため)。
    """
    assert not yoriai._is_fix_session_end_phrase("処理がまだ終了していません、直して")
    assert not yoriai._is_fix_session_end_phrase("まだ狭いよ")


def test_fix_session_touch_resets_unrelated_streak_and_updates_last_active():
    session = yoriai._FixSession("/tmp/some-project")
    session.unrelated_streak = 2
    old_last_active = session.last_active - 10
    session.last_active = old_last_active
    session.touch()
    assert session.unrelated_streak == 0
    assert session.last_active > old_last_active


def test_fix_session_is_idle_expired_true_after_timeout():
    session = yoriai._FixSession("/tmp/some-project")
    session.last_active = time.monotonic() - yoriai._FIX_SESSION_IDLE_TIMEOUT_SEC - 1
    assert session.is_idle_expired() is True


def test_fix_session_is_idle_expired_false_when_recently_active():
    session = yoriai._FixSession("/tmp/some-project")
    assert session.is_idle_expired() is False


def test_fix_session_project_name_is_the_directory_basename():
    session = yoriai._FixSession("/tmp/projects/todo-cli")
    assert session.project_name == "todo-cli"


# ---------------------------------------------------------------------------
# _begin_fix_session
# ---------------------------------------------------------------------------

class _FakeJobRunner:
    """`_begin_fix_session`が投入したジョブを実行せず記録するだけの
    フェイク(実際の修正の実行は別の関心事であり、ここではセッション
    状態の遷移だけを検証したいため)。
    """

    def __init__(self):
        self.submitted = []

    def submit(self, job, queued_notice=None):
        self.submitted.append(job)


def test_begin_fix_session_starts_a_new_session_on_first_resolution():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(projects_root, "todo-cli", [("utils.py", "IDの生成を担当")])

        runner = _FakeJobRunner()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fix_session = yoriai._begin_fix_session(
                47120, "fingerprint", out_dir, runner, None, "todo-cli: バグを直して",
            )
        output = buf.getvalue()

        assert fix_session is not None
        assert fix_session.project_dir == project_dir
        assert "[🔧 修正セッション中: todo-cli]" in output, output
        assert len(runner.submitted) == 1
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_begin_fix_session_keeps_the_same_session_object_for_the_same_project():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        runner = _FakeJobRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            first = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, None, "todo-cli: 直して")
            first.unrelated_streak = 2
            second = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, first, "todo-cli: もう一度直して")

        assert second is first, "同じプロジェクトへの再指定はセッションを再生成せず引き継ぐはずです"
        assert second.unrelated_streak == 0, "再指定はタッチ扱いとなり、無関係カウントはリセットされるはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_begin_fix_session_switches_and_announces_when_project_differs():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])
        weather_dir = _write_completed_project(
            projects_root, "weather-cli", [("api.py", "天気予報を取得するAPIクライアント")],
            request="天気予報を調べるCLIツールを作って",
        )

        runner = _FakeJobRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            first = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, None, "todo-cli: 直して")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            second = yoriai._begin_fix_session(
                47120, "fingerprint", out_dir, runner, first, "weather-cli: APIのURLを直して",
            )
        output = buf.getvalue()

        assert second is not first
        assert second.project_dir == weather_dir
        assert "終了しました" in output and "切り替え" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_begin_fix_session_reports_not_found_when_no_session_and_resolution_fails():
    """セッションが無い状態で、プロジェクト名の手がかりに乏しい依頼文が
    どの完成済みプロジェクトとも一致しなかった場合は、これまで通り
    「見つかりませんでした」と報告し、セッションは開始されないことを
    確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        runner = _FakeJobRunner()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, None, "xyzzy plugh quuxを直して")
        output = buf.getvalue()

        assert result is None, "特定に失敗した場合、セッションは開始されないはずです"
        assert "見つかりませんでした" in output, output
        assert len(runner.submitted) == 0, "特定に失敗した依頼はジョブとして投入されないはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_begin_fix_session_treats_prefixless_fix_command_as_continuation_when_session_active():
    """気づきへの対応の回帰テスト: セッションが有効な間に、プロジェクト名を
    省略した`//fix <依頼>`(手がかりに乏しくバイグラム照合では本来
    見つからないはずの依頼文)が送られても、識別処理へは回さず、
    コマンド無しの発言と同様に現在のセッション対象への継続修正として
    扱われることを確認する(以前は"対象となるプロジェクトが見つかりません
    でした"と誤って報告されていた)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        runner = _FakeJobRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            first = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, None, "todo-cli: 直して")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, first, "まだ狭いって")
        output = buf.getvalue()

        assert result is first, "同じセッションがそのまま継続されるはずです"
        assert "見つかりませんでした" not in output, output
        assert "[🔧 修正セッション中: todo-cli]" in output, output
        assert len(runner.submitted) == 2, "継続依頼もジョブとして投入されるはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_begin_fix_session_leaves_existing_session_untouched_when_explicit_target_is_incomplete():
    """プロジェクト名を明示指定した`//fix`で、その対象が未完了だった場合は
    従来通りエラーを報告し、既存のセッションは変化しないことを確認する
    (明示指定がある限り、セッション中でも通常の解決処理に委ねられる)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])
        incomplete_checklist = yoriai._build_task_checklist([("api.py", "説明")])
        yoriai._write_progress_md(
            os.path.join(projects_root, "weather-cli"), "天気予報アプリを作って",
            [("api.py", "説明")], incomplete_checklist, {},
        )

        runner = _FakeJobRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            first = yoriai._begin_fix_session(47120, "fingerprint", out_dir, runner, None, "todo-cli: 直して")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = yoriai._begin_fix_session(
                47120, "fingerprint", out_dir, runner, first, "weather-cli: URLを直して",
            )
        output = buf.getvalue()

        assert result is first, "特定に失敗した場合、既存のセッションは維持されるはずです"
        assert "未完了のタスクが残っています" in output, output
        assert len(runner.submitted) == 1, "特定に失敗した依頼はジョブとして投入されないはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# `_run_repl_client`全体を通した統合テスト
# ---------------------------------------------------------------------------

def _run_repl_with_keys(keystrokes, out_dir, run_fix_side_effect=None,
                         ask_collaborate_side_effect=None, ask_single_side_effect=None):
    calls = {"run_fix": 0, "ask_collaborate": 0, "ask_multi": 0, "ask_single": 0}
    fix_calls = []

    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_run_fix = yoriai._run_fix_on_project
    original_ask = yoriai._ask_organization
    original_ask_multi = yoriai._ask_organization_multi
    original_ask_collaborate = yoriai._ask_organization_collaborate

    def stub_run_fix(port, org_fingerprint, project_dir, request, out_dir_arg):
        calls["run_fix"] += 1
        fix_calls.append((project_dir, request))
        if run_fix_side_effect:
            run_fix_side_effect()

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1
        if ask_single_side_effect:
            ask_single_side_effect()

    def stub_ask_multi(*args, **kwargs):
        calls["ask_multi"] += 1

    def stub_ask_collaborate(*args, **kwargs):
        calls["ask_collaborate"] += 1
        if ask_collaborate_side_effect:
            ask_collaborate_side_effect()

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    with create_pipe_input() as pipe_input:
        def fake_create_session():
            return PromptSession(
                input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
            )

        yoriai._create_repl_prompt_session = fake_create_session
        yoriai._create_background_job_runner = fake_create_runner
        yoriai._run_fix_on_project = stub_run_fix
        yoriai._ask_organization = stub_ask
        yoriai._ask_organization_multi = stub_ask_multi
        yoriai._ask_organization_collaborate = stub_ask_collaborate

        pipe_input.send_text(keystrokes)

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", out_dir)
        finally:
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._create_background_job_runner = original_create_runner

    runner = runner_holder.get("runner")
    if runner is not None:
        runner.join()

    yoriai._run_fix_on_project = original_run_fix
    yoriai._ask_organization = original_ask
    yoriai._ask_organization_multi = original_ask_multi
    yoriai._ask_organization_collaborate = original_ask_collaborate

    return buf.getvalue(), calls, fix_calls


def test_fix_command_then_plain_followup_continues_the_same_project():
    """`//fix`で対象を指定した後、コマンド無しの(手がかりに乏しい)発言
    「まだ狭いよ」が、正しく同じプロジェクトへの継続的な修正依頼として
    扱われることを確認する(依頼の動作確認の中心)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-css-html-css", [("editor.css", "説明")],
        )

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} html-css-html-css: テキストエリアが狭いので広くして" + _SUBMIT
            + "まだ狭いよ" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 2, calls
        assert fix_calls[0][0] == project_dir and "広くして" in fix_calls[0][1]
        assert fix_calls[1][0] == project_dir and fix_calls[1][1] == "まだ狭いよ", fix_calls
        assert output.count("[🔧 修正セッション中: html-css-html-css]") == 2, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_fix_command_without_project_name_continues_the_session_like_a_plain_followup():
    """気づきへの対応の回帰テスト(`_run_repl_client`全体を通した検証):
    セッション中に、コマンド無しの発言だけでなく`//fix <依頼>`
    (プロジェクト名を省略した形式)で発言しても、同様に現在のセッション
    対象プロジェクトへの継続修正として扱われ、「対象となるプロジェクトが
    見つかりませんでした」というエラーにならないことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "html-css-html-css", [("editor.css", "説明")],
        )

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} html-css-html-css: テキストエリアが狭いので広くして" + _SUBMIT
            + f"{yoriai.FIX_PROJECT_COMMAND} まだ狭いって" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 2, calls
        assert fix_calls[1] == (project_dir, "まだ狭いって"), fix_calls
        assert "見つかりませんでした" not in output, output
        assert output.count("[🔧 修正セッション中: html-css-html-css]") == 2, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_explicit_end_phrase_ends_the_session_without_triggering_a_fix():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + "終了" + _SUBMIT
            + "まだ狭いよ" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        # 1回目(//fix)のみ修正ジョブが投入され、"終了"後の発言はセッションが
        # 無い状態の通常の単発質問として扱われる(修正ジョブは増えない)。
        assert calls["run_fix"] == 1, calls
        assert calls["ask_single"] == 1, calls
        assert "修正セッションを終了しました" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_new_agree_ends_the_active_fix_session():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + f"{yoriai.AGREE_COMMAND} 天気予報アプリを作って" + _SUBMIT
            + "まだ狭いよ" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 1, calls
        assert calls["ask_collaborate"] == 1, calls
        assert calls["ask_single"] == 1, "//agree後の発言はセッションが終了しているため単発質問になるはずです"
        assert f"新規の{yoriai.AGREE_COMMAND}" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_natural_production_request_ends_the_active_fix_session():
    """セッション中に、コマンド無しでも明らかに新規制作依頼と読み取れる
    発言(協業モードのキーワードを含む)があった場合、セッションを終了し
    通常の新規制作依頼として扱うことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + "全然別の天気予報アプリを作って" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 1, calls
        assert calls["ask_collaborate"] == 1, calls
        assert "別の新規制作依頼" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_switching_to_another_project_via_fix_ends_the_previous_session():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])
        weather_dir = _write_completed_project(
            projects_root, "weather-cli", [("api.py", "天気予報を取得するAPIクライアント")],
            request="天気予報を調べるCLIツールを作って",
        )

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + f"{yoriai.FIX_PROJECT_COMMAND} weather-cli: URLを直して" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 2, calls
        assert fix_calls[1][0] == weather_dir
        assert "切り替え" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_unrelated_topics_streak_ends_the_session_after_the_limit():
    """比較モード相当の「関係ない話題」がしきい値回数連続すると、
    自動的にセッションが終了することを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        unrelated_turns = "".join(
            "みんなの意見を聞かせて" + _SUBMIT for _ in range(yoriai._FIX_SESSION_UNRELATED_STREAK_LIMIT)
        )
        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + unrelated_turns
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 1, calls
        assert calls["ask_multi"] == yoriai._FIX_SESSION_UNRELATED_STREAK_LIMIT, calls
        assert "関係ない話題が続いた" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_a_single_unrelated_topic_does_not_end_the_session():
    """1回だけの脱線ではセッションを終了せず、その後の発言はまだ継続的な
    修正依頼として扱われることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        keys = (
            f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
            + "みんなの意見を聞かせて" + _SUBMIT
            + "今度は逆にして" + _SUBMIT
            + "exit" + _SUBMIT
        )
        output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)

        assert calls["run_fix"] == 2, calls
        assert calls["ask_multi"] == 1, calls
        assert fix_calls[1] == (project_dir, "今度は逆にして"), fix_calls
        assert "関係ない話題が続いた" not in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_idle_timeout_ends_the_session_before_processing_the_next_turn():
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_session_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        _write_completed_project(projects_root, "todo-cli", [("utils.py", "説明")])

        original_timeout = yoriai._FIX_SESSION_IDLE_TIMEOUT_SEC
        yoriai._FIX_SESSION_IDLE_TIMEOUT_SEC = 0
        try:
            keys = (
                f"{yoriai.FIX_PROJECT_COMMAND} todo-cli: バグを直して" + _SUBMIT
                + "まだ狭いよ" + _SUBMIT
                + "exit" + _SUBMIT
            )
            output, calls, fix_calls = _run_repl_with_keys(keys, out_dir)
        finally:
            yoriai._FIX_SESSION_IDLE_TIMEOUT_SEC = original_timeout

        assert calls["run_fix"] == 1, "アイドルタイムアウト後の発言は修正継続として扱われないはずです"
        assert calls["ask_single"] == 1, calls
        assert "操作がしばらく無かったため" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_is_fix_session_end_phrase_matches_short_exact_phrases,
        test_is_fix_session_end_phrase_does_not_match_ordinary_requests_mentioning_the_word,
        test_fix_session_touch_resets_unrelated_streak_and_updates_last_active,
        test_fix_session_is_idle_expired_true_after_timeout,
        test_fix_session_is_idle_expired_false_when_recently_active,
        test_fix_session_project_name_is_the_directory_basename,
        test_begin_fix_session_starts_a_new_session_on_first_resolution,
        test_begin_fix_session_keeps_the_same_session_object_for_the_same_project,
        test_begin_fix_session_switches_and_announces_when_project_differs,
        test_begin_fix_session_reports_not_found_when_no_session_and_resolution_fails,
        test_begin_fix_session_treats_prefixless_fix_command_as_continuation_when_session_active,
        test_begin_fix_session_leaves_existing_session_untouched_when_explicit_target_is_incomplete,
        test_fix_command_then_plain_followup_continues_the_same_project,
        test_fix_command_without_project_name_continues_the_session_like_a_plain_followup,
        test_explicit_end_phrase_ends_the_session_without_triggering_a_fix,
        test_new_agree_ends_the_active_fix_session,
        test_natural_production_request_ends_the_active_fix_session,
        test_switching_to_another_project_via_fix_ends_the_previous_session,
        test_unrelated_topics_streak_ends_the_session_after_the_limit,
        test_a_single_unrelated_topic_does_not_end_the_session,
        test_idle_timeout_ends_the_session_before_processing_the_next_turn,
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
