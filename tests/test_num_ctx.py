#!/usr/bin/env python3
"""Ollamaへの/api/chat問い合わせにnum_ctx(コンテキストウィンドウの長さ)・
keep_alive(モデルのメモリ保持時間)を明示的に指定するようにした変更を
検証するテスト。

背景: num_ctxを指定していなかったため、Ollamaはモデルの学習上の上限では
なくOllama側のデフォルト(バージョンにより4096または2048)でコンテキスト
ウィンドウを開いていた。超過した入力はエラーにならず先頭から黙って
切り捨てられるため、協業モードの実装計画やread_fileが返す大きめの
コードが真っ先に捨てられ、モデルが計画に無いファイル名を書いたり
実在するコードを見落としたりする不具合につながっていた疑いがある。

ここでは(1)get_ollama_context_length()が/api/showのmodel_infoから
".context_length"で終わるキーを(プレフィックスを決め打ちせずに)拾える
こと、(2)/api/showが失敗しても例外にならずNoneを返すこと、(3)2回目
以降の呼び出しでHTTPリクエストが発生しないこと(キャッシュ)、
(4)_decide_num_ctxがモデル上限・メモリ由来上限・MAX_NUM_CTXの最小値を
返すこと、(5)_stream_ollama_turnのリクエストボディにnum_ctx・keep_alive
が含まれ、num_ctxがNoneのときはキー自体が含まれないこと、を確認する。

実機のOllamaに接続できない環境のため、`requests.post`を差し替えて模擬する。

使い方: python3 tests/test_num_ctx.py
"""
import json as _json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


class _FakeShowResponse:
    def __init__(self, model_info=None, status_code=200):
        self.status_code = status_code
        self._model_info = model_info or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} Client Error")

    def json(self):
        return {"model_info": self._model_info}


class _FakeStreamResponse:
    """`requests.post(..., stream=True)`の戻り値を模擬する最小限のスタブ。"""

    def __init__(self, lines):
        self.ok = True
        self.status_code = 200
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line


def _reset_context_length_cache():
    yoriai._OLLAMA_CONTEXT_LENGTH_CACHE.clear()


# ---------------------------------------------------------------------------
# get_ollama_context_length()
# ---------------------------------------------------------------------------

def test_context_length_found_regardless_of_architecture_prefix():
    """アーキテクチャ名のプレフィックスがllama以外(qwen2やgemma3)でも、
    '.context_length'で終わるキーを拾えることを確認する。
    """
    _reset_context_length_cache()
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeShowResponse({"qwen2.context_length": 32768, "qwen2.embedding_length": 4096})

    original = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        result = yoriai.get_ollama_context_length("qwen2.5-coder:7b")
    finally:
        yoriai.requests.post = original

    assert result == 32768, result
    assert captured["url"] == f"{yoriai.OLLAMA_BASE_URL}/api/show", captured["url"]
    assert captured["json"] == {"model": "qwen2.5-coder:7b"}, captured["json"]


def test_context_length_found_with_llama_prefix():
    _reset_context_length_cache()
    original = yoriai.requests.post
    yoriai.requests.post = lambda url, json=None, timeout=None: _FakeShowResponse({"llama.context_length": 131072})
    try:
        result = yoriai.get_ollama_context_length("llama3.1")
    finally:
        yoriai.requests.post = original
    assert result == 131072, result


def test_context_length_returns_none_on_failure_without_raising():
    _reset_context_length_cache()

    def fake_post(url, json=None, timeout=None):
        raise Exception("接続できません")

    original = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        result = yoriai.get_ollama_context_length("broken-model")
    finally:
        yoriai.requests.post = original
    assert result is None, result


def test_context_length_returns_none_when_no_matching_key():
    _reset_context_length_cache()
    original = yoriai.requests.post
    yoriai.requests.post = lambda url, json=None, timeout=None: _FakeShowResponse({"some_other_field": 1})
    try:
        result = yoriai.get_ollama_context_length("mystery-model")
    finally:
        yoriai.requests.post = original
    assert result is None, result


def test_context_length_is_cached_after_first_call():
    _reset_context_length_cache()
    call_count = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        call_count["n"] += 1
        return _FakeShowResponse({"llama.context_length": 8192})

    original = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        first = yoriai.get_ollama_context_length("cached-model")
        second = yoriai.get_ollama_context_length("cached-model")
    finally:
        yoriai.requests.post = original

    assert first == 8192 and second == 8192
    assert call_count["n"] == 1, f"2回目の呼び出しでHTTPリクエストが発生しています: {call_count['n']}回"


def test_none_result_is_also_cached():
    """取得失敗(None)の結果もキャッシュされ、毎回問い合わせに行かないことを確認する。"""
    _reset_context_length_cache()
    call_count = {"n": 0}

    def fake_post(url, json=None, timeout=None):
        call_count["n"] += 1
        raise Exception("timeout")

    original = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        yoriai.get_ollama_context_length("unreachable-model")
        yoriai.get_ollama_context_length("unreachable-model")
    finally:
        yoriai.requests.post = original
    assert call_count["n"] == 1, call_count["n"]


# ---------------------------------------------------------------------------
# get_lmstudio_context_lengths()
# ---------------------------------------------------------------------------

class _FakeLmstudioModelsResponse:
    def __init__(self, data, status_code=200):
        self.status_code = status_code
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} Client Error")

    def json(self):
        return {"data": self._data}


def test_get_lmstudio_context_lengths_returns_loaded_context_length_when_available():
    data = [
        {
            "id": "qwen3-235b-a22b",
            "state": "loaded",
            "max_context_length": 262144,
            "loaded_context_length": 262144,
        },
    ]
    original = yoriai.requests.get
    yoriai.requests.get = lambda url, timeout=None: _FakeLmstudioModelsResponse(data)
    try:
        result = yoriai.get_lmstudio_context_lengths()
    finally:
        yoriai.requests.get = original
    assert result == {"qwen3-235b-a22b": 262144}, result


def test_get_lmstudio_context_lengths_falls_back_to_max_context_length():
    data = [
        {
            "id": "loading-model",
            "state": "loaded",
            "max_context_length": 32768,
            # loaded_context_length未取得(ロード完了直前などを想定)
        },
    ]
    original = yoriai.requests.get
    yoriai.requests.get = lambda url, timeout=None: _FakeLmstudioModelsResponse(data)
    try:
        result = yoriai.get_lmstudio_context_lengths()
    finally:
        yoriai.requests.get = original
    assert result == {"loading-model": 32768}, result


def test_get_lmstudio_context_lengths_excludes_not_loaded_models():
    data = [
        {"id": "not-loaded-model", "state": "not-loaded", "max_context_length": 8192},
        {
            "id": "loaded-model",
            "state": "loaded",
            "max_context_length": 16384,
            "loaded_context_length": 16384,
        },
    ]
    original = yoriai.requests.get
    yoriai.requests.get = lambda url, timeout=None: _FakeLmstudioModelsResponse(data)
    try:
        result = yoriai.get_lmstudio_context_lengths()
    finally:
        yoriai.requests.get = original
    assert result == {"loaded-model": 16384}, result


def test_get_lmstudio_context_lengths_returns_empty_dict_on_connection_failure():
    def fake_get(url, timeout=None):
        raise Exception("接続できません")

    original = yoriai.requests.get
    yoriai.requests.get = fake_get
    try:
        result = yoriai.get_lmstudio_context_lengths()
    finally:
        yoriai.requests.get = original
    assert result == {}, result


# ---------------------------------------------------------------------------
# _decide_num_ctx()
# ---------------------------------------------------------------------------

def _with_patched(model_limit, free_gb, fn):
    original_context = yoriai.get_ollama_context_length
    original_memory = yoriai.get_memory_info
    yoriai.get_ollama_context_length = lambda model: model_limit
    yoriai.get_memory_info = lambda: {"free_gb": free_gb, "total_gb": None, "free_bytes": None, "total_bytes": None}
    try:
        return fn()
    finally:
        yoriai.get_ollama_context_length = original_context
        yoriai.get_memory_info = original_memory


def test_decide_num_ctx_picks_minimum_of_model_memory_and_max():
    """モデル上限(131072)・メモリ由来上限(空き10GB→32768)・
    MAX_NUM_CTX(65536)のうち最小のメモリ由来上限が採用されることを確認する。
    """
    result = _with_patched(131072, 10.0, lambda: yoriai._decide_num_ctx("big-model"))
    assert result == 32768, result


def test_decide_num_ctx_picks_model_limit_when_smallest():
    """モデル上限(4096)がメモリ由来上限より小さい場合、モデル上限が
    採用されることを確認する。ただしCHAT_MAX_OUTPUT_TOKENS*2を下限として
    持ち上げるため、実際に採用される値はその下限になる。
    """
    result = _with_patched(4096, 20.0, lambda: yoriai._decide_num_ctx("small-model"))
    assert result == yoriai.CHAT_MAX_OUTPUT_TOKENS * 2, result


def test_decide_num_ctx_falls_back_to_memory_limit_when_model_limit_unknown():
    """モデル上限が取得できない場合、その条件は無視してメモリ由来上限と
    MAX_NUM_CTXの小さい方(ここでは空き10GB→32768)が採用されることを確認する。
    """
    result = _with_patched(None, 10.0, lambda: yoriai._decide_num_ctx("unknown-model"))
    assert result == 32768, result


def test_decide_num_ctx_never_below_double_output_tokens():
    """メモリ由来上限が極端に小さくても、CHAT_MAX_OUTPUT_TOKENS*2を
    下回らないことを確認する(出力の余地が無くなるのを防ぐため)。
    """
    original_tiers = yoriai._NUM_CTX_MEMORY_TIERS
    yoriai._NUM_CTX_MEMORY_TIERS = ((100.0, 1000),)
    try:
        result = _with_patched(None, 1.0, lambda: yoriai._decide_num_ctx("tiny-memory-model"))
    finally:
        yoriai._NUM_CTX_MEMORY_TIERS = original_tiers
    assert result == yoriai.CHAT_MAX_OUTPUT_TOKENS * 2, result


# ---------------------------------------------------------------------------
# _stream_ollama_turn()
# ---------------------------------------------------------------------------

def _drain(events):
    return list(events)


def test_stream_ollama_turn_includes_num_ctx_and_keep_alive():
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["json"] = json
        lines = [
            _json.dumps({"message": {"content": "こんにちは"}, "done": False}),
            _json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"}),
        ]
        return _FakeStreamResponse(lines)

    original_post = yoriai.requests.post
    original_decide = yoriai._decide_num_ctx
    yoriai.requests.post = fake_post
    yoriai._decide_num_ctx = lambda model: 32768
    try:
        _drain(llm_stream._stream_ollama_turn("qwen3-coder", [{"role": "user", "content": "こんにちは"}], []))
    finally:
        yoriai.requests.post = original_post
        yoriai._decide_num_ctx = original_decide

    assert captured["json"]["options"]["num_ctx"] == 32768, captured["json"]
    assert captured["json"]["options"]["num_predict"] == yoriai.CHAT_MAX_OUTPUT_TOKENS, captured["json"]
    assert captured["json"]["keep_alive"] == llm_stream.KEEP_ALIVE, captured["json"]


def test_stream_ollama_turn_omits_num_ctx_key_when_none():
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["json"] = json
        lines = [_json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"})]
        return _FakeStreamResponse(lines)

    original_post = yoriai.requests.post
    original_decide = yoriai._decide_num_ctx
    yoriai.requests.post = fake_post
    yoriai._decide_num_ctx = lambda model: None
    try:
        _drain(llm_stream._stream_ollama_turn("some-model", [{"role": "user", "content": "hi"}], []))
    finally:
        yoriai.requests.post = original_post
        yoriai._decide_num_ctx = original_decide

    assert "num_ctx" not in captured["json"]["options"], captured["json"]


def test_estimate_tokens_counts_ascii_and_non_ascii_differently():
    messages = [
        {"role": "user", "content": "abcd" * 4},  # 16 ASCII chars -> 4トークン
        {"role": "assistant", "content": "こんにちは"},  # 5 non-ASCII chars -> 5トークン
    ]
    assert llm_stream._estimate_tokens(messages) == 9, llm_stream._estimate_tokens(messages)


def main():
    tests = [
        test_context_length_found_regardless_of_architecture_prefix,
        test_context_length_found_with_llama_prefix,
        test_context_length_returns_none_on_failure_without_raising,
        test_context_length_returns_none_when_no_matching_key,
        test_context_length_is_cached_after_first_call,
        test_none_result_is_also_cached,
        test_get_lmstudio_context_lengths_returns_loaded_context_length_when_available,
        test_get_lmstudio_context_lengths_falls_back_to_max_context_length,
        test_get_lmstudio_context_lengths_excludes_not_loaded_models,
        test_get_lmstudio_context_lengths_returns_empty_dict_on_connection_failure,
        test_decide_num_ctx_picks_minimum_of_model_memory_and_max,
        test_decide_num_ctx_picks_model_limit_when_smallest,
        test_decide_num_ctx_falls_back_to_memory_limit_when_model_limit_unknown,
        test_decide_num_ctx_never_below_double_output_tokens,
        test_stream_ollama_turn_includes_num_ctx_and_keep_alive,
        test_stream_ollama_turn_omits_num_ctx_key_when_none,
        test_estimate_tokens_counts_ascii_and_non_ascii_differently,
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
