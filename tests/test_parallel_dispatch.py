#!/usr/bin/env python3
"""`//parallel` コマンド(実験1: 2台による並行モジュール開発)の検証。

//multi が「同じ質問」を複数メンバーに投げるのに対し、//parallel は
「異なる依頼を、異なるメンバーに同時に送り、各回答からコード部分を
抽出して手元に保存する」ための専用コマンド。ここでは:

1. タスク解析(`//parallel storage.py:... | cli.py:...`)が正しく分解されること
2. 依頼が異なるメンバーへ正しく振り分けられること(取り違えが起きないこと)
3. 各回答からコードブロックの中身だけが抽出され、指定ディレクトリに
   ファイル名で保存されること
4. 候補がタスク数より少ない場合に、スキップした旨を伝えつつ実行できる分だけ
   実行すること

を検証する。実機のOllama/LM Studio/MLX-LMには接続できない環境のため、
`_fetch_org_snapshot`と`_stream_chat_from_candidate`を差し替えて、
実際のメンバー2台(storage.py担当・cli.py担当)を模擬する。

使い方: python3 tests/test_parallel_dispatch.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


_STORAGE_CODE = '''import json
import os

_STORAGE_PATH = "todos.json"


def _load():
    if not os.path.exists(_STORAGE_PATH):
        return []
    with open(_STORAGE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(todos):
    with open(_STORAGE_PATH, "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)


def add_todo(text: str) -> int:
    todos = _load()
    new_id = (max((t["id"] for t in todos), default=0)) + 1
    todos.append({"id": new_id, "text": text, "done": False})
    _save(todos)
    return new_id


def list_todos() -> list:
    return _load()


def complete_todo(id: int) -> bool:
    todos = _load()
    for t in todos:
        if t["id"] == id:
            t["done"] = True
            _save(todos)
            return True
    return False


def delete_todo(id: int) -> bool:
    todos = _load()
    new_todos = [t for t in todos if t["id"] != id]
    if len(new_todos) == len(todos):
        return False
    _save(new_todos)
    return True
'''

_CLI_CODE = '''import sys

import storage


def main():
    if len(sys.argv) < 2:
        print("使い方: python cli.py <add|list|done|delete> [引数]")
        return

    command = sys.argv[1]
    if command == "add":
        new_id = storage.add_todo(sys.argv[2])
        print(f"追加しました (id={new_id})")
    elif command == "list":
        for t in storage.list_todos():
            mark = "x" if t["done"] else " "
            print(f"[{mark}] {t['id']}: {t['text']}")
    elif command == "done":
        ok = storage.complete_todo(int(sys.argv[2]))
        print("完了にしました" if ok else "見つかりませんでした")
    elif command == "delete":
        ok = storage.delete_todo(int(sys.argv[2]))
        print("削除しました" if ok else "見つかりませんでした")
    else:
        print(f"不明なコマンドです: {command}")


if __name__ == "__main__":
    main()
'''


def _make_card(device_name, model, free_gb):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 64},
        "models": {"installed": [model], "loaded": [model], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _fake_snapshot_two_members():
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peer_card = _make_card("MacMini", "qwen2.5-coder-7b", free_gb=20)
    return {
        "self": self_card,
        "peers": [{"card": peer_card, "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0}],
    }


def _with_patched_dispatch(snapshot_fn, response_by_label, fn):
    """_fetch_org_snapshot と _stream_chat_from_candidate を差し替えて、
    実機のネットワーク越しの問い合わせを行わずに //parallel の分配ロジックだけを
    検証できるようにする。response_by_label は {ラベル: (期待する依頼文の一部, 返す回答)}。
    実際に届いた依頼文が期待通りかどうかもここで検証し、取り違えがあれば例外を出す。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    def fake_stream(candidate, org_fingerprint, messages):
        expected_substring, canned_answer = response_by_label[candidate["label"]]
        actual_request = messages[0]["content"]
        assert expected_substring in actual_request, (
            f"{candidate['label']} に届いた依頼が想定と異なります: {actual_request!r}"
        )
        yield {"content": canned_answer}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: snapshot_fn()
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        return fn()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream


def test_two_tasks_are_routed_to_two_distinct_members_and_saved():
    """storage.py と cli.py の依頼が、それぞれ異なるメンバー(MacStudio/MacMini)に
    正しく振り分けられ、各回答からコードだけが抽出されてファイルに保存されることを確認する。
    """
    response_by_label = {
        "MacStudio(自分)": ("ToDoをJSONで管理する", f"以下の通りです。\n```python\n{_STORAGE_CODE}```\n"),
        "MacMini": ("storageを使うCLIを書いて", f"了解しました。\n```python\n{_CLI_CODE}```\n"),
    }

    out_dir = tempfile.mkdtemp(prefix="yoriai_parallel_test_")
    try:
        _with_patched_dispatch(
            _fake_snapshot_two_members,
            response_by_label,
            lambda: yoriai._ask_organization_parallel(
                47120, "fingerprint",
                "storage.py:ToDoをJSONで管理する関数を書いて | cli.py:storageを使うCLIを書いて",
                out_dir,
            ),
        )

        storage_path = os.path.join(out_dir, "storage.py")
        cli_path = os.path.join(out_dir, "cli.py")
        assert os.path.exists(storage_path), f"storage.pyが保存されていません: {os.listdir(out_dir)}"
        assert os.path.exists(cli_path), f"cli.pyが保存されていません: {os.listdir(out_dir)}"

        with open(storage_path, encoding="utf-8") as f:
            saved_storage = f.read()
        with open(cli_path, encoding="utf-8") as f:
            saved_cli = f.read()

        assert "def add_todo" in saved_storage and "```" not in saved_storage, (
            "storage.pyにコードブロック抽出後の余計な記号が残っています"
        )
        assert "import storage" in saved_cli and "```" not in saved_cli, (
            "cli.pyにコードブロック抽出後の余計な記号が残っています"
        )
        return out_dir
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise


def test_fewer_candidates_than_tasks_skips_remainder_with_warning():
    """メンバーが1台しかいない状況で2件のタスクを依頼した場合、
    先頭のタスクだけが実行され、もう片方はスキップされることを確認する。
    """
    def one_member_snapshot():
        self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
        return {"self": self_card, "peers": []}

    response_by_label = {
        "MacStudio(自分)": ("ToDoをJSONで管理する", f"```python\n{_STORAGE_CODE}```"),
    }

    out_dir = tempfile.mkdtemp(prefix="yoriai_parallel_test_")
    try:
        _with_patched_dispatch(
            one_member_snapshot,
            response_by_label,
            lambda: yoriai._ask_organization_parallel(
                47120, "fingerprint",
                "storage.py:ToDoをJSONで管理する関数を書いて | cli.py:storageを使うCLIを書いて",
                out_dir,
            ),
        )
        assert os.path.exists(os.path.join(out_dir, "storage.py")), "storage.pyは保存されるはずです"
        assert not os.path.exists(os.path.join(out_dir, "cli.py")), (
            "候補が足りないためcli.pyはスキップされるはずです"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_parse_parallel_tasks_splits_on_pipe():
    tasks = yoriai._parse_parallel_tasks("a.py:依頼A | b.py:依頼B")
    assert tasks == [("a.py", "依頼A"), ("b.py", "依頼B")], tasks


def test_parse_parallel_tasks_ignores_malformed_chunks():
    tasks = yoriai._parse_parallel_tasks("a.py:依頼A | コロン無し | b.py:依頼B")
    assert tasks == [("a.py", "依頼A"), ("b.py", "依頼B")], tasks


def test_extract_code_from_answer_returns_full_text_when_no_code_block():
    result = yoriai._extract_code_from_answer("コードブロックが無い回答です")
    assert result == "コードブロックが無い回答です\n", repr(result)


def test_generated_todo_cli_actually_works_end_to_end():
    """//parallel が保存したファイル(を模した、想定される回答内容)を使い、
    実際に python cli.py add/list/done/delete を実行して2つのモジュールが
    連携して動くことを確認する(ユーザー依頼の「動作確認」手順に対応)。
    """
    out_dir = test_two_tasks_are_routed_to_two_distinct_members_and_saved()
    try:
        import subprocess

        def run_cli(*args):
            result = subprocess.run(
                [sys.executable, "cli.py", *args],
                cwd=out_dir, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, f"cli.py {args} が失敗しました: {result.stderr}"
            return result.stdout

        out = run_cli("add", "テスト")
        assert "追加しました" in out, out

        out = run_cli("list")
        assert "テスト" in out, out

        out = run_cli("done", "1")
        assert "完了にしました" in out, out

        out = run_cli("list")
        assert "[x] 1: テスト" in out, out

        out = run_cli("delete", "1")
        assert "削除しました" in out, out

        out = run_cli("list")
        assert "テスト" not in out, out
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_parse_parallel_tasks_splits_on_pipe,
        test_parse_parallel_tasks_ignores_malformed_chunks,
        test_extract_code_from_answer_returns_full_text_when_no_code_block,
        test_two_tasks_are_routed_to_two_distinct_members_and_saved,
        test_fewer_candidates_than_tasks_skips_remainder_with_warning,
        test_generated_todo_cli_actually_works_end_to_end,
    ]
    failures = 0
    for test in tests:
        try:
            out_dir = test()
            if isinstance(out_dir, str) and os.path.isdir(out_dir):
                shutil.rmtree(out_dir, ignore_errors=True)
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
