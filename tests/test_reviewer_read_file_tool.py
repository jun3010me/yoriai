#!/usr/bin/env python3
"""レビュー担当が、必要に応じて他のファイルの実際の中身をread_fileツールで
確認できる機能を検証する。

実機で、cli.pyのレビュー時に「storage.pyがconfig.pyを正しく使っているか
確認できない」という指摘が出た件を受けた改善。従来はレビュー担当自身が
担当したファイルの中身だけをプロンプトに埋め込んでいたが(伝聞情報)、
これに加えてread_fileという関数呼び出し(function calling)の選択肢を
与え、モデル自身が必要と判断した場合に実際のファイルの中身を取得できる
ようにした。

仮の判断(アーキテクチャ上の制約): プロジェクトの生成物は、その協業を
開始したメンバー(実装依頼元)のローカルディスクにしか存在しない。一方、
レビュー依頼は別のメンバー(多くの場合は別の物理マシン)の/chat
エンドポイントへのHTTPリクエストとして送られ、read_fileツールの実行
(`_execute_tool_call`)はそのレビュー担当自身のプロセス内で行われる。
そのため、read_fileはCHAT_TOOLS(常時有効なグローバルなツール)には
含めず、実装依頼元がレビュー依頼のリクエストボディに直接「その時点で
実際に保存されているファイルの中身」(available_files)を埋め込み、
レビュー担当側はそのリクエストの中で完結してファイルを「読む」(実際には
ネットワーク越しの追加の往復無しに、渡された辞書を参照するだけ)方式に
した。この設計により、新しいサーバーエンドポイントや、実装依頼元自身の
外部アドレスをレビュー担当に知らせる仕組みを新設せずに済んでいる。

このファイルでは、(1)ツール本体の実行(`_execute_read_file_tool_call`)、
(2)実装依頼元がその時点のプロジェクトの中身を集める
(`_snapshot_available_files`)、(3)`stream_chat_completion`の実際の
ツール呼び出しループを通しての実行(モデルがread_fileを呼び、結果を
踏まえて最終判定を出す一連の流れ)、(4)レビュー依頼を送る側
(`_review_one_file`)がavailable_filesを正しく組み立てて渡していること、
(5)呼び出し側(`_collect_answer_from_candidate`)がread_fileの呼び出しを
画面に表示すること、を検証する。

使い方: python3 tests/test_reviewer_read_file_tool.py
"""
import json
import os
import sys
import tempfile
import shutil
import io
import contextlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def test_execute_read_file_tool_call_returns_content_for_existing_file():
    tool_context = {"available_files": {"storage.py": "def add_todo():\n    pass\n"}, "read_file_call_count": 0}
    result = json.loads(yoriai._execute_read_file_tool_call({"filename": "storage.py"}, tool_context))
    assert result == {"filename": "storage.py", "exists": True, "content": "def add_todo():\n    pass\n"}, result
    assert tool_context["read_file_call_count"] == 1, tool_context


def test_execute_read_file_tool_call_reports_missing_file_as_not_implemented():
    """依頼の項目4: 存在しないファイル名を指定された場合、エラーにはせず
    「まだ実装されていません」という情報を正常な回答として返すことを確認する。
    """
    tool_context = {"available_files": {"storage.py": "..."}, "read_file_call_count": 0}
    result = json.loads(yoriai._execute_read_file_tool_call({"filename": "config.py"}, tool_context))
    assert result["exists"] is False, result
    assert "まだ存在しません" in result["message"], result


def test_execute_read_file_tool_call_is_capped_at_three_calls_per_review():
    """依頼の項目5: 1回のレビュー(=tool_contextを共有する1回の
    stream_chat_completion実行)につき、read_fileの呼び出しは最大3回までに
    制限されることを確認する。
    """
    tool_context = {"available_files": {"storage.py": "code"}, "read_file_call_count": 0}
    for _ in range(3):
        result = json.loads(yoriai._execute_read_file_tool_call({"filename": "storage.py"}, tool_context))
        assert "error" not in result, result

    fourth = json.loads(yoriai._execute_read_file_tool_call({"filename": "storage.py"}, tool_context))
    assert "error" in fourth, fourth
    assert "上限" in fourth["error"], fourth
    assert tool_context["read_file_call_count"] == 3, (
        f"上限到達後もカウンタが増え続けているべきではありません: {tool_context}"
    )


def test_execute_tool_call_dispatches_read_file_by_name_with_json_string_arguments():
    """LM Studio(OpenAI互換)ではtool_call.function.argumentsがJSON文字列で
    来ることが多いため、この形式でも正しくfilenameを取り出せることを確認する。
    """
    tool_context = {"available_files": {"cli.py": "import storage\n"}, "read_file_call_count": 0}
    tool_call = {"function": {"name": "read_file", "arguments": json.dumps({"filename": "cli.py"})}}
    result = json.loads(yoriai._execute_tool_call(tool_call, tool_context=tool_context))
    assert result == {"filename": "cli.py", "exists": True, "content": "import storage\n"}, result


def test_snapshot_available_files_reads_all_files_in_out_dir():
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")
        with open(os.path.join(out_dir, "cli.py"), "w", encoding="utf-8") as f:
            f.write("import storage\n")

        snapshot = yoriai._snapshot_available_files(out_dir)
        assert snapshot == {
            "storage.py": "def add_todo():\n    pass\n",
            "cli.py": "import storage\n",
        }, snapshot
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_snapshot_available_files_returns_empty_dict_for_missing_directory():
    assert yoriai._snapshot_available_files("/no/such/directory/at/all") == {}


def test_stream_chat_completion_executes_read_file_and_lets_model_revise_answer():
    """`stream_chat_completion`の実際のツール呼び出しループを通して、
    (1)モデルがread_fileを呼び出す、(2)ツールの実行結果(実際のファイルの
    中身)が会話履歴に追加されて次のラウンドに渡る、(3)モデルがその内容を
    踏まえて最終的な回答を返す、という一連の流れを検証する
    (依頼の動作確認: 「レビュアーが実際にstorage.pyを読みに行く様子が
    確認でき、その結果、より根拠のある判定に変わる」を模擬する)。

    実機のOllama/LM Studio/MLX-LMに接続できない環境のため、ワイヤレベルの
    ターン関数(`_stream_openai_compatible_turn`)を差し替える
    (tests/test_tools_fallback_logging.pyと同じ手法)。
    """
    rounds = {"n": 0}
    seen_tool_messages = []

    def fake_turn(base_url, model, messages, tools):
        rounds["n"] += 1
        if rounds["n"] == 1:
            # 1ラウンド目: モデルはstorage.pyの中身を確認したいと判断する。
            tool_names = {t["function"]["name"] for t in (tools or [])}
            assert yoriai.READ_FILE_TOOL_NAME in tool_names, f"read_fileツールがオファーされていません: {tools}"
            yield {
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "storage.py"}}},
                ],
            }
            return
        # 2ラウンド目: ツール実行結果(storage.pyの実際の中身)が会話履歴に
        # 追加されているはずなので、それを踏まえた最終回答を返す。
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        seen_tool_messages.extend(tool_messages)
        yield {"content": "問題なし(storage.pyの中身を確認し、config.pyの使い方が正しいことを確認しました)"}
        yield {"tool_calls": []}

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        tool_context = {"available_files": {"storage.py": "def add_todo():\n    from config import DB_PATH\n"}, "read_file_call_count": 0}
        events = list(yoriai.stream_chat_completion(
            "some-review-model",
            [{"role": "user", "content": "cli.pyをレビューしてください"}],
            extra_tools=[yoriai.READ_FILE_TOOL_SCHEMA],
            tool_context=tool_context,
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original

    tool_call_events = [e for e in events if e.get("tool_call") == yoriai.READ_FILE_TOOL_NAME]
    assert len(tool_call_events) == 1, f"read_fileのtool_callイベントが1回だけ観測されるはずです: {events}"

    assert len(seen_tool_messages) == 1, f"2ラウンド目にツール実行結果が1件渡っているはずです: {seen_tool_messages}"
    tool_result = json.loads(seen_tool_messages[0]["content"])
    assert tool_result["exists"] is True
    assert "DB_PATH" in tool_result["content"], (
        f"storage.pyの実際の中身がツール結果として渡っているはずです: {tool_result}"
    )

    final_contents = [e["content"] for e in events if "content" in e]
    assert any("問題なし" in c for c in final_contents), (
        f"storage.pyの中身を確認した後、最終的な判定が返るはずです: {events}"
    )
    assert tool_context["read_file_call_count"] == 1, tool_context


def test_review_one_file_passes_project_directory_snapshot_as_available_files():
    """`_review_one_file`が、レビュー依頼を送る際にout_dir配下の実際の
    ファイル一式をavailable_filesとして組み立てて渡していることを確認する
    (レビュー担当は自分の担当ファイルだけでなく、プロジェクト内の任意の
    ファイルを読めるべきという依頼の趣旨に対応)。
    """
    captured = {}

    def fake_stream(candidate, org_fingerprint, messages, available_files=None, **_kwargs):
        captured["available_files"] = available_files
        yield {"content": "問題なし"}
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")
        with open(os.path.join(out_dir, "cli.py"), "w", encoding="utf-8") as f:
            f.write("import storage\n")

        original_stream = yoriai._stream_chat_from_candidate
        yoriai._stream_chat_from_candidate = fake_stream
        try:
            yoriai._review_one_file(
                "cli.py", "import storage\n", _member("junnoMac-mini", "qwen2.5-coder-14b"),
                "cli.py", "import storage\n", "実装計画", "fingerprint", "1回目のレビュー", out_dir,
            )
        finally:
            yoriai._stream_chat_from_candidate = original_stream
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert captured["available_files"] == {
        "storage.py": "def add_todo():\n    pass\n",
        "cli.py": "import storage\n",
    }, captured


def test_collect_answer_from_candidate_shows_read_file_progress_message():
    """依頼の動作確認要件(ツール呼び出しのログが確認できる)を検証する。
    read_fileのtool_callイベントを受け取った際、対象のファイル名を含む
    進捗メッセージが画面に表示されることを確認する。
    """
    def fake_stream(candidate, org_fingerprint, messages, available_files=None, **_kwargs):
        yield {"tool_call": yoriai.READ_FILE_TOOL_NAME, "tool_call_arguments": json.dumps({"filename": "storage.py"})}
        yield {"content": "問題なし"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            answer, error = yoriai._collect_answer_from_candidate(
                _member("junnoMac-mini", "qwen2.5-coder-14b"), "fingerprint",
                [{"role": "user", "content": "レビューしてください"}], available_files={"storage.py": "code"},
            )
        output = buf.getvalue()
    finally:
        yoriai._stream_chat_from_candidate = original_stream

    assert error is None
    assert answer == "問題なし", answer
    assert "storage.py" in output and "読みに行って" in output, (
        f"read_fileの呼び出しが画面に表示されるはずです: {output!r}"
    )


def main():
    tests = [
        test_execute_read_file_tool_call_returns_content_for_existing_file,
        test_execute_read_file_tool_call_reports_missing_file_as_not_implemented,
        test_execute_read_file_tool_call_is_capped_at_three_calls_per_review,
        test_execute_tool_call_dispatches_read_file_by_name_with_json_string_arguments,
        test_snapshot_available_files_reads_all_files_in_out_dir,
        test_snapshot_available_files_returns_empty_dict_for_missing_directory,
        test_stream_chat_completion_executes_read_file_and_lets_model_revise_answer,
        test_review_one_file_passes_project_directory_snapshot_as_available_files,
        test_collect_answer_from_candidate_shows_read_file_progress_message,
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
