#!/usr/bin/env python3
"""対話モードの時間のかかる処理(協業モード・`//parallel`・
`//resume-all`)を、入力ループをブロックしないバックグラウンドスレッド
(`_BackgroundJobRunner`)で実行する仕組みを検証する。

以前は、協業モード(合意フェーズ→タスクキュー方式による実装・レビュー)は
対話モードのメインスレッドで同期的に実行されており、完了するまで次の
依頼を入力できなかった。実機では数分単位の時間がかかることもあり、
Claude Code等のツールのように「処理はバックグラウンドで進みつつ、
いつでも次の入力を受け付けられる」体験に変更した。

単発質問(`_ask_organization`)・比較質問(`//multi`)は、対話モードの
会話履歴(`messages`)を共有・追記する仕組みになっているため、非同期化
すると「1件目の処理中に2件目の発言が`messages`に追記され、1件目の
応答が2件目宛であるかのように扱われてしまう」という順序が壊れる不具合を
生みかねない。そのため、この2つは意図的にバックグラウンド化の対象外
とし、これまで通り同期的に実行されることも合わせて検証する
(`yoriai._BackgroundJobRunner`のクラスdocstring参照)。

`prompt_toolkit.input.create_pipe_input`で仮想的なキー入力を送り込み、
`yoriai._create_repl_prompt_session`・`yoriai._create_background_job_runner`を
差し替えて`_run_repl_client`全体を実際に動かして検証する(既存のREPL
テストファイル群と同じ手法)。

使い方: python3 tests/test_background_collaborate.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"  # Enterキー単体(送信)


def _wait_until(predicate, timeout=5, interval=0.02):
    """`predicate()`が真になるまで短い間隔でポーリングする(`time.sleep`の
    決め打ちではなく、実際に条件が満たされた時点で先に進めるようにする
    ための共通ヘルパー)。タイムアウトしても例外は投げず、最後の判定結果を
    そのまま返す(呼び出し元がその後のアサーションで詳細な失敗理由を
    報告できるようにするため)。
    """
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


def test_background_job_runner_runs_job_without_blocking_submit():
    runner = yoriai._BackgroundJobRunner()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def job():
        started.set()
        release.wait(timeout=5)
        finished.set()

    runner.submit(job)
    assert started.wait(timeout=5), "ジョブが実行されませんでした"
    assert not finished.is_set(), "submit()の呼び出し元はジョブの完了を待たないはずです"
    release.set()
    runner.join()
    assert finished.is_set()


def test_background_job_runner_prints_queued_notice_only_when_busy():
    runner = yoriai._BackgroundJobRunner()
    release = threading.Event()

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        runner.submit(lambda: release.wait(timeout=5), queued_notice="[queued: 1]")
        # 1件目はすぐに拾われるはずなので、少し待ってから2件目を投入する。
        time.sleep(0.05)
        runner.submit(lambda: None, queued_notice="[queued: 2]")
    release.set()
    runner.join()

    output = buf.getvalue()
    assert "[queued: 1]" not in output, "最初のジョブでは(先行するジョブが無いため)通知は出ないはずです"
    assert "[queued: 2]" in output, "実行中のジョブがある間に投入した2件目では通知が出るはずです"


def test_background_job_runner_continues_after_a_job_raises():
    """1件目のジョブが例外を送出しても、ワーカースレッド自体は止まらず
    2件目のジョブが引き続き処理されることを確認する(1つのジョブの
    失敗が対話モード全体を止めてはいけない)。
    """
    runner = yoriai._BackgroundJobRunner()
    second_ran = threading.Event()

    runner.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    runner.submit(lambda: second_ran.set())
    runner.join()

    assert second_ran.is_set(), "1件目の失敗後も2件目のジョブは実行されるはずです"


def _run_repl_with_keys(keystrokes: str, ask_collaborate_side_effect=None, ask_single_side_effect=None):
    """`tests/test_repl_input_handling.py`と同様、`_run_repl_client`
    全体を実際に走らせる。`yoriai._create_background_job_runner`も
    差し替え、テスト側でバックグラウンドジョブの完了を`.join()`で
    待てるようにする(`_run_repl_client`自体は非同期ジョブの完了を
    待たずに`exit`できる設計のため、副作用を確認するにはテスト側で
    明示的に待つ必要がある)。

    仮の判断: 依頼系関数(`_ask_organization_collaborate`等)の差し替えは、
    `_run_repl_client`が返ってきた時点では元に戻さない。バックグラウンド
    ジョブは`_run_repl_client`の終了後もキューに残っている・実行中で
    ある可能性があり(それこそがこの機能の核心)、ここで即座に元へ戻して
    しまうと、まだキューに残っているジョブが本物の`_ask_organization_
    collaborate`(実在しないポートへの本物のHTTP問い合わせ)を呼んで
    しまう。そのため、戻り値として`restore_dispatch_stubs`関数を返し、
    呼び出し元が`runner.join()`で全ジョブの完了を待った後に、明示的に
    元へ戻すことを要求する。
    """
    calls = {"classify": 0, "ask_single": 0, "ask_multi": 0, "ask_collaborate": 0, "ask_parallel": 0, "resume_all": 0}

    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_classify = yoriai._classify_execution_mode
    original_ask = yoriai._ask_organization
    original_ask_multi = yoriai._ask_organization_multi
    original_ask_collaborate = yoriai._ask_organization_collaborate
    original_ask_parallel = yoriai._ask_organization_parallel
    original_resume_all = yoriai._run_resume_all

    def restore_dispatch_stubs():
        yoriai._classify_execution_mode = original_classify
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_multi = original_ask_multi
        yoriai._ask_organization_collaborate = original_ask_collaborate
        yoriai._ask_organization_parallel = original_ask_parallel
        yoriai._run_resume_all = original_resume_all

    def spy_classify(text, out_dir=None):
        calls["classify"] += 1
        return original_classify(text, out_dir)

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

    def stub_ask_parallel(*args, **kwargs):
        calls["ask_parallel"] += 1

    def stub_resume_all(*args, **kwargs):
        calls["resume_all"] += 1

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
        yoriai._classify_execution_mode = spy_classify
        yoriai._ask_organization = stub_ask
        yoriai._ask_organization_multi = stub_ask_multi
        yoriai._ask_organization_collaborate = stub_ask_collaborate
        yoriai._ask_organization_parallel = stub_ask_parallel
        yoriai._run_resume_all = stub_resume_all

        pipe_input.send_text(keystrokes)

        buf = io.StringIO()
        # 仮の判断: この関数はバックグラウンドジョブの完了(呼び出し元が
        # `runner.join()`する)を待たずに返るため、ここで一時ディレクトリを
        # 作ってもこの関数内では削除しない(ジョブが後から書き込む可能性が
        # ある)。OSが定期的に掃除する/tmp配下に作ることで、少なくとも
        # リポジトリの作業ディレクトリを汚さないようにする。
        out_dir = tempfile.mkdtemp(prefix="yoriai_background_collaborate_test_")
        with contextlib.redirect_stdout(buf):
            yoriai._run_repl_client(47120, "fingerprint", out_dir)
        yoriai._create_repl_prompt_session = original_create_session
        yoriai._create_background_job_runner = original_create_runner

    return buf.getvalue(), calls, runner_holder.get("runner"), restore_dispatch_stubs


def test_agree_command_returns_to_prompt_before_the_job_finishes():
    """依頼の要件2の核心: `//agree`は完了を待たずに次の入力(ここでは
    `exit`)をすぐに受け付け、対話モードを終了できることを確認する。
    バックグラウンドジョブは`release`イベントを立てるまで完了しない
    ようにしておき、`_run_repl_client`が(そのジョブの完了を待たずに)
    先に終了することを確認したうえで、最後にジョブを完了させて後始末する。
    """
    release = threading.Event()

    def slow_collaborate():
        release.wait(timeout=5)

    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT + "exit" + _SUBMIT,
        ask_collaborate_side_effect=slow_collaborate,
    )
    assert "対話モードを終了します。" in output, (
        f"バックグラウンドジョブの完了を待たずに終了できるはずです: {output}"
    )
    release.set()
    runner.join()
    restore()
    assert calls["ask_collaborate"] == 1, calls


def test_auto_classified_collaborate_mode_is_also_backgrounded():
    """平文からの自動ツール選択の復活: 「作って」等の制作系キーワードを
    含む平文は、`//agree`を明示しなくても自動的に協業モードへ振り分け
    られ、`//agree`と同様にバックグラウンドジョブとして扱われる
    (完了を待たずに次の入力・`exit`を受け付けられる)ことを確認する。
    """
    release = threading.Event()

    def slow_collaborate():
        release.wait(timeout=5)

    output, calls, runner, restore = _run_repl_with_keys(
        "ToDoリストのCLIツールを作って" + _SUBMIT + "exit" + _SUBMIT,
        ask_collaborate_side_effect=slow_collaborate,
    )
    assert "対話モードを終了します。" in output, output
    release.set()
    runner.join()
    restore()
    assert calls["ask_collaborate"] == 1, calls


def test_followup_right_after_a_dialogue_pause_is_not_reclassified():
    """平文からの自動ツール選択の復活に伴う回帰検知(637cce6で報告された
    不具合の再発防止): 対話プロトコルが人間の確認を求めて一時停止した
    直後に、たとえ協業モードのキーワード(「作って」)を含む発言を送っても、
    新規の自動判定にはかけず「今の会話の続き」の単発質問として扱う
    ことを確認する。
    """
    def paused_collaborate():
        print(yoriai._DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)

    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT
        + "やっぱり別のアプリを作って" + _SUBMIT
        + "exit" + _SUBMIT,
        ask_collaborate_side_effect=paused_collaborate,
    )
    runner.join()
    restore()
    assert "対話モードを終了します。" in output, output
    assert "[💬 対話プロトコルの一時停止直後の発言のため、会話の続きとして扱います]" in output, output
    # 1回目(明示//agree)はモード判定を経由しない。2回目の「作って」を
    # 含む続きの発言も、一時停止直後のためモード判定をスキップするはず。
    assert calls["classify"] == 0, calls
    assert calls["ask_collaborate"] == 1, calls
    assert calls["ask_single"] == 1, calls


def test_followup_right_after_a_dialogue_pause_fallback_disables_web_search():
    """不具合修正の回帰検知(対話プロトコル一時停止後、実装フェーズに
    繋がらない不具合、副作用の是正): 構造化された再開情報(`pending_box`)が
    無く、やむを得ず一時停止直後の続きの発言を単発質問(`_ask_organization`)
    へフォールバックさせる場合、その問い合わせに限りweb_searchツールを
    オファーしないことを確認する(`disable_web_search=True`が渡ること)。
    通常の(一時停止直後ではない)単発質問では、これまで通りweb_search
    ツールをオファーする(`disable_web_search`が渡らない、または`False`)。

    仮の判断: `test_followup_right_after_a_design_dialogue_pause_resumes_
    instead_of_single_question`と同じ理由(バックグラウンドジョブの完了と
    次のキー入力の間の競合)により、`_run_repl_client`を別スレッドで動かし、
    一時停止のジョブが完了したことを確認してから続きの発言を送り込む。
    """
    ask_single_kwargs_calls = []
    paused_ready = threading.Event()

    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_ask = yoriai._ask_organization
    original_ask_collaborate = yoriai._ask_organization_collaborate

    def stub_ask(*args, **kwargs):
        ask_single_kwargs_calls.append(kwargs)

    def paused_collaborate(*args, **kwargs):
        print(yoriai._DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)
        paused_ready.set()

    yoriai._ask_organization = stub_ask
    yoriai._ask_organization_collaborate = paused_collaborate

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    out_dir = tempfile.mkdtemp(prefix="yoriai_dialogue_pause_no_web_search_test_")
    buf = io.StringIO()
    try:
        with create_pipe_input() as pipe_input:
            def fake_create_session():
                return PromptSession(
                    input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
                )

            yoriai._create_repl_prompt_session = fake_create_session
            yoriai._create_background_job_runner = fake_create_runner

            def run_client():
                with contextlib.redirect_stdout(buf):
                    yoriai._run_repl_client(47120, "fingerprint", out_dir)

            thread = threading.Thread(target=run_client)
            thread.start()
            try:
                pipe_input.send_text("富士山の標高は?" + _SUBMIT)
                pipe_input.send_text(f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT)
                assert paused_ready.wait(timeout=5), "合意フェーズの一時停止(バックグラウンドジョブ)がタイムアウトしました"
                runner_holder["runner"].join()
                pipe_input.send_text("やっぱり別のアプリを作って" + _SUBMIT + "exit" + _SUBMIT)
                thread.join(timeout=5)
                assert not thread.is_alive(), "対話モードがexitで終了しませんでした"
            finally:
                yoriai._create_repl_prompt_session = original_create_session
                yoriai._create_background_job_runner = original_create_runner
    finally:
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_collaborate = original_ask_collaborate
        shutil.rmtree(out_dir, ignore_errors=True)

    output = buf.getvalue()
    assert "対話モードを終了します。" in output, output
    assert len(ask_single_kwargs_calls) == 2, ask_single_kwargs_calls
    # 1回目(一時停止前の通常の単発質問)はweb_searchを無効化しない。
    assert ask_single_kwargs_calls[0].get("disable_web_search", False) is False, ask_single_kwargs_calls
    # 2回目(一時停止直後の単発質問フォールバック)はweb_searchを無効化する。
    assert ask_single_kwargs_calls[1].get("disable_web_search") is True, ask_single_kwargs_calls


def test_followup_right_after_a_design_dialogue_pause_resumes_instead_of_single_question():
    """不具合修正の回帰検知(対話プロトコル一時停止後、実装フェーズに
    繋がらない不具合、パターンA): //agreeの合意フェーズが構造化された
    再開情報(`pending_box`)を残して一時停止した場合、直後の発言は単発
    質問(`_ask_organization`)にはルーティングされず、合意フェーズの再開
    (`_resume_organization_collaborate`)にルーティングされることを確認
    する。

    仮の判断: `_run_repl_with_keys`(全キーストロークを起動前に一括で
    パイプへ送り込む既存の共通ハーネス)をそのまま使うと、「一時停止を
    知らせるバックグラウンドジョブの完了」と「2件目の発言の読み取り」の
    間に本質的な競合(どちらが先に進むかはOSのスレッドスケジューリング
    次第)が生じ、テストとして不安定になる(既存の類似テスト
    `test_followup_right_after_a_dialogue_pause_is_not_reclassified`も
    同じ競合を抱えている)。この不具合修正の回帰検知としては安定して
    動作させたいため、`_run_repl_client`を別スレッドで動かし、1件目の
    ジョブが完了した(`paused_ready`が立った)ことを確認してから2件目の
    キー入力を送り込むことで、競合を避けて決定的にする。
    """
    calls = {"classify": 0, "ask_single": 0, "ask_collaborate": 0, "resume_organization_collaborate": 0}
    paused_ready = threading.Event()

    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_classify = yoriai._classify_execution_mode
    original_ask = yoriai._ask_organization
    original_ask_collaborate = yoriai._ask_organization_collaborate
    original_resume_organization_collaborate = yoriai._resume_organization_collaborate

    def spy_classify(text, out_dir=None):
        calls["classify"] += 1
        return original_classify(text, out_dir)

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1

    def stub_ask_collaborate(
        port, org_fingerprint, request, out_dir, enable_dialogue=False, pending_box=None, completed_box=None,
    ):
        calls["ask_collaborate"] += 1
        pending = yoriai._PendingDesignDialogue(
            port, org_fingerprint, request, [{"label": "設計担当"}], "/tmp/fake-project-dir",
            {
                "status": yoriai.DIALOGUE_STATUS_NEEDS_HUMAN, "transcript": [], "summary": "",
                "human_message": "方向性を決めてください。",
            },
        )
        pending_box.set(pending)
        print(yoriai._DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)
        paused_ready.set()

    def stub_resume_organization_collaborate(*args, **kwargs):
        calls["resume_organization_collaborate"] += 1

    yoriai._classify_execution_mode = spy_classify
    yoriai._ask_organization = stub_ask
    yoriai._ask_organization_collaborate = stub_ask_collaborate
    yoriai._resume_organization_collaborate = stub_resume_organization_collaborate

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    out_dir = tempfile.mkdtemp(prefix="yoriai_design_pause_resume_repl_test_")
    buf = io.StringIO()
    try:
        with create_pipe_input() as pipe_input:
            def fake_create_session():
                return PromptSession(
                    input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
                )

            yoriai._create_repl_prompt_session = fake_create_session
            yoriai._create_background_job_runner = fake_create_runner

            def run_client():
                with contextlib.redirect_stdout(buf):
                    yoriai._run_repl_client(47120, "fingerprint", out_dir)

            thread = threading.Thread(target=run_client)
            thread.start()
            try:
                pipe_input.send_text(f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT)
                assert paused_ready.wait(timeout=5), "合意フェーズの一時停止(バックグラウンドジョブ)がタイムアウトしました"
                runner_holder["runner"].join()
                pipe_input.send_text("方向性はAでお願いします" + _SUBMIT + "exit" + _SUBMIT)
                thread.join(timeout=5)
                assert not thread.is_alive(), "対話モードがexitで終了しませんでした"
                runner_holder["runner"].join()
            finally:
                yoriai._create_repl_prompt_session = original_create_session
                yoriai._create_background_job_runner = original_create_runner
    finally:
        yoriai._classify_execution_mode = original_classify
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_collaborate = original_ask_collaborate
        yoriai._resume_organization_collaborate = original_resume_organization_collaborate
        shutil.rmtree(out_dir, ignore_errors=True)

    output = buf.getvalue()
    assert "対話モードを終了します。" in output, output
    assert "[💬 対話プロトコルの一時停止直後の発言のため、合意フェーズを続きから再開します]" in output, output
    assert calls["classify"] == 0, calls
    assert calls["ask_collaborate"] == 1, calls
    # 単発質問(_ask_organization)ではなく、合意フェーズの再開へルーティング
    # されるはずです(不具合修正前は誤ってask_singleへ流れていた)。
    assert calls["ask_single"] == 0, calls
    assert calls["resume_organization_collaborate"] == 1, calls


def test_second_followup_during_slow_resume_still_avoids_tool_less_single_chat():
    """重大なバグ報告への対応の回帰検知: 「続けて」の後、実際のファイル
    書き込みが一切行われず、write_file等のツールを持たない単発チャットの
    文章だけで"完了したふり"になってしまう不具合。

    再現条件: (1)`//agree`の合意フェーズが一時停止し、一時停止マーカーが
    `messages`(会話履歴)に記録され`pending_box`にも再開情報が格納される、
    (2)ユーザーが「続けて」を送ると、メインループはその場で同期的に
    `pending_box.take()`して空にし、再開ジョブをバックグラウンドキューに
    積む、(3)この再開ジョブが実際に`messages`へ「続けて」を追記する
    (`_run_job_with_conversation_log`)より前に、ユーザーがさらに3件目の
    発言を送る。

    この場合、`pending_box`は既に空(2で消費済み)、かつ`messages`の
    末尾はまだ一時停止マーカーのまま(3で再開ジョブの追記がまだ)という
    状態になり、`_last_turn_is_dialogue_pause`は真と判定される。修正前は
    ここでwrite_file等のツールを一切持たない単発質問(`_ask_organization`)
    に落ちてしまっていたが、`_PendingDesignDialogueBox.last_project_dir()`
    (`take()`後も保持され続ける)を使って、直前に一時停止していた
    プロジェクトへの修正セッション(`_run_fix_on_project`、write_file等の
    ツールを実際に持つ)として継続することを確認する。

    仮の判断: この一瞬の競合状態を`time.sleep`頼みではなく決定的に
    再現するため、`_run_job_with_conversation_log`自体を差し替え、
    「続けて」ジョブに限って`messages`への追記の直前で
    `threading.Event`により意図的に足止めする。
    """
    calls = {"ask_single": 0, "run_fix_on_project": 0, "project_dir": None, "request": None}
    paused_ready = threading.Event()
    resume_job_dequeued = threading.Event()
    release_resume_job = threading.Event()

    original_ask = yoriai._ask_organization
    original_ask_collaborate = yoriai._ask_organization_collaborate
    original_resume_organization_collaborate = yoriai._resume_organization_collaborate
    original_run_fix_on_project = yoriai._run_fix_on_project
    original_run_job_with_conversation_log = yoriai._run_job_with_conversation_log
    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner

    stale_project_dir = tempfile.mkdtemp(prefix="yoriai_stale_paused_project_")

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1

    def stub_ask_collaborate(
        port, org_fingerprint, request, out_dir, enable_dialogue=False, pending_box=None, completed_box=None,
    ):
        pending = yoriai._PendingDesignDialogue(
            port, org_fingerprint, request, [{"label": "設計担当"}], stale_project_dir,
            {
                "status": yoriai.DIALOGUE_STATUS_NEEDS_HUMAN, "transcript": [], "summary": "",
                "human_message": "方向性を決めてください。",
            },
        )
        pending_box.set(pending)
        print(yoriai._DIALOGUE_PAUSE_HUMAN_JUDGEMENT_MARKER)
        paused_ready.set()

    def stub_resume_organization_collaborate(*args, **kwargs):
        pass

    def stub_run_fix_on_project(port, org_fingerprint, project_dir, request, out_dir, enable_dialogue=True):
        calls["run_fix_on_project"] += 1
        calls["project_dir"] = project_dir
        calls["request"] = request

    def delayed_run_job_with_conversation_log(chat_log, user_label, job):
        if user_label == "続けて":
            # 「続けて」ジョブがバックグラウンドワーカーに実際に着手された
            # (=キューから取り出された)ことをテスト側に伝えたうえで、
            # `messages`への追記(一時停止マーカーの上書き)の直前で
            # 意図的に足止めする。
            resume_job_dequeued.set()
            release_resume_job.wait(timeout=5)
        return original_run_job_with_conversation_log(chat_log, user_label, job)

    yoriai._ask_organization = stub_ask
    yoriai._ask_organization_collaborate = stub_ask_collaborate
    yoriai._resume_organization_collaborate = stub_resume_organization_collaborate
    yoriai._run_fix_on_project = stub_run_fix_on_project
    yoriai._run_job_with_conversation_log = delayed_run_job_with_conversation_log

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    out_dir = tempfile.mkdtemp(prefix="yoriai_race_followup_test_")
    buf = io.StringIO()
    try:
        with create_pipe_input() as pipe_input:
            def fake_create_session():
                return PromptSession(
                    input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
                )

            yoriai._create_repl_prompt_session = fake_create_session
            yoriai._create_background_job_runner = fake_create_runner

            def run_client():
                with contextlib.redirect_stdout(buf):
                    yoriai._run_repl_client(47120, "fingerprint", out_dir)

            thread = threading.Thread(target=run_client)
            thread.start()
            try:
                pipe_input.send_text(f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT)
                assert paused_ready.wait(timeout=5), "合意フェーズの一時停止(バックグラウンドジョブ)がタイムアウトしました"
                # 一時停止ジョブは(このスタブでは)ブロックせずすぐ完了する
                # ため、まずキューが空になる(=マーカーがmessagesに記録
                # される)のを待つ。
                runner_holder["runner"].join()
                pipe_input.send_text("続けて" + _SUBMIT)
                assert resume_job_dequeued.wait(timeout=5), (
                    "「続けて」ジョブがバックグラウンドワーカーに着手されませんでした"
                )
                # ここでpending_boxは既に空、かつ再開ジョブはmessagesへの
                # 追記直前で足止めされている(=messagesの末尾はまだ一時
                # 停止マーカーのまま)。この状態で3件目の発言を送る。
                pipe_input.send_text("早く終わらせて" + _SUBMIT)
                _wait_until(lambda: "直前のプロジェクト" in buf.getvalue() or calls["ask_single"] > 0, timeout=5)
                release_resume_job.set()
                pipe_input.send_text("exit" + _SUBMIT)
                thread.join(timeout=5)
                assert not thread.is_alive(), "対話モードがexitで終了しませんでした"
                runner_holder["runner"].join()
            finally:
                yoriai._create_repl_prompt_session = original_create_session
                yoriai._create_background_job_runner = original_create_runner
    finally:
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_collaborate = original_ask_collaborate
        yoriai._resume_organization_collaborate = original_resume_organization_collaborate
        yoriai._run_fix_on_project = original_run_fix_on_project
        yoriai._run_job_with_conversation_log = original_run_job_with_conversation_log
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(stale_project_dir, ignore_errors=True)

    output = buf.getvalue()
    assert "対話モードを終了します。" in output, output
    assert "直前のプロジェクト" in output, output
    # write_file等を持たない単発質問(_ask_organization)には落ちないはずです。
    assert calls["ask_single"] == 0, calls
    assert calls["run_fix_on_project"] == 1, calls
    assert calls["project_dir"] == stale_project_dir, calls
    assert calls["request"] == "早く終わらせて", calls


def test_followup_right_after_collaborate_completion_becomes_a_fix_session():
    """不具合修正の回帰検知: 「〇〇を作って」の協業モードが実装フェーズまで
    完了した直後、`直して`・`修正して`・`作業して`等のキーワードを一切
    含まない自然文の追加指示(例:「Webで調べながらコンテンツを書いて」)が、
    `write_file`等のプロジェクト操作ツールを持たない単発質問
    (`_ask_organization`)に落ちず、そのプロジェクトへの継続的な修正依頼
    (`_FixSession`経由の`_run_fix_on_project`)として自動的に扱われることを
    確認する(`_CompletedBuildBox`参照)。

    `test_followup_right_after_a_design_dialogue_pause_resumes_instead_of_
    single_question`と同じ理由(バックグラウンドジョブの完了と2件目の
    発言の読み取りの間の競合を避ける)で、別スレッド+`threading.Event`に
    よる決定的な同期を使う。
    """
    calls = {"ask_single": 0, "run_fix_on_project": 0, "project_dir": None, "request": None}
    completed_ready = threading.Event()

    original_ask = yoriai._ask_organization
    original_ask_collaborate = yoriai._ask_organization_collaborate
    original_run_fix_on_project = yoriai._run_fix_on_project
    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner

    def stub_ask(*args, **kwargs):
        calls["ask_single"] += 1

    def stub_ask_collaborate(
        port, org_fingerprint, request, out_dir, enable_dialogue=False, pending_box=None, completed_box=None,
    ):
        if completed_box is not None:
            completed_box.set("/tmp/fake-completed-project-dir")
        completed_ready.set()

    def stub_run_fix_on_project(port, org_fingerprint, project_dir, request, out_dir, enable_dialogue=True):
        calls["run_fix_on_project"] += 1
        calls["project_dir"] = project_dir
        calls["request"] = request

    yoriai._ask_organization = stub_ask
    yoriai._ask_organization_collaborate = stub_ask_collaborate
    yoriai._run_fix_on_project = stub_run_fix_on_project

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    out_dir = tempfile.mkdtemp(prefix="yoriai_completed_build_followup_test_")
    buf = io.StringIO()
    try:
        with create_pipe_input() as pipe_input:
            def fake_create_session():
                return PromptSession(
                    input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
                )

            yoriai._create_repl_prompt_session = fake_create_session
            yoriai._create_background_job_runner = fake_create_runner

            def run_client():
                with contextlib.redirect_stdout(buf):
                    yoriai._run_repl_client(47120, "fingerprint", out_dir)

            thread = threading.Thread(target=run_client)
            thread.start()
            try:
                pipe_input.send_text("ObsidianのPKMサイトを作って" + _SUBMIT)
                assert completed_ready.wait(timeout=5), "協業モードの実装フェーズ完了(バックグラウンドジョブ)がタイムアウトしました"
                runner_holder["runner"].join()
                pipe_input.send_text("Webで調べながらコンテンツを書いて" + _SUBMIT + "exit" + _SUBMIT)
                thread.join(timeout=5)
                assert not thread.is_alive(), "対話モードがexitで終了しませんでした"
                runner_holder["runner"].join()
            finally:
                yoriai._create_repl_prompt_session = original_create_session
                yoriai._create_background_job_runner = original_create_runner
    finally:
        yoriai._ask_organization = original_ask
        yoriai._ask_organization_collaborate = original_ask_collaborate
        yoriai._run_fix_on_project = original_run_fix_on_project
        shutil.rmtree(out_dir, ignore_errors=True)

    output = buf.getvalue()
    assert "対話モードを終了します。" in output, output
    assert "[🏗️" in output, output
    # write_file等を持たない単発質問(_ask_organization)には落ちないはずです
    # (不具合修正前は、これらのキーワードを含まない追加指示がここへ流れて
    # いた)。
    assert calls["ask_single"] == 0, calls
    assert calls["run_fix_on_project"] == 1, calls
    assert calls["project_dir"] == "/tmp/fake-completed-project-dir", calls
    assert calls["request"] == "Webで調べながらコンテンツを書いて", calls


def test_parallel_command_is_backgrounded():
    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.PARALLEL_QUERY_COMMAND} a.py:aを作って" + _SUBMIT + "exit" + _SUBMIT,
    )
    runner.join()
    restore()
    assert "対話モードを終了します。" in output, output
    assert calls["ask_parallel"] == 1, calls


def test_resume_all_command_is_backgrounded():
    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.RESUME_ALL_COMMAND}" + _SUBMIT + "exit" + _SUBMIT,
    )
    runner.join()
    restore()
    assert "対話モードを終了します。" in output, output
    assert calls["resume_all"] == 1, calls


def test_second_agree_while_first_still_running_shows_queued_notice():
    """複数の依頼を続けて入力した場合、キューに積まれた旨の通知が表示
    されることを確認する(依頼の「いつでも次の入力を受け付けられる」の
    裏付け: 入力自体は即座に受理され、処理順だけがキューで管理される)。
    1件目のジョブは`release`を立てるまで完了しないようにしておき、その
    間に2件目の`//agree`を入力すると、2件目のsubmit()の時点で1件目が
    まだ完了していない(`_pending > 0`)ため通知が出るはずである。
    """
    release = threading.Event()
    call_count = {"n": 0}

    def side_effect():
        call_count["n"] += 1
        if call_count["n"] == 1:
            release.wait(timeout=5)

    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.AGREE_COMMAND} 1件目" + _SUBMIT + f"{yoriai.AGREE_COMMAND} 2件目" + _SUBMIT + "exit" + _SUBMIT,
        ask_collaborate_side_effect=side_effect,
    )
    release.set()
    runner.join()
    restore()

    assert yoriai._BACKGROUND_QUEUED_NOTICE in output, output
    assert calls["ask_collaborate"] == 2, calls


def test_single_query_mode_is_not_backgrounded():
    """依頼の意図しない副作用を作らないための回帰検知: 単発質問は会話
    履歴(messages)を共有するため、これまで通り同期的に実行され、
    バックグラウンドジョブキューには乗らないことを確認する。
    """
    ran_before_exit = {"value": False}

    def mark_ran():
        ran_before_exit["value"] = True

    output, calls, runner, restore = _run_repl_with_keys(
        "富士山の標高は?" + _SUBMIT + "exit" + _SUBMIT,
        ask_single_side_effect=mark_ran,
    )
    runner.join()
    restore()
    # 同期実行であれば、_run_repl_clientが"exit"を処理する前に
    # 必ずask_singleが完了しているはず(バックグラウンド化されていれば
    # このタイミング保証は無くなる)。
    assert ran_before_exit["value"] is True
    assert calls["ask_single"] == 1, calls
    assert "対話モードを終了します。" in output, output


def test_double_ctrl_c_still_terminates_while_a_background_job_is_running():
    """依頼の要件3(最優先): バックグラウンドジョブが実行中であっても、
    Ctrl+Cを2秒以内に2回連続で押すと確実に対話モードを終了できることを
    確認する(既存の非常口機能が、今回のバックグラウンド化によって
    壊れていないことの回帰検知)。バックグラウンドジョブは`daemon=True`の
    スレッドで動いているため、対話モードの終了(プロセス終了)を妨げない。
    """
    release = threading.Event()

    def slow_collaborate():
        release.wait(timeout=5)

    output, calls, runner, restore = _run_repl_with_keys(
        f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT + "\x03\x03",
        ask_collaborate_side_effect=slow_collaborate,
    )
    assert "対話モードを終了します。" in output, output
    assert "[⚠️ Ctrl+Cが2回連続で押されたため、対話モードを終了します]" in output, output
    release.set()
    runner.join()
    restore()


def main():
    tests = [
        test_background_job_runner_runs_job_without_blocking_submit,
        test_background_job_runner_prints_queued_notice_only_when_busy,
        test_background_job_runner_continues_after_a_job_raises,
        test_agree_command_returns_to_prompt_before_the_job_finishes,
        test_auto_classified_collaborate_mode_is_also_backgrounded,
        test_followup_right_after_a_dialogue_pause_is_not_reclassified,
        test_followup_right_after_a_dialogue_pause_fallback_disables_web_search,
        test_followup_right_after_a_design_dialogue_pause_resumes_instead_of_single_question,
        test_second_followup_during_slow_resume_still_avoids_tool_less_single_chat,
        test_followup_right_after_collaborate_completion_becomes_a_fix_session,
        test_parallel_command_is_backgrounded,
        test_resume_all_command_is_backgrounded,
        test_second_agree_while_first_still_running_shows_queued_notice,
        test_single_query_mode_is_not_backgrounded,
        test_double_ctrl_c_still_terminates_while_a_background_job_is_running,
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
