#!/usr/bin/env python3
"""サンプリングパラメータ(temperature・repeat_penalty)の明示指定を検証する
テスト(PR4)。

背景: 実運用ログの調査で、MacStudio(LM Studio、qwen3.8-flash-next)が
list_dirツール呼び出しを、堂々巡り検出により1度促されても繰り返す退行
ループに陥っていることが確認された。原因調査の結果、llm_stream.py内の
リクエスト組み立て箇所(Ollama向け/api/chat呼び出し・LM Studio/MLX-LM向け
/v1/chat/completions呼び出しの両方)がtemperature・repeat_penaltyの
いずれも明示的に指定しておらず、各バックエンド側のその場のデフォルト値に
生成挙動を委ねてしまっていることが判明した。

ここでは(1)LM Studio/MLX-LM向けpayloadにtemperature・repeat_penaltyが
含まれること、(2)Ollama向けoptions辞書にも同様のキーが含まれること、
(3)config.py側でモデルごとの個別値を設定した場合それがリクエストに
反映されること、(4)未設定時はconfig.pyの既定値が使われること、を確認する。

使い方: python3 -m pytest tests/test_sampling_params.py
        python3 tests/test_sampling_params.py
"""
import json as _json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config  # noqa: E402
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


class _FakeStreamResponse:
    """`requests.post(..., stream=True)`の戻り値を模擬する最小限のスタブ
    (tests/test_num_ctx.py等と同じもの)。"""

    def __init__(self, lines):
        self.ok = True
        self.status_code = 200
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self):
        for line in self._lines:
            yield line.encode("utf-8") if isinstance(line, str) else line


def _ollama_done_lines():
    return [_json.dumps({"message": {"content": ""}, "done": True, "done_reason": "stop"})]


def _sse_done_lines():
    return ["data: [DONE]"]


def _run_ollama_turn(model):
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["json"] = json
        return _FakeStreamResponse(_ollama_done_lines())

    original_post = yoriai.requests.post
    original_decide = yoriai._decide_num_ctx
    yoriai.requests.post = fake_post
    yoriai._decide_num_ctx = lambda model: None
    try:
        list(llm_stream._stream_ollama_turn(model, [{"role": "user", "content": "hi"}], []))
    finally:
        yoriai.requests.post = original_post
        yoriai._decide_num_ctx = original_decide
    return captured["json"]


def _run_openai_compatible_turn(model):
    captured = {}

    def fake_post(url, json=None, stream=None, timeout=None):
        captured["json"] = json
        return _FakeStreamResponse(_sse_done_lines())

    original_post = yoriai.requests.post
    yoriai.requests.post = fake_post
    try:
        list(llm_stream._stream_openai_compatible_turn(
            yoriai.LMSTUDIO_BASE_URL, model, [{"role": "user", "content": "hi"}], [],
        ))
    finally:
        yoriai.requests.post = original_post
    return captured["json"]


# ---------------------------------------------------------------------------
# 既定値が使われること
# ---------------------------------------------------------------------------

def test_ollama_options_include_default_temperature_and_repeat_penalty():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {}
    try:
        payload = _run_ollama_turn("some-model")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert payload["options"]["temperature"] == config.DEFAULT_TEMPERATURE, payload
    assert payload["options"]["repeat_penalty"] == config.DEFAULT_REPEAT_PENALTY, payload


def test_openai_compatible_payload_includes_default_temperature_and_repeat_penalty():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {}
    try:
        payload = _run_openai_compatible_turn("qwen3.8-flash-next")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert payload["temperature"] == config.DEFAULT_TEMPERATURE, payload
    assert payload["repeat_penalty"] == config.DEFAULT_REPEAT_PENALTY, payload


# ---------------------------------------------------------------------------
# config.py側でのモデルごとの個別値がリクエストに反映されること
# ---------------------------------------------------------------------------

def test_ollama_options_reflect_model_specific_override():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {"some-model": {"temperature": 0.1, "repeat_penalty": 1.3}}
    try:
        payload = _run_ollama_turn("some-model")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert payload["options"]["temperature"] == 0.1, payload
    assert payload["options"]["repeat_penalty"] == 1.3, payload


def test_openai_compatible_payload_reflects_model_specific_override():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {"qwen3.8-flash-next": {"temperature": 0.7}}
    try:
        payload = _run_openai_compatible_turn("qwen3.8-flash-next")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    # temperatureだけ上書きし、repeat_penaltyは指定していないので既定値が使われる。
    assert payload["temperature"] == 0.7, payload
    assert payload["repeat_penalty"] == config.DEFAULT_REPEAT_PENALTY, payload


def test_override_for_other_model_does_not_leak():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {"other-model": {"temperature": 0.9}}
    try:
        payload = _run_ollama_turn("some-model")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert payload["options"]["temperature"] == config.DEFAULT_TEMPERATURE, payload


# ---------------------------------------------------------------------------
# config.get_sampling_params()単体の挙動
# ---------------------------------------------------------------------------

def test_get_sampling_params_uses_defaults_when_no_override():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {}
    try:
        params = config.get_sampling_params("unknown-model")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert params == {
        "temperature": config.DEFAULT_TEMPERATURE,
        "repeat_penalty": config.DEFAULT_REPEAT_PENALTY,
    }, params


def test_get_sampling_params_partial_override_falls_back_to_default_for_missing_key():
    original_overrides = config.MODEL_SAMPLING_OVERRIDES
    config.MODEL_SAMPLING_OVERRIDES = {"my-model": {"repeat_penalty": 1.5}}
    try:
        params = config.get_sampling_params("my-model")
    finally:
        config.MODEL_SAMPLING_OVERRIDES = original_overrides

    assert params == {"temperature": config.DEFAULT_TEMPERATURE, "repeat_penalty": 1.5}, params


def main():
    tests = [
        test_ollama_options_include_default_temperature_and_repeat_penalty,
        test_openai_compatible_payload_includes_default_temperature_and_repeat_penalty,
        test_ollama_options_reflect_model_specific_override,
        test_openai_compatible_payload_reflects_model_specific_override,
        test_override_for_other_model_does_not_leak,
        test_get_sampling_params_uses_defaults_when_no_override,
        test_get_sampling_params_partial_override_falls_back_to_default_for_missing_key,
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
