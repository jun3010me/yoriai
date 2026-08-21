#!/usr/bin/env python3
"""協業モードの生成物を Yoriai 本体から分離する仕組み
(projects/<プロジェクト名>/ への保存)を検証する。

これまで協業モードの生成物は`--dir`で指定したディレクトリ直下に
そのまま保存されており、Yoriai本体のファイル(config.py等)と混ざる・
名前が衝突するリスクがあった。この変更で、生成物は必ず
`<--dirで指定したディレクトリ>/projects/<プロジェクト名>/`にまとめて
保存されるようになる。

使い方: python3 tests/test_project_output_dir.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_generate_project_name_extracts_ascii_tokens_from_request():
    """依頼の例(「ToDoリストのCLIツールを作って」)で、期待通り
    'todo-cli' が生成されることを確認する。
    """
    assert yoriai._generate_project_name("ToDoリストのCLIツールを作って") == "todo-cli"


def test_generate_project_name_falls_back_to_default_without_ascii_tokens():
    """依頼文にASCII英数字のまとまりが全く無い場合、既定の名前
    ('project')にフォールバックすることを確認する。"""
    assert yoriai._generate_project_name("何かいい感じのものを作ってほしい") == "project"


def test_generate_project_name_is_length_limited():
    """トークン数・全体長に上限があり、極端に長い名前にならないことを
    確認する。"""
    request = "作って " + " ".join(f"Token{i}" for i in range(20))
    name = yoriai._generate_project_name(request)
    assert len(name) <= 40, name
    assert name.count("-") <= 3, f"最大4トークンまでのはずです: {name}"


def test_resolve_project_dir_returns_base_name_when_unused():
    root = tempfile.mkdtemp(prefix="yoriai_projects_test_")
    try:
        result = yoriai._resolve_project_dir(root, "todo-cli")
        assert result == os.path.join(root, "todo-cli"), result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resolve_project_dir_avoids_overwriting_existing_projects():
    """同名のプロジェクトが既に存在する場合、連番を振って既存の生成物を
    上書きしないことを確認する。"""
    root = tempfile.mkdtemp(prefix="yoriai_projects_test_")
    try:
        os.makedirs(os.path.join(root, "todo-cli"))
        result2 = yoriai._resolve_project_dir(root, "todo-cli")
        assert result2 == os.path.join(root, "todo-cli-2"), result2

        os.makedirs(result2)
        result3 = yoriai._resolve_project_dir(root, "todo-cli")
        assert result3 == os.path.join(root, "todo-cli-3"), result3
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _make_card(device_name, model, free_gb):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 64},
        "models": {"installed": [model], "loaded": [model], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _two_member_snapshot():
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peer_card = _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20)
    return {
        "self": self_card,
        "peers": [{"card": peer_card, "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0}],
    }


def _fake_stream_for_todo_cli(candidate, org_fingerprint, messages):
    request_text = messages[0]["content"]
    if "ファイルに分割する実装計画" in request_text:
        yield {"content": "storage.py: add_todo(text: str) -> int を実装\ncli.py: storage.add_todoを呼び出すCLI\n"}
    elif "レビュー対象" in request_text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_collaborate_saves_generated_files_under_projects_subdir_only():
    """協業モードの生成物が、--dirで指定したディレクトリの直下ではなく
    projects/<プロジェクト名>/配下にのみ保存され、直下(Yoriai本体を
    模したディレクトリ)には生成物が一切残らないことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_for_todo_cli

    out_dir = tempfile.mkdtemp(prefix="yoriai_repo_root_test_")
    try:
        # Yoriai本体を模して、既存のconfig.pyがあるディレクトリという想定にする。
        with open(os.path.join(out_dir, "config.py"), "w", encoding="utf-8") as f:
            f.write("# Yoriai本体のconfig.py(生成物のconfig.pyと衝突してはいけない)\n")

        yoriai._ask_organization_collaborate(
            47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir,
        )

        # 直下には元からあったconfig.pyと、projects/ディレクトリだけが
        # 存在するはず(storage.py/cli.pyが直下に漏れ出していないこと)。
        assert set(os.listdir(out_dir)) == {"config.py", "projects"}, (
            f"生成物がYoriai本体のディレクトリ直下に漏れ出しています: {os.listdir(out_dir)}"
        )
        with open(os.path.join(out_dir, "config.py"), encoding="utf-8") as f:
            assert "Yoriai本体" in f.read(), "本体のconfig.pyが上書きされています"

        project_dir = os.path.join(out_dir, "projects", "todo-cli")
        assert set(os.listdir(project_dir)) == {"storage.py", "cli.py"}, os.listdir(project_dir)
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collaborate_does_not_overwrite_existing_project_on_rerun():
    """同じ依頼(同じプロジェクト名になる依頼)を2回実行しても、1回目の
    生成物が上書きされず、2回目は連番のディレクトリに保存されることを
    確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_for_todo_cli

    out_dir = tempfile.mkdtemp(prefix="yoriai_repo_root_test_")
    try:
        yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)
        yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)

        projects_root = os.path.join(out_dir, "projects")
        assert set(os.listdir(projects_root)) == {"todo-cli", "todo-cli-2"}, os.listdir(projects_root)
        assert set(os.listdir(os.path.join(projects_root, "todo-cli"))) == {"storage.py", "cli.py"}
        assert set(os.listdir(os.path.join(projects_root, "todo-cli-2"))) == {"storage.py", "cli.py"}
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_completion_message_shows_actual_save_path():
    """実装完了・レビュー完了のログに、実際の保存先パス
    (projects/<プロジェクト名>/を含むパス)が表示されることを確認する。
    """
    import contextlib
    import io

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = _fake_stream_for_todo_cli

    out_dir = tempfile.mkdtemp(prefix="yoriai_repo_root_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)
        output = buf.getvalue()

        project_dir = os.path.join(out_dir, "projects", "todo-cli")
        assert project_dir in output, f"保存先パスがログに表示されていません: {output}"
        assert os.path.join(project_dir, "storage.py") in output, output
        assert os.path.join(project_dir, "cli.py") in output, output
        assert f"レビュー完了 (保存先: {project_dir})" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_generate_project_name_extracts_ascii_tokens_from_request,
        test_generate_project_name_falls_back_to_default_without_ascii_tokens,
        test_generate_project_name_is_length_limited,
        test_resolve_project_dir_returns_base_name_when_unused,
        test_resolve_project_dir_avoids_overwriting_existing_projects,
        test_collaborate_saves_generated_files_under_projects_subdir_only,
        test_collaborate_does_not_overwrite_existing_project_on_rerun,
        test_completion_message_shows_actual_save_path,
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
