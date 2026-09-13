#!/usr/bin/env python3
"""事前調査付きVisionエージェント(`//vision`)を検証する。

背景: 新しいツール/コンテンツの計画は、外部の参照点なしに「ゼロから」
立てられており、結果として実現可能性を無意識に織り込んだ小さい計画に
なりやすい。Vision agentは、実装の難易度・工数といった実現可能性を
一切考慮せず要件を洗い出す役割として、既存の`_classify_agree_request_
type`のような実装寄りの分類ロジックとは完全に切り離して追加した。
実行前にSearXNG経由(`web_search`)で類似OSSプロジェクト・関連GitHub
リポジトリを検索し、得られた検索結果をVision agentへのプロンプトに
含めることで、規模感のアンカーを与える。

ここでは(1)外部検索結果(モックレスポンス)がVision agentへの問い合わせ
メッセージに正しく注入されること、(2)検索結果が得られなかった場合も
その旨が明示されること、(3)生成されたバックログが指定した7つの機能
カテゴリを最低限カバーしているかどうかの検出ロジック
(`_vision_agent_backlog_missing_categories`、`_looks_like_process_
narration`と同様のキーワードベースの判定)、(4)`//vision`単体モード
(`_ask_organization_vision`)がVISION_BACKLOG.mdとして保存すること、を
確認する。

実機のOllama/LM Studio/MLX-LMやSearXNGインスタンスに依存しないよう、
`web_search`・`_collect_answer_from_candidate`を差し替えて模擬する。

使い方: python3 tests/test_vision_agent.py
"""
import contextlib
import io
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


_FULL_BACKLOG = """### コア機能
- ノートの作成・編集・削除ができる
- タグ付けができる

### エラーハンドリング
- 保存失敗時にエラーメッセージを表示する

### 設定管理
- 保存先ディレクトリを設定ファイルで変更できる

### ログ
- 操作履歴をログファイルに記録する

### CLI/UI
- コマンドラインから起動できる

### ドキュメント
- README.mdにセットアップ手順を記載する

### テスト
- 主要機能に単体テストを用意する
"""


# ---------------------------------------------------------------------------
# _build_vision_agent_search_queries / _format_external_references_for_prompt
# ---------------------------------------------------------------------------

def test_build_vision_agent_search_queries_covers_similar_oss_and_github():
    queries = yoriai._build_vision_agent_search_queries("メモアプリを作って")
    assert any("類似" in q and "OSS" in q for q in queries), queries
    assert any("GitHub" in q for q in queries), queries


def test_format_external_references_includes_title_url_and_snippet():
    search_results_by_query = {
        "メモアプリを作って 類似 OSS プロジェクト": [
            {"title": "Obsidian", "url": "https://obsidian.md/", "snippet": "ローカルファーストのメモアプリ"},
        ],
        "メモアプリを作って GitHub リポジトリ": [],
    }
    text = yoriai._format_external_references_for_prompt(search_results_by_query)
    assert "Obsidian" in text
    assert "https://obsidian.md/" in text
    assert "ローカルファーストのメモアプリ" in text
    assert "検索結果は得られませんでした" in text  # 2件目のクエリの空リストへの言及


def test_format_external_references_explicit_message_when_all_empty():
    text = yoriai._format_external_references_for_prompt({"q1": [], "q2": []})
    assert "一般的な知識に基づいて" in text


# ---------------------------------------------------------------------------
# _run_vision_agent: 外部検索結果のプロンプトへの注入
# ---------------------------------------------------------------------------

def test_run_vision_agent_injects_search_results_into_prompt():
    def fake_web_search(query, max_results=5):
        return [{"title": f"結果: {query}", "url": "https://example.com", "snippet": "参考になる機能一覧"}]

    captured_messages = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        captured_messages.append(messages)
        return _FULL_BACKLOG, None, False

    original_search = _patched(yoriai, "web_search", fake_web_search)
    original_collect = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        backlog = yoriai._run_vision_agent(_member("MacStudio", "qwen3-coder-30b"), "org-fp", "メモアプリを作って")
    finally:
        yoriai.web_search = original_search
        yoriai._collect_answer_from_candidate = original_collect

    assert backlog == _FULL_BACKLOG
    assert len(captured_messages) == 1
    prompt = captured_messages[0][0]["content"]
    assert "結果: メモアプリを作って 類似 OSS プロジェクト" in prompt
    assert "結果: メモアプリを作って GitHub リポジトリ" in prompt
    assert "https://example.com" in prompt
    assert "参考になる機能一覧" in prompt
    # 実現可能性を問わないことを明示していること
    assert "実現できるかどうかを一切気にせず" in prompt


def test_run_vision_agent_disables_web_search_tool_since_search_already_performed():
    """外部検索(web_search)はYoriai側が既に直接実行済みのため、Vision
    agent自身にはツールとしてweb_searchをオファーしない
    (disable_web_search=Trueが渡ること)ことを確認する。
    """
    calls = []

    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        calls.append(disable_web_search)
        return _FULL_BACKLOG, None, False

    original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
    original_collect = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        yoriai._run_vision_agent(_member("MacStudio", "qwen3-coder-30b"), "org-fp", "メモアプリを作って")
    finally:
        yoriai.web_search = original_search
        yoriai._collect_answer_from_candidate = original_collect

    assert calls == [True], calls


def test_run_vision_agent_returns_empty_string_on_error():
    def fake_collect(candidate, org_fingerprint, messages, disable_web_search=False, **kwargs):
        return "", "接続に失敗しました", False

    original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
    original_collect = _patched(yoriai, "_collect_answer_from_candidate", fake_collect)
    try:
        backlog = yoriai._run_vision_agent(_member("MacStudio", "qwen3-coder-30b"), "org-fp", "メモアプリを作って")
    finally:
        yoriai.web_search = original_search
        yoriai._collect_answer_from_candidate = original_collect

    assert backlog == ""


# ---------------------------------------------------------------------------
# _vision_agent_backlog_missing_categories: カテゴリ網羅性の検出
# ---------------------------------------------------------------------------

def test_backlog_missing_categories_empty_when_all_present():
    assert yoriai._vision_agent_backlog_missing_categories(_FULL_BACKLOG) == []


def test_backlog_missing_categories_detects_missing_headings():
    partial_backlog = "### コア機能\n- ノートの作成ができる\n\n### テスト\n- 単体テストを用意する\n"
    missing = yoriai._vision_agent_backlog_missing_categories(partial_backlog)
    assert "エラーハンドリング" in missing
    assert "設定管理" in missing
    assert "ログ" in missing
    assert "CLI/UI" in missing
    assert "ドキュメント" in missing
    assert "コア機能" not in missing
    assert "テスト" not in missing


def test_backlog_missing_categories_all_missing_for_empty_text():
    missing = yoriai._vision_agent_backlog_missing_categories("")
    assert set(missing) == set(yoriai._VISION_AGENT_REQUIRED_CATEGORIES)


# ---------------------------------------------------------------------------
# _ask_organization_vision: 単体モードとしての振る舞い
# ---------------------------------------------------------------------------

def _fake_org_snapshot(*_args, **_kwargs):
    return {"self": {"label": "MacStudio", "model": "qwen3-coder-30b"}, "peers": []}


def test_ask_organization_vision_saves_backlog_file_and_reports_no_warning():
    original_snapshot = _patched(yoriai, "_fetch_org_snapshot", _fake_org_snapshot)
    original_select = _patched(
        yoriai, "_select_chat_candidates",
        lambda self_info, peers, port, task_type: [_member("MacStudio", "qwen3-coder-30b")],
    )
    original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
    original_collect = _patched(
        yoriai, "_collect_answer_from_candidate",
        lambda candidate, org_fingerprint, messages, disable_web_search=False, **kwargs: (_FULL_BACKLOG, None, False),
    )
    out_dir = tempfile.mkdtemp(prefix="yoriai_vision_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_vision(47120, "org-fp", "メモアプリを作って", out_dir)
        output = buf.getvalue()

        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dirs = os.listdir(projects_root)
        assert len(project_dirs) == 1, project_dirs
        assert project_dirs[0].endswith("-vision"), project_dirs
        backlog_path = os.path.join(projects_root, project_dirs[0], yoriai.VISION_BACKLOG_FILENAME)
        with open(backlog_path, encoding="utf-8") as f:
            saved_backlog = f.read()
        assert saved_backlog == _FULL_BACKLOG
        assert "見出しが出力に含まれていません" not in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._select_chat_candidates = original_select
        yoriai.web_search = original_search
        yoriai._collect_answer_from_candidate = original_collect
        shutil.rmtree(out_dir, ignore_errors=True)


def test_ask_organization_vision_warns_when_categories_missing():
    partial_backlog = "### コア機能\n- ノートの作成ができる\n"
    original_snapshot = _patched(yoriai, "_fetch_org_snapshot", _fake_org_snapshot)
    original_select = _patched(
        yoriai, "_select_chat_candidates",
        lambda self_info, peers, port, task_type: [_member("MacStudio", "qwen3-coder-30b")],
    )
    original_search = _patched(yoriai, "web_search", lambda query, max_results=5: [])
    original_collect = _patched(
        yoriai, "_collect_answer_from_candidate",
        lambda candidate, org_fingerprint, messages, disable_web_search=False, **kwargs: (partial_backlog, None, False),
    )
    out_dir = tempfile.mkdtemp(prefix="yoriai_vision_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_vision(47120, "org-fp", "メモアプリを作って", out_dir)
        output = buf.getvalue()
        assert "見出しが出力に含まれていません" in output, output
        assert "エラーハンドリング" in output, output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._select_chat_candidates = original_select
        yoriai.web_search = original_search
        yoriai._collect_answer_from_candidate = original_collect
        shutil.rmtree(out_dir, ignore_errors=True)


def test_ask_organization_vision_reports_when_no_candidates():
    original_snapshot = _patched(yoriai, "_fetch_org_snapshot", _fake_org_snapshot)
    original_select = _patched(yoriai, "_select_chat_candidates", lambda self_info, peers, port, task_type: [])
    out_dir = tempfile.mkdtemp(prefix="yoriai_vision_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_vision(47120, "org-fp", "メモアプリを作って", out_dir)
        output = buf.getvalue()
        assert "ロード済みモデルを持つメンバーがいません" in output, output
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        assert not os.path.isdir(projects_root)
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._select_chat_candidates = original_select
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_build_vision_agent_search_queries_covers_similar_oss_and_github,
        test_format_external_references_includes_title_url_and_snippet,
        test_format_external_references_explicit_message_when_all_empty,
        test_run_vision_agent_injects_search_results_into_prompt,
        test_run_vision_agent_disables_web_search_tool_since_search_already_performed,
        test_run_vision_agent_returns_empty_string_on_error,
        test_backlog_missing_categories_empty_when_all_present,
        test_backlog_missing_categories_detects_missing_headings,
        test_backlog_missing_categories_all_missing_for_empty_text,
        test_ask_organization_vision_saves_backlog_file_and_reports_no_warning,
        test_ask_organization_vision_warns_when_categories_missing,
        test_ask_organization_vision_reports_when_no_candidates,
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
