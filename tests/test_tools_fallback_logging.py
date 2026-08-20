#!/usr/bin/env python3
"""stream_chat_completion()の「tools無し再試行」ロジックが、発動する場合・
しない場合の両方で期待通りのログを出すことを検証するテスト。

依存ライブラリは使わず、標準ライブラリのloggingでログ出力を直接捕捉して
アサーションする。実機で「tools無し再試行のログが出ているはずなのに出て
いない」という問題(実際には常駐エージェントの再起動漏れが原因だった)が
起きた際、ログの文言だけで「ロジックが動いたかどうか」を切り分けられる
ようにするために追加した。

さらに、ステータスコードだけで再試行の要否を判断すると、モデルのロード
失敗(メモリ不足)のようなツールと無関係な400/422まで再試行対象に
含めてしまう不具合が実機で見つかったため、エラー文にツール関連の記述が
含まれるかどうかも合わせて確認するようにした変更もここで検証している。

使い方: python3 tests/test_tools_fallback_logging.py
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _run_with_capture(fn):
    capture = _LogCapture()
    yoriai.logger.addHandler(capture)
    original_level = yoriai.logger.level
    yoriai.logger.setLevel(logging.DEBUG)
    try:
        result = fn()
    finally:
        yoriai.logger.removeHandler(capture)
        yoriai.logger.setLevel(original_level)
    return result, capture.records


def test_tools_rejected_triggers_retry_and_logs_it():
    """tools付きリクエストが400/422で拒否され、かつエラー文にツール関連の記述が
    含まれる場合、同一モデルへツールなしで自動的に再試行し、そのことを示す
    [tools無し再試行]ログが出ることを確認する。エラー文は実機で実際に報告された
    ものを使う: 「This model does not support tools/function calling with the
    current context settings.」
    """
    def fake_turn(base_url, model, messages, tools):
        if tools:
            yield {
                "error": "400: Trying to keep the first 32768 tokens when the context the model "
                          "can handle is only 4096 tokens. This model does not support "
                          "tools/function calling with the current context settings.",
                "status_code": 400,
            }
        else:
            yield {"content": "OK without tools"}
            yield {"tool_calls": []}

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        events, logs = _run_with_capture(
            lambda: list(yoriai.stream_chat_completion("qwen3-coder-30b", [{"role": "user", "content": "fix bug"}]))
        )
    finally:
        yoriai._stream_openai_compatible_turn = original

    assert any("[tools無し再試行]" in line and "qwen3-coder-30b" in line for line in logs), (
        f"発動ログ([tools無し再試行])が見つかりません: {logs}"
    )
    assert any(e.get("content") == "OK without tools" for e in events), (
        f"tools無し再試行後の成功応答が得られていません: {events}"
    )


def test_resource_error_does_not_trigger_retry_despite_matching_status_code():
    """モデルのロード失敗(メモリ不足)のような、ツールとは無関係な原因でも
    LM Studioは同じ400を返してくることが実機で見つかった。ステータスコードが
    400であっても、エラー文にツール関連の記述が無ければツールなし再試行を
    行わず、元のエラーをそのまま伝播することを確認する。エラー文は実機で
    実際に報告されたものを使う: 「Model loading was stopped due to
    insufficient system resources.」
    """
    def fake_turn(base_url, model, messages, tools):
        yield {
            "error": '400: Failed to load model "qwen/qwen3-coder-30b". Error: Model loading was '
                      "stopped due to insufficient system resources. Continuing to load the model "
                      "would likely overload your system and cause it to freeze.",
            "status_code": 400,
        }

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        events, logs = _run_with_capture(
            lambda: list(yoriai.stream_chat_completion("qwen/qwen3-coder-30b", [{"role": "user", "content": "fix bug"}]))
        )
    finally:
        yoriai._stream_openai_compatible_turn = original

    assert not any("[tools無し再試行]" in line for line in logs), (
        f"ツールと無関係なエラーなのに再試行が発動しています: {logs}"
    )
    assert any("[tools無し再試行なし]" in line and "insufficient system resources" in line for line in logs), (
        f"非発動ログ([tools無し再試行なし])に元のエラー詳細が含まれていません: {logs}"
    )
    assert len(events) == 1 and "error" in events[0] and "insufficient system resources" in events[0]["error"], (
        f"リソース不足エラーがそのまま1件だけ伝播するはずです: {events}"
    )


def test_non_tools_status_code_does_not_trigger_retry_and_logs_it():
    """401のような、tools無し再試行の対象外のステータスコードでは再試行せず、
    [tools無し再試行なし]ログを出したうえでエラーをそのまま伝播することを確認する。
    """
    def fake_turn(base_url, model, messages, tools):
        yield {"error": "401: unauthorized", "status_code": 401}

    original = yoriai._stream_openai_compatible_turn
    yoriai._stream_openai_compatible_turn = fake_turn
    try:
        events, logs = _run_with_capture(
            lambda: list(yoriai.stream_chat_completion("some-model", [{"role": "user", "content": "hi"}]))
        )
    finally:
        yoriai._stream_openai_compatible_turn = original

    assert any("[tools無し再試行なし]" in line for line in logs), (
        f"非発動ログ([tools無し再試行なし])が見つかりません: {logs}"
    )
    assert not any("[tools無し再試行]" in line for line in logs), (
        f"発動していないはずなのに[tools無し再試行]ログが出ています: {logs}"
    )
    assert events == [{"error": "401: unauthorized", "status_code": 401}], (
        f"401エラーがそのまま伝播していません: {events}"
    )


def test_http_error_response_body_is_logged():
    """resp.raise_for_status()ではなくresp.okのチェックに変えたことで、
    LM Studio等が返すレスポンスボディ(具体的なエラーメッセージ)がログに
    残ることを確認する(以前はステータス行しか残らなかった)。
    """
    class _FakeResponse:
        ok = False
        status_code = 400
        text = '{"error": {"message": "context length exceeded for tools"}}'

        def json(self):
            import json
            return json.loads(self.text)

    event, logs = _run_with_capture(lambda: yoriai._log_and_build_http_error("http://localhost:1234", _FakeResponse()))

    assert event["status_code"] == 400
    assert "context length exceeded for tools" in event["error"], event
    assert any("context length exceeded for tools" in line for line in logs), (
        f"レスポンスボディの内容がログに出ていません: {logs}"
    )
    # 修正前の raise_for_status() 由来の定型文("400 Client Error: ...")では
    # ないことも確認しておく(退行検知のため)。
    assert "Client Error" not in event["error"], event


def main():
    tests = [
        test_tools_rejected_triggers_retry_and_logs_it,
        test_resource_error_does_not_trigger_retry_despite_matching_status_code,
        test_non_tools_status_code_does_not_trigger_retry_and_logs_it,
        test_http_error_response_body_is_logged,
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
