#!/usr/bin/env python3
"""1ファイル分のレビュー→(必要なら)修正→再レビューのサイクル
(`_review_and_fix_one_file`)を検証する。

以前はこのサイクルを複数ファイルにまとめて適用する`_run_review_phase`
(実装済みファイルへの静的な円環割り当てで1回だけレビューする関数)経由で
テストしていたが、タスクキュー方式への変更に伴い`_run_review_phase`は
廃止された(`_run_collaborative_task_queue`が同じサイクルを、キューから
取り出した1ファイルずつに対して都度呼び出す形に置き換わった)。
`_review_and_fix_one_file`自体はキュー方式でもそのまま再利用されている
ため、このファイルでは`_run_review_phase`を経由せず直接呼び出して検証する。

暴走防止の要件(1ファイルにつき最大2回のレビュー)、および構文チェックが
LLMによる内容レビューより前段の関門として機能することが実際に守られて
いることを確認する。実機のOllama/LM Studio/MLX-LMには接続できない環境の
ため、`_stream_chat_from_candidate`を差し替えてメンバーを模擬する。

使い方: python3 tests/test_review_phase.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _member(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


_OWNER = _member("MacStudio(自分)", "qwen2.5-coder-32b")
_REVIEWER = _member("junnoMac-mini", "qwen2.5-coder-14b")
_FULL_PLAN = "storage.py: add_todo(text: str) -> int を実装\ncli.py: storage.add_todoを呼び出すCLI"


def _call_review_and_fix(code, fake_stream, out_dir):
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        return yoriai._review_and_fix_one_file(
            filename="storage.py", owner=_OWNER, code=code, reviewer=_REVIEWER,
            reviewer_own_filename="cli.py", reviewer_own_code="import storage\n",
            full_plan=_FULL_PLAN, org_fingerprint="fingerprint", out_dir=out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream


def test_no_issues_resolves_on_first_round_without_fix_request():
    fix_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        if "現在のコード" in messages[0]["content"]:
            fix_calls["n"] += 1
        yield {"content": "問題なし"}
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        ok, feedback = _call_review_and_fix("def add_todo(text):\n    return 1\n", fake_stream, out_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is True
    assert feedback is None
    assert fix_calls["n"] == 0, "問題なしの場合は修正依頼を送らないはずです"


def test_issue_found_then_fixed_resolves_on_rereview():
    review_round = {"n": 0}
    fix_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            review_round["n"] += 1
            if review_round["n"] == 1:
                yield {"content": "問題あり\nadd_todoの戻り値がNoneです。IDを返してください。"}
            else:
                yield {"content": "問題なし"}
        elif "現在のコード" in text:
            fix_calls["n"] += 1
            yield {"content": "```python\ndef add_todo(text):\n    return 1\n```"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:100]}")
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        ok, feedback = _call_review_and_fix("def add_todo(text):\n    pass\n", fake_stream, out_dir)
        with open(os.path.join(out_dir, "storage.py"), encoding="utf-8") as f:
            saved = f.read()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is True
    assert feedback is None
    assert fix_calls["n"] == 1
    assert review_round["n"] == 2
    assert "return 1" in saved, f"修正版が保存されていません: {saved!r}"


def test_issue_persists_after_two_rounds_gives_up_without_third_review():
    review_round = {"n": 0}
    fix_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            review_round["n"] += 1
            yield {"content": "問題あり\nまだ直っていません。"}
        elif "現在のコード" in text:
            fix_calls["n"] += 1
            yield {"content": "```python\ndef add_todo(text):\n    pass\n```"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:100]}")
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        ok, feedback = _call_review_and_fix("def add_todo(text):\n    pass\n", fake_stream, out_dir)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is False
    assert feedback == "まだ直っていません。", repr(feedback)
    assert review_round["n"] == 2, f"3回目のレビューは行われないはずです(実際: {review_round['n']}回)"
    assert fix_calls["n"] == 1


def test_syntax_error_is_caught_before_any_llm_review_call():
    """メソッドチェーンの括弧漏れによるIndentationErrorを、LLMレビューを
    一切呼ばずに検出し、修正依頼だけを送ることを確認する。
    """
    broken_code = (
        "def main():\n"
        "    x = foo()\n"
        "        .bar()\n"
    )
    fixed_code = "def main():\n    x = foo().bar()\n"
    llm_review_calls = {"n": 0}
    fix_calls = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            llm_review_calls["n"] += 1
            yield {"content": "問題なし"}
        elif "現在のコード" in text:
            fix_calls["n"] += 1
            yield {"content": f"```python\n{fixed_code}```"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:100]}")
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        ok, feedback = _call_review_and_fix(broken_code, fake_stream, out_dir)
        with open(os.path.join(out_dir, "storage.py"), encoding="utf-8") as f:
            saved = f.read()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is True
    assert feedback is None
    assert llm_review_calls["n"] == 1, (
        f"構文エラーのある初回はLLMレビューを呼ばずに弾かれ、修正後の再レビューだけが行われるはずです: {llm_review_calls}"
    )
    assert fix_calls["n"] == 1
    ok_syntax, _detail = yoriai._check_python_syntax(saved)
    assert ok_syntax, f"保存されたコードは構文的に正しいはずです: {saved!r}"


def test_syntax_check_skipped_for_non_python_filename():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"content": "問題なし"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        ok, feedback = yoriai._review_and_fix_one_file(
            filename="README.md", owner=_OWNER, code="これはMarkdownで、Pythonとしては構文エラーです\n",
            reviewer=_REVIEWER, reviewer_own_filename="cli.py", reviewer_own_code="import storage\n",
            full_plan=_FULL_PLAN, org_fingerprint="fingerprint", out_dir=out_dir,
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert ok is True
    assert feedback is None


def main():
    tests = [
        test_no_issues_resolves_on_first_round_without_fix_request,
        test_issue_found_then_fixed_resolves_on_rereview,
        test_issue_persists_after_two_rounds_gives_up_without_third_review,
        test_syntax_error_is_caught_before_any_llm_review_call,
        test_syntax_check_skipped_for_non_python_filename,
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
