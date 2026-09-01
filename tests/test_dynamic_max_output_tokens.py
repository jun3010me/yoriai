#!/usr/bin/env python3
"""`yoriai.py`の`_decide_max_output_tokens`(バックエンド・モデル名から
モデルのコンテキスト長を調べ、CHAT_MAX_OUTPUT_TOKENS(応答の最大出力
トークン数)を自動的に引き上げる機能)を検証するテスト。

背景: `CHAT_MAX_OUTPUT_TOKENS`(既定8192)は、機体の性能やモデルの
コンテキスト長に関わらず一律の値だった。一方、自己紹介カード
(`build_profile_card`)は既にモデルごとのコンテキスト長
(`context_lengths`、Ollama/LM Studio双方対応済み)を把握しているため、
この情報を使って「コンテキスト長の大きいモデルを動かしている機体では
出力上限も引き上げる」ようにした(思考モデルの長い思考でも8192トークンで
打ち切られてしまう実機の報告への対応)。`_decide_num_ctx`と同じ
`yoriai.py`モジュールのグローバル関数を直接差し替えるモック方式
(`tests/test_num_ctx.py`を参照)を踏襲する。

使い方: python3 tests/test_dynamic_max_output_tokens.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


# ---------------------------------------------------------------------------
# _decide_max_output_tokens()
# ---------------------------------------------------------------------------

def test_decide_max_output_tokens_scales_up_for_large_context_model():
    """コンテキスト長262144のOllamaモデルでは、その1/8(32768)が
    CHAT_MAX_OUTPUT_TOKENS(8192)より大きい値として採用されることを確認する。
    """
    original = yoriai.get_ollama_context_length
    yoriai.get_ollama_context_length = lambda model: 262144
    try:
        result = yoriai._decide_max_output_tokens("ollama", "big-context-model")
    finally:
        yoriai.get_ollama_context_length = original

    assert result == 262144 // 8, result
    assert result > yoriai.CHAT_MAX_OUTPUT_TOKENS, result


def test_decide_max_output_tokens_respects_hard_cap():
    """極端に大きいコンテキスト長(1000000、1/8だと125000)でも、
    _MAX_OUTPUT_TOKENS_HARD_CAP(65536)を超えないことを確認する。
    """
    original = yoriai.get_ollama_context_length
    yoriai.get_ollama_context_length = lambda model: 1000000
    try:
        result = yoriai._decide_max_output_tokens("ollama", "huge-context-model")
    finally:
        yoriai.get_ollama_context_length = original

    assert result == yoriai._MAX_OUTPUT_TOKENS_HARD_CAP, result


def test_decide_max_output_tokens_falls_back_to_default_when_unknown():
    """コンテキスト長が取得できない場合、CHAT_MAX_OUTPUT_TOKENSがそのまま
    返ることを確認する。backendが"ollama"でget_ollama_context_lengthが
    Noneを返す場合と、backendが"mlx_lm"(コンテキスト長取得手段が無い)の
    場合の両方を確認する。
    """
    original = yoriai.get_ollama_context_length
    yoriai.get_ollama_context_length = lambda model: None
    try:
        result = yoriai._decide_max_output_tokens("ollama", "unknown-model")
    finally:
        yoriai.get_ollama_context_length = original
    assert result == yoriai.CHAT_MAX_OUTPUT_TOKENS, result

    result_mlx = yoriai._decide_max_output_tokens("mlx_lm", "some-mlx-model")
    assert result_mlx == yoriai.CHAT_MAX_OUTPUT_TOKENS, result_mlx


def test_decide_max_output_tokens_never_goes_below_env_override():
    """環境変数YORIAI_CHAT_MAX_OUTPUT_TOKENSによる手動指定
    (CHAT_MAX_OUTPUT_TOKENSそのものが大きな値になっている状況)は、常に
    自動計算結果の下限として尊重されることを確認する。

    仮の判断: 実際の環境変数読み込みはyoriai_typesのモジュールロード時
    (importlib.reload)にしか反映されず、yoriai.py全体を再importするのは
    副作用が大きく重い(tests/test_chat_read_timeout_env_override.pyの
    CHAT_READ_TIMEOUT_SECはyoriai_types側の値を直接見るだけなのに対し、
    ここではyoriai.py内の関数が使うCHAT_MAX_OUTPUT_TOKENSの値そのものを
    差し替える必要があるため)。`yoriai.CHAT_MAX_OUTPUT_TOKENS`を直接
    書き換えることで「環境変数によって既定値そのものが大きく上書きされて
    いる」状況を模擬する(_decide_max_output_tokensはこのモジュール
    グローバルを名前で参照するため、他のテストの`_NUM_CTX_MEMORY_TIERS`
    差し替えと同じ要領で成立する)。
    """
    original_max_tokens = yoriai.CHAT_MAX_OUTPUT_TOKENS
    original_context = yoriai.get_ollama_context_length
    yoriai.CHAT_MAX_OUTPUT_TOKENS = 100000  # 環境変数で手動上書きされた想定
    # 自動計算結果(4096モデル上限 // 8 = 512)は手動指定より明らかに小さい。
    yoriai.get_ollama_context_length = lambda model: 4096
    try:
        result = yoriai._decide_max_output_tokens("ollama", "small-context-model")
    finally:
        yoriai.CHAT_MAX_OUTPUT_TOKENS = original_max_tokens
        yoriai.get_ollama_context_length = original_context

    assert result == 100000, result


# ---------------------------------------------------------------------------
# stream_chat_completion(): _decide_max_output_tokensの結果がバックエンドへ渡る
# ---------------------------------------------------------------------------

def test_stream_chat_completion_passes_effective_max_output_tokens_to_backend():
    """stream_chat_completionが、振り分けたバックエンド(ここではLM Studio)の
    _stream_lmstudio_turnへ、_decide_max_output_tokensの結果をそのまま
    max_output_tokens引数として渡していることを確認する。
    """
    captured = {}

    def fake_lmstudio_turn(model, messages, tools, max_output_tokens=None):
        captured["model"] = model
        captured["max_output_tokens"] = max_output_tokens
        yield {"tool_calls": [], "truncated": False}

    decide_calls = []

    def fake_decide(backend, model):
        decide_calls.append((backend, model))
        return 40000

    original_ollama = yoriai.get_ollama_loaded_models
    original_mlx = yoriai.get_mlx_lm_models
    original_decide = yoriai._decide_max_output_tokens
    original_turn = llm_stream._stream_lmstudio_turn
    yoriai.get_ollama_loaded_models = lambda: []
    yoriai.get_mlx_lm_models = lambda: []
    yoriai._decide_max_output_tokens = fake_decide
    llm_stream._stream_lmstudio_turn = fake_lmstudio_turn
    try:
        list(llm_stream.stream_chat_completion("qwen3-235b", [{"role": "user", "content": "こんにちは"}]))
    finally:
        yoriai.get_ollama_loaded_models = original_ollama
        yoriai.get_mlx_lm_models = original_mlx
        yoriai._decide_max_output_tokens = original_decide
        llm_stream._stream_lmstudio_turn = original_turn

    assert captured["max_output_tokens"] == 40000, captured
    assert decide_calls == [("lmstudio", "qwen3-235b")], decide_calls


def main():
    tests = [
        test_decide_max_output_tokens_scales_up_for_large_context_model,
        test_decide_max_output_tokens_respects_hard_cap,
        test_decide_max_output_tokens_falls_back_to_default_when_unknown,
        test_decide_max_output_tokens_never_goes_below_env_override,
        test_stream_chat_completion_passes_effective_max_output_tokens_to_backend,
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
