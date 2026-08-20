#!/usr/bin/env python3
"""協業モードのレビューフェーズ(実験3)を検証する。

実装フェーズ完了後、お互いが担当外のファイルをレビューし、問題があれば
担当メンバーに修正を依頼し、修正後にもう一度だけ再レビューする、という
一連の流れを、`_run_review_phase`を直接呼び出して検証する。暴走防止の
要件(1ファイルにつき最大2回のレビュー)が実際に守られていることも
併せて確認する。実機のOllama/LM Studio/MLX-LMには接続できない環境のため、
`_stream_chat_from_candidate`を差し替えて2台のメンバーを模擬する。

使い方: python3 tests/test_review_phase.py
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


def _make_implemented():
    return [
        {"filename": "storage.py", "candidate": _member("MacStudio(自分)", "qwen2.5-coder-32b"), "code": "def add_todo(text):\n    pass\n"},
        {"filename": "cli.py", "candidate": _member("junnoMac-mini", "qwen2.5-coder-14b"), "code": "import storage\n"},
    ]


_AGREED_PLAN = [
    ("storage.py", "add_todo(text: str) -> int を実装"),
    ("cli.py", "storage.add_todoを呼び出すCLI"),
]


def _run_review_phase_capturing_stdout(implemented, agreed_plan, out_dir):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yoriai._run_review_phase(implemented, agreed_plan, "fingerprint", out_dir)
    return buf.getvalue()


def test_review_phase_completes_when_both_reviewers_report_no_issues():
    """両方のレビューで「問題なし」なら、修正依頼を送ることなく
    「レビュー完了」と表示されることを確認する(依頼の動作確認シナリオ:
    「前回の実装で既に完全に一致していて指摘が出ない場合」に対応)。
    """
    implemented = _make_implemented()
    review_calls = []

    def fake_stream(candidate, org_fingerprint, messages):
        review_calls.append(candidate["label"])
        yield {"content": "問題なし"}
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        output = _run_review_phase_capturing_stdout(implemented, _AGREED_PLAN, out_dir)
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    # storage.pyの担当外(junnoMac-mini)がstorage.pyを、cli.pyの担当外
    # (MacStudio自身)がcli.pyをレビューする、の2回だけ問い合わせが飛ぶこと。
    assert review_calls == ["junnoMac-mini", "MacStudio(自分)"], review_calls
    assert "[✅ レビュー完了]" in output, output
    assert "未解決" not in output, output
    assert "修正を依頼しています" not in output, "問題なしの場合は修正依頼を送らないはずです"


def test_review_phase_requests_fix_and_resolves_after_rereview():
    """レビューで指摘が入った場合、担当メンバーに修正を依頼し、修正後の
    再レビューで「問題なし」になれば解決として扱われることを確認する。
    保存済みファイルが修正後のコードで上書きされることも確認する。
    """
    implemented = _make_implemented()
    fix_call_count = {"n": 0}
    review_round_count = {"storage.py": 0}

    def fake_stream(candidate, org_fingerprint, messages):
        label = candidate["label"]
        request_text = messages[0]["content"]
        if "レビュー対象: storage.py" in request_text:
            review_round_count["storage.py"] += 1
            if review_round_count["storage.py"] == 1:
                yield {"content": "問題あり\nadd_todoの戻り値がNoneになっています。IDを返すよう修正してください。"}
            else:
                yield {"content": "問題なし"}
        elif "現在のコード: storage.py" in request_text:
            fix_call_count["n"] += 1
            yield {"content": "```python\ndef add_todo(text):\n    return 1\n```"}
        elif "レビュー対象: cli.py" in request_text:
            yield {"content": "問題なし"}
        else:
            raise AssertionError(f"想定外の問い合わせです(label={label}): {request_text[:100]}")
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        output = _run_review_phase_capturing_stdout(implemented, _AGREED_PLAN, out_dir)

        assert fix_call_count["n"] == 1, "修正依頼はちょうど1回のはずです"
        assert review_round_count["storage.py"] == 2, "storage.pyのレビューは初回+再レビューの2回のはずです"

        with open(os.path.join(out_dir, "storage.py"), encoding="utf-8") as f:
            saved_storage = f.read()
        assert "return 1" in saved_storage, (
            f"修正後のコードでファイルが上書きされていません: {saved_storage!r}"
        )
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert "1回目のレビュー" in output, output
    assert "修正後の再レビュー" in output, output
    assert "修正を依頼しています" in output, output
    assert "[✅ レビュー完了]" in output, (
        f"再レビューで問題なしになったので最終的に完了扱いになるはずです: {output}"
    )
    assert "未解決" not in output, output


def test_review_phase_reports_unresolved_when_issue_persists_after_two_rounds():
    """再レビューでも問題が残る場合、3回目のレビューは行わず、
    「未解決の指摘が残っています」と報告して打ち切ることを確認する
    (暴走防止の要件: 1ファイルにつき最大2回のレビューまで)。
    """
    implemented = _make_implemented()
    review_round_count = {"storage.py": 0}
    fix_call_count = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages):
        request_text = messages[0]["content"]
        if "レビュー対象: storage.py" in request_text:
            review_round_count["storage.py"] += 1
            yield {"content": "問題あり\nまだ問題が残っています。"}
        elif "現在のコード: storage.py" in request_text:
            fix_call_count["n"] += 1
            # 仮の判断: 修正を試みても直らないケースを再現する(モデルが
            # 指摘を反映しきれない、という現実的なシナリオ)。
            yield {"content": "```python\ndef add_todo(text):\n    pass\n```"}
        elif "レビュー対象: cli.py" in request_text:
            yield {"content": "問題なし"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {request_text[:100]}")
        yield {"done": True}

    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        output = _run_review_phase_capturing_stdout(implemented, _AGREED_PLAN, out_dir)
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    # 暴走防止: 3回目のレビューは絶対に発生しない。
    assert review_round_count["storage.py"] == 2, (
        f"レビューは初回+再レビューの2回で打ち切られるはずです(実際: {review_round_count['storage.py']}回)"
    )
    assert fix_call_count["n"] == 1, "修正依頼は初回レビュー後の1回だけのはずです"
    assert "[⚠️ 未解決の指摘が残っています: storage.py]" in output, output
    assert "[✅ レビュー完了]" not in output, output


def test_review_phase_skipped_when_fewer_than_two_files_implemented():
    """実装済みファイルが1件しかない場合、レビューフェーズ自体を実行せず
    その旨を表示することを確認する(_ask_organization_collaborate側の
    ガード条件)。
    """
    implemented = [_make_implemented()[0]]  # storage.pyのみ

    call_log = []

    def fake_stream(candidate, org_fingerprint, messages):
        call_log.append(candidate["label"])
        yield {"content": "問題なし"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    def fake_snapshot(port, fp, fail_fast=False):
        self_card = {
            "device_name": "MacStudio",
            "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
            "memory": {"free_gb": 40, "total_gb": 64},
            "models": {"installed": ["qwen2.5-coder-32b"], "loaded": ["qwen2.5-coder-32b"], "backends": ["lmstudio"]},
            "generated_at": "2026-08-20T00:00:00+0900",
        }
        return {"self": self_card, "peers": []}

    yoriai._fetch_org_snapshot = fake_snapshot

    def fake_stream_collaborate(candidate, org_fingerprint, messages):
        request_text = messages[0]["content"]
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": "storage.py: add_todo(text: str) -> int を実装\n"}
        else:
            yield {"content": "```python\ndef add_todo(text):\n    return 1\n```"}
        yield {"done": True}

    yoriai._stream_chat_from_candidate = fake_stream_collaborate

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "何か作って", out_dir)
        output = buf.getvalue()
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)

    assert "レビューフェーズはスキップします" in output, output
    assert "レビューフェーズ開始" not in output, output


def main():
    tests = [
        test_review_phase_completes_when_both_reviewers_report_no_issues,
        test_review_phase_requests_fix_and_resolves_after_rereview,
        test_review_phase_reports_unresolved_when_issue_persists_after_two_rounds,
        test_review_phase_skipped_when_fewer_than_two_files_implemented,
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
