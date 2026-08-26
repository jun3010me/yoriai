#!/usr/bin/env python3
"""`yoriai_types.py`のCHAT_READ_TIMEOUT_SECが環境変数
`YORIAI_CHAT_READ_TIMEOUT_SEC`で上書きできることを検証する。

背景: 大型モデル(MacStudio上のqwen3-235b等)は最初の1トークンを出す
までの時間(プロンプト処理+thinking)が既定の読み取りタイムアウト
(120秒)を超えることがあり、特に対話プロトコルはラウンドを重ねるほど
議事録全文をプロンプトに累積するため起きやすい。既定値そのものを
引き上げるのではなく、KEEP_ALIVE(llm_stream.py・
YORIAI_OLLAMA_KEEP_ALIVE環境変数)と同じ流儀で環境変数による上書きの
みを可能にする。

モジュールレベル定数の環境変数依存を検証するため、`importlib.reload`
でyoriai_typesを再importする。

使い方: python3 tests/test_chat_read_timeout_env_override.py
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai_types  # noqa: E402

_ENV_VAR = "YORIAI_CHAT_READ_TIMEOUT_SEC"


def test_chat_read_timeout_sec_defaults_to_120_without_env_var():
    os.environ.pop(_ENV_VAR, None)
    try:
        importlib.reload(yoriai_types)
        assert yoriai_types.CHAT_READ_TIMEOUT_SEC == 120
    finally:
        os.environ.pop(_ENV_VAR, None)
        importlib.reload(yoriai_types)


def test_chat_read_timeout_sec_honors_env_var_override():
    os.environ[_ENV_VAR] = "300"
    try:
        importlib.reload(yoriai_types)
        assert yoriai_types.CHAT_READ_TIMEOUT_SEC == 300
    finally:
        os.environ.pop(_ENV_VAR, None)
        importlib.reload(yoriai_types)


def main():
    tests = [
        test_chat_read_timeout_sec_defaults_to_120_without_env_var,
        test_chat_read_timeout_sec_honors_env_var_override,
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
