#!/usr/bin/env python3
"""`--chat`の1回の起動中、単発質問だけでなく`//agree`・`//fix`・
`//plan-only`・`//parallel`・`//resume-all`のやり取りも同じ会話履歴に
積み上げられ、起動時刻をファイル名に持つログファイルにも記録されることを
検証する。

実機で、対話プロトコルが「人間の確認が必要です」と一時停止した直後に
ユーザーが感想・追加情報を送っても、それが全く無関係な新規の単発質問
として扱われ、直前まで何を話していたかをメンバー側が一切踏まえられない
不具合が報告された。これに対応するため、`messages`(会話履歴)を
`_ChatLog`という`list`互換のクラスに置き換え、バックグラウンドジョブの
出力も`_run_job_with_conversation_log`経由で同じ履歴に記録するように
した。

使い方: python3 tests/test_chat_conversation_log.py
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402

from prompt_toolkit import PromptSession  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

_SUBMIT = "\r"


# ---------------------------------------------------------------------------
# _ChatLog
# ---------------------------------------------------------------------------

def test_chat_log_is_list_compatible_and_persists_appends_to_file():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        log_path = os.path.join(tmp_dir, "chat.md")
        chat_log = yoriai._ChatLog(log_path)
        assert isinstance(chat_log, list)

        chat_log.append({"role": "user", "content": "こんにちは"})
        chat_log.append({"role": "assistant", "content": "こんにちは、元気ですか?"})

        assert len(chat_log) == 2
        assert chat_log[-1]["content"] == "こんにちは、元気ですか?"
        # 既存の_ask_organization等が行う素朴なlist操作(スライス・イテレート)も
        # そのまま使えるはず。
        assert [m["role"] for m in chat_log] == ["user", "assistant"]

        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        assert "こんにちは" in content
        assert "こんにちは、元気ですか?" in content
        assert "user" in content and "assistant" in content
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_chat_log_creates_missing_parent_directory():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        log_path = os.path.join(tmp_dir, "chat_logs", "chat_20260101_000000.md")
        yoriai._ChatLog(log_path)
        assert os.path.isfile(log_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_create_chat_log_uses_startup_timestamp_in_filename():
    out_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        chat_log = yoriai._create_chat_log(out_dir)
        log_dir = os.path.join(out_dir, yoriai._CHAT_LOG_SUBDIR_NAME)
        files = os.listdir(log_dir)
        assert len(files) == 1, files
        assert re.match(r"^chat_\d{8}_\d{6}\.md$", files[0]), files[0]
        assert isinstance(chat_log, list) and len(chat_log) == 0
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _run_job_with_conversation_log
# ---------------------------------------------------------------------------

def test_run_job_with_conversation_log_records_user_and_captured_output():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        chat_log = yoriai._ChatLog(os.path.join(tmp_dir, "chat.md"))

        def job():
            print("[🧭 対話プロトコルによる合意フェーズ開始...]")
            print("最終合意内容: storage.py: 実装する")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_job_with_conversation_log(chat_log, "//agree ToDoリストを作って", job)

        # 実際の標準出力(端末表示)には、これまで通りjob()の出力がそのまま出る。
        assert "対話プロトコルによる合意フェーズ開始" in buf.getvalue()

        assert len(chat_log) == 2
        assert chat_log[0] == {"role": "user", "content": "//agree ToDoリストを作って"}
        assert chat_log[1]["role"] == "assistant"
        assert "最終合意内容: storage.py: 実装する" in chat_log[1]["content"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_run_job_with_conversation_log_records_placeholder_when_job_prints_nothing():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        chat_log = yoriai._ChatLog(os.path.join(tmp_dir, "chat.md"))
        with contextlib.redirect_stdout(io.StringIO()):
            yoriai._run_job_with_conversation_log(chat_log, "//resume-all", lambda: None)
        assert chat_log[1]["content"] == "(出力はありませんでした)"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_run_job_with_conversation_log_truncates_long_output_in_history_but_not_on_screen():
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        chat_log = yoriai._ChatLog(os.path.join(tmp_dir, "chat.md"))
        long_text = "x" * (yoriai._BACKGROUND_JOB_HISTORY_TRUNCATE_CHARS + 500)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_job_with_conversation_log(chat_log, "//plan-only 何か作りたい", lambda: print(long_text))

        # 画面表示(実際の標準出力)は切り詰めない。
        assert long_text in buf.getvalue()
        # 会話履歴(次にLLMへ送り直す部分)は切り詰める。
        assert len(chat_log[1]["content"]) < len(long_text)
        assert "以下省略" in chat_log[1]["content"]
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_run_job_with_conversation_log_is_noop_when_chat_log_none():
    ran = {"n": 0}

    def job():
        ran["n"] += 1

    yoriai._run_job_with_conversation_log(None, "//parallel a.py:何か", job)
    assert ran["n"] == 1


def test_run_job_with_conversation_log_does_not_clobber_stdout_changed_during_job():
    """デバッグ中に見つかった不具合の再発防止(実機で顕在化しうる):
    バックグラウンドジョブは、それを起動した側のコンテキスト
    (`patch_stdout()`・テストの`contextlib.redirect_stdout`等)より長く
    実行され続けることがある。ジョブの実行中に、その外側のコンテキストが
    既に`sys.stdout`を別の値へ復元し終えていた場合、ジョブの終了時に
    それを自分が最初に見た古い値で上書きしてはいけない(グローバルな
    `sys.stdout`が壊れた状態のまま残り、以降のprintがすべて見えなくなる
    不具合が実際に発生した)。
    """
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    original_stdout = sys.stdout
    try:
        chat_log = yoriai._ChatLog(os.path.join(tmp_dir, "chat.md"))
        sentinel = io.StringIO()

        def job():
            # ジョブの実行中に、外側のコンテキストが既に別の値へ
            # sys.stdoutを復元し終えたかのようにシミュレートする。
            sys.stdout = sentinel

        yoriai._run_job_with_conversation_log(chat_log, "//resume-all", job)
        assert sys.stdout is sentinel, (
            "外側のコンテキストが既に復元した値を上書きしてはいけません"
        )
    finally:
        sys.stdout = original_stdout
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 会話履歴の一本化: 単発質問が、直前のバックグラウンドジョブの
# やり取りを会話コンテキストとして踏まえられること
# ---------------------------------------------------------------------------

def test_single_query_after_agree_job_receives_prior_conversation_as_context():
    """依頼2の核心: `//agree`が(対話プロトコルにより)人間の確認を求めて
    一時停止した直後に単発質問を送っても、それが直前のやり取りを完全に
    無視した新規の会話として扱われないことを確認する。`_ask_organization`
    に渡される`messages`(=共有の会話履歴`chat_log`)に、直前の`//agree`
    の依頼文と結果が既に含まれているはずである。
    """
    tmp_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_test_")
    try:
        chat_log = yoriai._ChatLog(os.path.join(tmp_dir, "chat.md"))

        def agree_job():
            print("[🙋 対話プロトコル: 合意に至らず、人間の確認が必要です]")
            print("Mac miniさんには一度も設計案を振れていませんでした。")

        with contextlib.redirect_stdout(io.StringIO()):
            yoriai._run_job_with_conversation_log(
                chat_log, f"{yoriai.AGREE_COMMAND} ToDoリストのCLIツールを作って", agree_job,
            )

        # 続けて単発質問を送る(_run_repl_client内でこのように
        # messages.append(...)された後に_ask_organizationへ渡される)。
        chat_log.append({"role": "user", "content": "Macminiからの応答がないじゃん。ちゃんと会話に参加させてよ"})

        captured_messages = {}
        original_snapshot = yoriai._fetch_org_snapshot
        original_stream = yoriai._stream_chat_from_candidate

        def fake_snapshot(port, fp, fail_fast=False):
            return {
                "self": {
                    "device_name": "MacStudio",
                    "models": {"loaded": ["qwen2.5-coder-32b"], "installed": ["qwen2.5-coder-32b"]},
                    "memory": {"free_gb": 40},
                },
                "peers": [],
            }

        def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
            captured_messages["messages"] = list(messages)
            yield {"content": "承知しました、Mac miniさんにも振りますね。"}
            yield {"done": True}

        yoriai._fetch_org_snapshot = fake_snapshot
        yoriai._stream_chat_from_candidate = fake_stream
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                yoriai._ask_organization(47120, "fingerprint", chat_log)
        finally:
            yoriai._fetch_org_snapshot = original_snapshot
            yoriai._stream_chat_from_candidate = original_stream

        joined = " ".join(m["content"] for m in captured_messages["messages"])
        assert "ToDoリストのCLIツールを作って" in joined, (
            f"直前の//agreeの依頼文が会話履歴として渡っていません: {joined}"
        )
        assert "Mac miniさんには一度も設計案を振れていませんでした" in joined, (
            f"直前の//agreeの結果が会話履歴として渡っていません: {joined}"
        )
        assert "Macminiからの応答がないじゃん" in joined
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# REPL全体を通した統合テスト: //agreeの実行後、実際にログファイルへ
# 会話が記録されること
# ---------------------------------------------------------------------------

def test_repl_agree_command_writes_conversation_to_log_file():
    original_create_session = yoriai._create_repl_prompt_session
    original_create_runner = yoriai._create_background_job_runner
    original_ask_collaborate = yoriai._ask_organization_collaborate

    def stub_ask_collaborate(port, org_fingerprint, request, out_dir, **kwargs):
        print(f"[🧭 対話プロトコルによる合意フェーズ開始: {request}について検討します...]")
        print("[🙋 対話プロトコル: 合意に至らず、人間の確認が必要です]")

    runner_holder = {}

    def fake_create_runner():
        runner = original_create_runner()
        runner_holder["runner"] = runner
        return runner

    out_dir = tempfile.mkdtemp(prefix="yoriai_chat_log_repl_test_")
    try:
        with create_pipe_input() as pipe_input:
            def fake_create_session():
                return PromptSession(
                    input=pipe_input, output=DummyOutput(), key_bindings=yoriai._make_repl_key_bindings()
                )

            yoriai._create_repl_prompt_session = fake_create_session
            yoriai._create_background_job_runner = fake_create_runner
            yoriai._ask_organization_collaborate = stub_ask_collaborate

            pipe_input.send_text(f"{yoriai.AGREE_COMMAND} ToDoリストを作って" + _SUBMIT + "exit" + _SUBMIT)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                yoriai._run_repl_client(47120, "fingerprint", out_dir)
            yoriai._create_repl_prompt_session = original_create_session
            yoriai._create_background_job_runner = original_create_runner

        runner_holder["runner"].join()
        yoriai._ask_organization_collaborate = original_ask_collaborate

        log_dir = os.path.join(out_dir, yoriai._CHAT_LOG_SUBDIR_NAME)
        log_files = os.listdir(log_dir)
        assert len(log_files) == 1, log_files
        assert re.match(r"^chat_\d{8}_\d{6}\.md$", log_files[0]), log_files[0]

        with open(os.path.join(log_dir, log_files[0]), encoding="utf-8") as f:
            log_content = f.read()
        assert f"{yoriai.AGREE_COMMAND} ToDoリストを作って" in log_content
        assert "合意に至らず、人間の確認が必要です" in log_content
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# プロジェクトフォルダの日付プレフィックス
# ---------------------------------------------------------------------------

def test_project_name_with_date_prefix_starts_with_yymmdd():
    import time as time_module

    name = yoriai._project_name_with_date_prefix("ToDoリストのCLIツールを作って")
    today_prefix = time_module.strftime("%y%m%d")
    assert name == f"{today_prefix}-todo-cli", name


def test_project_name_with_date_prefix_supports_suffix_for_plan_only():
    import time as time_module

    name = yoriai._project_name_with_date_prefix("ToDoリストのCLIツールを作って", suffix="-plan")
    today_prefix = time_module.strftime("%y%m%d")
    assert name == f"{today_prefix}-todo-cli-plan", name


def test_generate_project_name_itself_has_no_date_prefix():
    """`_generate_project_name`自体は日付を混ぜない、依頼文からの命名
    だけを行う既存の役割のままであることを確認する(日付を付けるかどうかは
    `_project_name_with_date_prefix`側の関心事)。
    """
    assert yoriai._generate_project_name("ToDoリストのCLIツールを作って") == "todo-cli"


def main():
    tests = [
        test_chat_log_is_list_compatible_and_persists_appends_to_file,
        test_chat_log_creates_missing_parent_directory,
        test_create_chat_log_uses_startup_timestamp_in_filename,
        test_run_job_with_conversation_log_records_user_and_captured_output,
        test_run_job_with_conversation_log_records_placeholder_when_job_prints_nothing,
        test_run_job_with_conversation_log_truncates_long_output_in_history_but_not_on_screen,
        test_run_job_with_conversation_log_is_noop_when_chat_log_none,
        test_run_job_with_conversation_log_does_not_clobber_stdout_changed_during_job,
        test_single_query_after_agree_job_receives_prior_conversation_as_context,
        test_repl_agree_command_writes_conversation_to_log_file,
        test_project_name_with_date_prefix_starts_with_yymmdd,
        test_project_name_with_date_prefix_supports_suffix_for_plan_only,
        test_generate_project_name_itself_has_no_date_prefix,
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
