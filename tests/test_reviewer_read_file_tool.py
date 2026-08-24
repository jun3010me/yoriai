#!/usr/bin/env python3
"""レビュー担当が、必要に応じて他のファイルの実際の中身をread_fileツールで
確認できる機能(および、そのfreshness修正)を検証する。

実機で、cli.pyのレビュー時に「storage.pyがconfig.pyを正しく使っているか
確認できない」という指摘が出た件を受けて、レビュー担当にread_fileという
関数呼び出し(function calling)の選択肢を与える機能を最初に追加した。
その最初の実装は、レビュー依頼のリクエストボディに「その時点でプロジェクト
に実際に保存されているファイルの中身」を丸ごとスナップショットとして
埋め込み、read_fileの実行(_execute_tool_call)はレビュー担当自身の
プロセス内でそのスナップショットを参照するだけ、という設計だった。

しかしこれには不具合があった: スナップショットは依頼発行(=リクエスト
送信)の瞬間に固定されてしまうため、LLMの応答生成中(read_fileが実際に
呼ばれるまでの間、ローカルLLMでは数秒〜数十秒かかりうる)に他のワーカー
スレッドが新しいファイルを完成させても、その更新が一切反映されない。
実機で、2回目のレビュー(修正後の再レビュー)の時点では実際には既に
完成していたconfig.pyが「存在しない」と誤判定され、無駄にレビュー往復の
上限を使い切ってタスクが未完了のまま打ち切られる不具合が報告された。

修正後は、read_fileを「レビュー担当のプロセス内では実行しない、
呼び出し元(実装依頼元、プロジェクトの実際のファイルへのアクセス権を
持つ側)が引き取って実行するツール」として再設計した:
- `stream_chat_completion`は、read_file(client_tool_namesに含まれる
  ツール)が要求されたラウンドでは実行せず`{"pending_tool_calls": [...]}`
  をyieldして処理を打ち切り、呼び出し元に実行を委ねる。
- 実装依頼元の`_collect_review_answer_with_read_file`が、pending_tool_calls
  を受け取ったら`_read_project_file_fresh`でその瞬間のout_dirを直接
  読み直し、結果を会話履歴に追加した新しいmessagesで/chatを呼び直す
  (/chatは毎回messages全体を送るステートレスな設計のため、この
  「続きから再開する」やり取りが自然に行える)。

このファイルでは、(1)`_read_project_file_fresh`本体(存在するファイル・
存在しないファイル・パストラバーサル対策)、(2)`stream_chat_completion`が
read_fileの要求されたラウンドで実行を打ち切りpending_tool_callsを
yieldすること、(3)`_collect_review_answer_with_read_file`が実際に
呼び出し間でout_dirを読み直す(freshnessの回帰再現)こと、
(4)呼び出し回数の上限、(5)`_review_one_file`との結線、(6)画面表示、
を検証する。

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


def test_read_project_file_fresh_returns_content_for_existing_file():
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "storage.py"))
        assert result == {"filename": "storage.py", "exists": True, "content": "def add_todo():\n    pass\n"}, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_reports_missing_file_as_not_implemented():
    """依頼の項目4: 存在しないファイル名を指定された場合、エラーにはせず
    「まだ実装されていません」という情報を正常な回答として返すことを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "config.py"))
        assert result["exists"] is False, result
        assert "まだ存在しません" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_reflects_file_created_after_first_check():
    """バグ1の直接の回帰再現: 1回目の呼び出し時点では存在しなかった
    ファイルが、その後(同じレビューの往復の間に)完成した場合、
    再度呼び出すと最新の内容が正しく反映されることを確認する
    (依頼発行時に固定したスナップショットを使い回さないことの検証)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        first = json.loads(yoriai._read_project_file_fresh(out_dir, "config.py"))
        assert first["exists"] is False, first

        # 別スレッド(実装フェーズのワーカー)がこのタイミングでファイルを完成させる想定。
        with open(os.path.join(out_dir, "config.py"), "w", encoding="utf-8") as f:
            f.write("DB_PATH = 'todos.json'\n")

        second = json.loads(yoriai._read_project_file_fresh(out_dir, "config.py"))
        assert second == {"filename": "config.py", "exists": True, "content": "DB_PATH = 'todos.json'\n"}, second
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_rejects_path_traversal():
    """モデルが../等を含むファイル名を指定しても、out_dir外のファイルを
    読めないことを確認する(os.path.basenameによる安全対策)。
    """
    outer_dir = tempfile.mkdtemp(prefix="yoriai_read_file_outer_")
    out_dir = os.path.join(outer_dir, "project")
    os.makedirs(out_dir)
    try:
        secret_path = os.path.join(outer_dir, "secret.txt")
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write("SECRET")

        result = json.loads(yoriai._read_project_file_fresh(out_dir, "../secret.txt"))
        assert result["exists"] is False, (
            f"out_dir外のファイルを読めてしまっています(パストラバーサル対策の不備): {result}"
        )
    finally:
        shutil.rmtree(outer_dir, ignore_errors=True)


def test_truncate_file_content_leaves_short_content_unchanged():
    content, truncated = yoriai._truncate_file_content("hello", limit=100)
    assert content == "hello"
    assert truncated is False


def test_truncate_file_content_truncates_long_content():
    content, truncated = yoriai._truncate_file_content("x" * 200, limit=100)
    assert content == "x" * 100
    assert truncated is True


def test_read_project_file_fresh_truncates_large_files():
    """調査依頼への対応: 複雑な依頼(HTML/CSS学習サイト等)で1ファイルの
    サイズが大きい場合に、read_fileがその内容を丸ごと返すとトークン
    使用量が膨らむ問題への対策(_FILE_CONTENT_TRUNCATE_CHARSによる上限)
    を確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        big_content = "a" * (yoriai._FILE_CONTENT_TRUNCATE_CHARS + 500)
        with open(os.path.join(out_dir, "big.html"), "w", encoding="utf-8") as f:
            f.write(big_content)
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "big.html"))
        assert result["exists"] is True, result
        assert len(result["content"]) == yoriai._FILE_CONTENT_TRUNCATE_CHARS, len(result["content"])
        assert result["truncated"] is True, result
        assert "message" in result, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_does_not_truncate_small_files():
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        with open(os.path.join(out_dir, "small.py"), "w", encoding="utf-8") as f:
            f.write("def f():\n    pass\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "small.py"))
        assert "truncated" not in result, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_stream_chat_completion_yields_pending_tool_calls_for_client_tool_without_executing():
    """`stream_chat_completion`が、client_tool_namesに含まれるツール
    (read_file)が要求されたラウンドでは、自前で実行(_execute_tool_call)
    せずに{"pending_tool_calls": ...}をyieldして即座に終了することを
    確認する。実機のOllama/LM Studio/MLX-LMに接続できない環境のため、
    ワイヤレベルのターン関数(`_stream_openai_compatible_turn`)を差し替える。
    """
    def fake_turn(base_url, model, messages, tools):
        tool_names = {t["function"]["name"] for t in (tools or [])}
        assert yoriai.READ_FILE_TOOL_NAME in tool_names, f"read_fileツールがオファーされていません: {tools}"
        yield {
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "storage.py"}}},
            ],
        }

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        events = list(yoriai.stream_chat_completion(
            "some-review-model",
            [{"role": "user", "content": "cli.pyをレビューしてください"}],
            extra_tools=[yoriai.READ_FILE_TOOL_SCHEMA],
            client_tool_names={yoriai.READ_FILE_TOOL_NAME},
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original

    assert len(events) == 1 and "pending_tool_calls" in events[0], (
        f"read_file要求時はpending_tool_callsを1件だけyieldして終了するはずです: {events}"
    )
    assert events[0]["pending_tool_calls"][0]["function"]["name"] == "read_file"
    # {"done": True}は出ない(呼び出し元が実行するまで、このラウンドは未完了のため)。


def test_collect_review_answer_with_read_file_rereads_disk_between_rounds():
    """バグ1の本丸: `_collect_review_answer_with_read_file`が、1回目の
    read_file呼び出しと2回目の呼び出しの間に他のスレッドが完成させた
    ファイルを、都度ディスクから読み直すことで正しく反映することを確認する。
    実機のOllama/LM Studio/MLX-LMに接続できない環境のため、
    `_stream_chat_from_candidate`を差し替えてメンバーを模擬する。
    """
    call_log = []
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")

    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        call_log.append(len(messages))
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if len(tool_messages) == 0:
            # 1回目: config.pyの中身を確認したい(この時点ではまだ存在しない)。
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "config.py"}}},
            ]}
            return
        if len(tool_messages) == 1:
            first_result = json.loads(tool_messages[0]["content"])
            assert first_result["exists"] is False, f"1回目の時点ではconfig.pyはまだ存在しないはずです: {first_result}"
            # ここで、別のワーカースレッドが1回目の呼び出しの直後に
            # config.pyを完成させた、という状況を決定的に模擬する
            # (実際のディスク書き込みを、次のread_file呼び出しより
            # 前に行う点だけが重要で、実時間の経過そのものは本質ではない)。
            with open(os.path.join(out_dir, "config.py"), "w", encoding="utf-8") as f:
                f.write("DB_PATH = 'todos.json'\n")
            yield {"pending_tool_calls": [
                {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "config.py"}}},
            ]}
            return
        second_result = json.loads(tool_messages[1]["content"])
        assert second_result["exists"] is True, f"2回目の時点では既に完成しているはずです: {second_result}"
        assert "DB_PATH" in second_result["content"]
        yield {"content": "問題なし(config.pyの内容を確認しました)"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        answer, error, truncated = yoriai._collect_review_answer_with_read_file(
            _member("junnoMac-mini", "qwen2.5-coder-14b"), "fingerprint",
            [{"role": "user", "content": "cli.pyをレビューしてください"}], out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert error is None, error
    assert "問題なし" in answer, answer
    assert truncated is False
    assert len(call_log) == 3, f"初回+2回のread_file往復で合計3回/chatを呼ぶはずです: {call_log}"


def test_collect_review_answer_with_read_file_caps_at_max_calls():
    """1回のレビューにつきread_fileの呼び出しは最大3回までで、それ以上は
    上限メッセージが返り、実際のディスク読み取り(_read_project_file_fresh)
    は行われないことを確認する。モデルは4回目の要求が上限メッセージで
    返ってきたことを踏まえて、そこで最終回答を出す(現実的なモデルの
    振る舞いを模擬する)。
    """
    read_calls = {"n": 0}
    original_read = yoriai._read_project_file_fresh

    def counting_read(out_dir, filename, *args, **kwargs):
        read_calls["n"] += 1
        return original_read(out_dir, filename, *args, **kwargs)

    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if len(tool_messages) < 4:
            # 4回目まではリクエストしてみる(上限3回を超える)。
            yield {"pending_tool_calls": [
                {"id": f"call_{len(tool_messages)}", "type": "function", "function": {"name": "read_file", "arguments": {"filename": "storage.py"}}},
            ]}
            return
        # 4回目の要求が上限メッセージで返ってきたので、それを踏まえて最終回答を出す。
        yield {"content": "問題なし"}
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    yoriai._read_project_file_fresh = counting_read
    try:
        answer, error, truncated = yoriai._collect_review_answer_with_read_file(
            _member("junnoMac-mini", "qwen2.5-coder-14b"), "fingerprint",
            [{"role": "user", "content": "storage.pyをレビューしてください"}], out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        yoriai._read_project_file_fresh = original_read
        shutil.rmtree(out_dir, ignore_errors=True)

    assert read_calls["n"] == 3, f"実際のディスク読み取りは最大3回までのはずです: {read_calls}"
    assert error is None, error
    assert "問題なし" in answer, answer


def test_review_one_file_uses_read_file_powered_review_answer():
    """`_review_one_file`が`_collect_review_answer_with_read_file`
    (out_dirを都度読み直す経路)を使ってレビューを依頼していることを確認する。
    あわせて、並行スレッドの出力の判別可能性の修正(バグ2)とのマージ後、
    `print_lock`/`tag`(タグはファイル名)が正しく渡されることも確認する。
    """
    captured = {}

    def fake_collect(candidate, org_fingerprint, messages, out_dir, print_lock=None, tag=None):
        captured["out_dir"] = out_dir
        captured["prompt"] = messages[0]["content"]
        captured["tag"] = tag
        return "問題なし", None, False

    original = yoriai._collect_review_answer_with_read_file
    yoriai._collect_review_answer_with_read_file = fake_collect
    try:
        ok, feedback = yoriai._review_one_file(
            "cli.py", "import storage\n", _member("junnoMac-mini", "qwen2.5-coder-14b"),
            "cli.py", "import storage\n", "実装計画", "fingerprint", "1回目のレビュー", "/tmp/some-project-dir",
        )
    finally:
        yoriai._collect_review_answer_with_read_file = original

    assert ok is True and feedback == "", (ok, feedback)
    assert captured["out_dir"] == "/tmp/some-project-dir", captured
    assert "cli.py" in captured["prompt"], captured
    assert captured["tag"] == "cli.py", captured


def test_review_one_file_truncates_large_reviewer_own_code_in_prompt():
    """調査依頼への対応: レビュー担当自身の担当ファイル(参考として
    プロンプトに直接埋め込まれる`reviewer_own_code`)が大きい場合も、
    read_file経由のファイルと同じくトークン使用量対策で切り詰められる
    ことを確認する。一方、レビュー対象そのもの(`code`)は切り詰めず
    全体をそのままプロンプトに含めることも合わせて確認する。
    """
    captured = {}

    def fake_collect(candidate, org_fingerprint, messages, out_dir, print_lock=None, tag=None):
        captured["prompt"] = messages[0]["content"]
        return "問題なし", None, False

    original = yoriai._collect_review_answer_with_read_file
    yoriai._collect_review_answer_with_read_file = fake_collect
    try:
        big_own_code = "b" * (yoriai._FILE_CONTENT_TRUNCATE_CHARS + 500)
        review_target_code = "z" * (yoriai._FILE_CONTENT_TRUNCATE_CHARS + 500)
        yoriai._review_one_file(
            "app.py", review_target_code, _member("junnoMac-mini", "qwen2.5-coder-14b"),
            "index.html", big_own_code, "実装計画", "fingerprint", "1回目のレビュー", "/tmp/some-project-dir",
        )
    finally:
        yoriai._collect_review_answer_with_read_file = original

    prompt = captured["prompt"]
    assert prompt.count("b") == yoriai._FILE_CONTENT_TRUNCATE_CHARS, (
        "reviewer_own_codeは上限文字数までに切り詰められるはずです"
    )
    assert "省略" in prompt, prompt
    assert prompt.count("z") == len(review_target_code), (
        "レビュー対象そのもの(code)は切り詰めずに全体を渡すはずです"
    )


def test_collect_review_answer_with_read_file_shows_read_progress_message():
    """依頼の動作確認要件(ツール呼び出しのログが確認できる)を検証する。"""
    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if not tool_messages:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps({"filename": "storage.py"})}},
            ]}
            return
        yield {"content": "問題なし"}
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            answer, error, truncated = yoriai._collect_review_answer_with_read_file(
                _member("junnoMac-mini", "qwen2.5-coder-14b"), "fingerprint",
                [{"role": "user", "content": "レビューしてください"}], out_dir,
            )
        output = buf.getvalue()
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert error is None
    assert answer == "問題なし", answer
    assert "storage.py" in output and "読みに行って" in output, (
        f"read_fileの呼び出しが画面に表示されるはずです: {output!r}"
    )


def test_read_project_file_fresh_returns_range_with_line_numbers():
    """依頼の項目2: start_line/end_lineを指定すると、その範囲だけを
    「行番号: 内容」形式で返すことを確認する(省略時は従来通り全体)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        lines = [f"line{i}" for i in range(1, 21)]
        with open(os.path.join(out_dir, "big.py"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "big.py", 5, 8))
        assert result["exists"] is True, result
        assert result["start_line"] == 5 and result["end_line"] == 8, result
        assert result["total_lines"] == 20, result
        assert result["content"] == "5: line5\n6: line6\n7: line7\n8: line8", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_line_and_context_lines():
    """`line`+`context_lines`(開始行・終了行の代わりに「この行から前後N行」)
    指定でも範囲読みができることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        lines = [f"line{i}" for i in range(1, 21)]
        with open(os.path.join(out_dir, "big.py"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "big.py", None, None, 10, 2))
        assert result["start_line"] == 8 and result["end_line"] == 12, result
        assert result["content"] == "8: line8\n9: line9\n10: line10\n11: line11\n12: line12", result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_read_project_file_fresh_caps_overly_wide_line_range():
    out_dir = tempfile.mkdtemp(prefix="yoriai_read_file_test_")
    try:
        lines = [f"line{i}" for i in range(1, 1000)]
        with open(os.path.join(out_dir, "huge.py"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        result = json.loads(yoriai._read_project_file_fresh(out_dir, "huge.py", 1, 999))
        assert result["end_line"] - result["start_line"] + 1 == yoriai._READ_FILE_MAX_LINES_PER_CALL, result
        assert "message" in result, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_search_in_project_file_returns_matched_lines_with_excerpts():
    """依頼の項目1: search_in_fileが一致行番号と前後の抜粋を返すことを確認する。"""
    out_dir = tempfile.mkdtemp(prefix="yoriai_search_in_file_test_")
    try:
        lines = [f"line{i}" for i in range(1, 21)]
        lines[9] = "def target_function():"
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        result = json.loads(yoriai._search_in_project_file(out_dir, "storage.py", "target_function"))
        assert result["exists"] is True, result
        assert result["match_count"] == 1, result
        assert result["matched_lines"] == [10], result
        excerpt = result["excerpts"][0]["excerpt"]
        assert "10: def target_function():" in excerpt, excerpt
        assert "5: line5" in excerpt and "15: line15" in excerpt, excerpt
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_search_in_project_file_reports_no_match():
    out_dir = tempfile.mkdtemp(prefix="yoriai_search_in_file_test_")
    try:
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")
        result = json.loads(yoriai._search_in_project_file(out_dir, "storage.py", "nonexistent_keyword"))
        assert result["exists"] is True and result["match_count"] == 0, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_search_in_project_file_reports_missing_file():
    out_dir = tempfile.mkdtemp(prefix="yoriai_search_in_file_test_")
    try:
        result = json.loads(yoriai._search_in_project_file(out_dir, "config.py", "DB_PATH"))
        assert result["exists"] is False, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_review_offers_search_in_file_tool_alongside_read_file():
    """レビュー専用のツールオファーに、read_fileに加えてsearch_in_fileも
    含まれることを確認する(依頼の項目1・2: 「まず検索してから部分的に
    読む」2段階アプローチをレビューフェーズでも使えるようにする)。
    """
    def fake_turn(base_url, model, messages, tools):
        tool_names = {t["function"]["name"] for t in (tools or [])}
        assert yoriai.SEARCH_IN_FILE_TOOL_NAME in tool_names, f"search_in_fileがオファーされていません: {tools}"
        yield {"content": "問題なし"}
        yield {"done": True}

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        list(yoriai.stream_chat_completion(
            "some-review-model",
            [{"role": "user", "content": "cli.pyをレビューしてください"}],
            extra_tools=[yoriai.READ_FILE_TOOL_SCHEMA, yoriai.SEARCH_IN_FILE_TOOL_SCHEMA],
            client_tool_names={yoriai.READ_FILE_TOOL_NAME, yoriai.SEARCH_IN_FILE_TOOL_NAME},
        ))
    finally:
        yoriai._stream_openai_compatible_turn = original


def test_collect_review_answer_with_read_file_dispatches_search_in_file():
    """`_collect_review_answer_with_read_file`がsearch_in_file要求も
    (read_fileと同じ呼び出し元実行の経路で)正しく処理することを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_search_in_file_test_")

    def fake_stream(candidate, org_fingerprint, messages, offer_read_file_tool=False, **_kwargs):
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if not tool_messages:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "search_in_file", "arguments": {"filename": "storage.py", "query": "add_todo"},
                }},
            ]}
            return
        result = json.loads(tool_messages[0]["content"])
        assert result["exists"] is True and result["match_count"] == 1, result
        yield {"content": "問題なし"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        with open(os.path.join(out_dir, "storage.py"), "w", encoding="utf-8") as f:
            f.write("def add_todo():\n    pass\n")
        answer, error, truncated = yoriai._collect_review_answer_with_read_file(
            _member("junnoMac-mini", "qwen2.5-coder-14b"), "fingerprint",
            [{"role": "user", "content": "storage.pyをレビューしてください"}], out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert error is None, error
    assert "問題なし" in answer, answer
    assert truncated is False


def main():
    tests = [
        test_read_project_file_fresh_returns_content_for_existing_file,
        test_read_project_file_fresh_reports_missing_file_as_not_implemented,
        test_read_project_file_fresh_reflects_file_created_after_first_check,
        test_read_project_file_fresh_rejects_path_traversal,
        test_truncate_file_content_leaves_short_content_unchanged,
        test_truncate_file_content_truncates_long_content,
        test_read_project_file_fresh_truncates_large_files,
        test_read_project_file_fresh_does_not_truncate_small_files,
        test_read_project_file_fresh_returns_range_with_line_numbers,
        test_read_project_file_fresh_line_and_context_lines,
        test_read_project_file_fresh_caps_overly_wide_line_range,
        test_search_in_project_file_returns_matched_lines_with_excerpts,
        test_search_in_project_file_reports_no_match,
        test_search_in_project_file_reports_missing_file,
        test_review_offers_search_in_file_tool_alongside_read_file,
        test_collect_review_answer_with_read_file_dispatches_search_in_file,
        test_stream_chat_completion_yields_pending_tool_calls_for_client_tool_without_executing,
        test_collect_review_answer_with_read_file_rereads_disk_between_rounds,
        test_collect_review_answer_with_read_file_caps_at_max_calls,
        test_review_one_file_uses_read_file_powered_review_answer,
        test_review_one_file_truncates_large_reviewer_own_code_in_prompt,
        test_collect_review_answer_with_read_file_shows_read_progress_message,
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
