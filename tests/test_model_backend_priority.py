#!/usr/bin/env python3
"""build_profile_card()が組み立てるmodels.loadedの並び順が、
stream_chat_completion()のバックエンド振り分け優先順位
(Ollama→MLX-LM→LM Studio)と一致していることを検証するテスト。

実機(Mac Studio、LM StudioとMLX-LMの両バックエンドを保持)で、
//multi 実行時にMLX-LM側のモデル(mlx-community/Qwen2.5-7B-Instruct-4bit)
ではなくLM Studio側のモデル(qwen3-235b-a22b)が選ばれてしまう不具合が
報告された。原因はbuild_profile_card()内の_merge_model_lists()の
引数順序が(ollama, lmstudio, mlx_lm)となっており、
_build_chat_candidate()が使うloaded[0]がLM Studio側のモデルに
なってしまうことだった。フェーズ6-3の判断メモに記載された優先順位
(Ollama→MLX-LM→LM Studio)に合わせて(ollama, mlx_lm, lmstudio)の
順に修正したことをここで検証する。

使い方: python3 tests/test_model_backend_priority.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


def _with_patched_backends(ollama_installed, ollama_loaded, lmstudio_models, mlx_lm_models, fn):
    originals = {
        "get_ollama_installed_models": yoriai.get_ollama_installed_models,
        "get_ollama_loaded_models": yoriai.get_ollama_loaded_models,
        "get_lmstudio_models": yoriai.get_lmstudio_models,
        "get_mlx_lm_models": yoriai.get_mlx_lm_models,
    }
    yoriai.get_ollama_installed_models = lambda: ollama_installed
    yoriai.get_ollama_loaded_models = lambda: ollama_loaded
    yoriai.get_lmstudio_models = lambda: lmstudio_models
    yoriai.get_mlx_lm_models = lambda: mlx_lm_models
    try:
        return fn()
    finally:
        for name, original in originals.items():
            setattr(yoriai, name, original)


def test_mlx_lm_prioritized_over_lmstudio_when_no_ollama():
    """実機で報告されたMac Studioの状況を再現する: Ollamaは未使用、
    LM StudioにqweN3-235b-a22b、MLX-LMにmlx-community/Qwen2.5-7B-Instruct-4bit
    がロード済み。build_profile_card()のmodels.loaded[0]がMLX-LM側の
    モデルになっていること(=LM Studio側が誤って選ばれないこと)を確認する。
    """
    card = _with_patched_backends(
        ollama_installed=[],
        ollama_loaded=[],
        lmstudio_models=["qwen3-235b-a22b"],
        mlx_lm_models=["mlx-community/Qwen2.5-7B-Instruct-4bit"],
        fn=lambda: llm_stream.build_profile_card("test-agent"),
    )

    assert card["models"]["loaded"][0] == "mlx-community/Qwen2.5-7B-Instruct-4bit", (
        f"loaded[0]がMLX-LM側のモデルになっていません: {card['models']['loaded']}"
    )
    assert card["models"]["backends"] == ["mlx_lm", "lmstudio"], (
        f"backendsの並び順もMLX-LM優先になっているはずです: {card['models']['backends']}"
    )

    candidate = yoriai._build_chat_candidate(card, is_self=False, address="127.0.0.1", port=9999)
    assert candidate["model"] == "mlx-community/Qwen2.5-7B-Instruct-4bit", (
        f"_build_chat_candidateが選ぶ代表モデルがMLX-LM側ではありません: {candidate}"
    )


def test_ollama_still_wins_over_mlx_lm_and_lmstudio():
    """Ollamaが動いている場合は引き続き最優先で選ばれることを確認する
    (今回の修正でOllamaの優先度を崩していないことの退行検知)。
    """
    card = _with_patched_backends(
        ollama_installed=["llama3"],
        ollama_loaded=["llama3"],
        lmstudio_models=["qwen3-235b-a22b"],
        mlx_lm_models=["mlx-community/Qwen2.5-7B-Instruct-4bit"],
        fn=lambda: llm_stream.build_profile_card("test-agent"),
    )

    assert card["models"]["loaded"][0] == "llama3", (
        f"loaded[0]がOllama側のモデルになっていません: {card['models']['loaded']}"
    )
    assert card["models"]["backends"] == ["ollama", "mlx_lm", "lmstudio"], (
        f"backendsの並び順がOllama→MLX-LM→LM Studioになっていません: {card['models']['backends']}"
    )


def test_build_profile_card_merges_ollama_and_lmstudio_context_lengths():
    """models.context_lengthsに、OllamaとLM Studio両方のモデルの値が
    含まれることを確認する(get_lmstudio_context_lengthsが追加される前は
    LM Studio・MLX-LM経由のメンバーでは常に空になっていた不具合の検証)。
    """
    originals = {
        "get_ollama_installed_models": yoriai.get_ollama_installed_models,
        "get_ollama_loaded_models": yoriai.get_ollama_loaded_models,
        "get_lmstudio_models": yoriai.get_lmstudio_models,
        "get_lmstudio_context_lengths": yoriai.get_lmstudio_context_lengths,
        "get_mlx_lm_models": yoriai.get_mlx_lm_models,
        "get_ollama_context_length": yoriai.get_ollama_context_length,
    }
    yoriai.get_ollama_installed_models = lambda: ["llama3"]
    yoriai.get_ollama_loaded_models = lambda: ["llama3"]
    yoriai.get_lmstudio_models = lambda: ["qwen3-235b-a22b"]
    yoriai.get_lmstudio_context_lengths = lambda: {"qwen3-235b-a22b": 262144}
    yoriai.get_mlx_lm_models = lambda: []
    yoriai.get_ollama_context_length = lambda model: 131072
    try:
        card = llm_stream.build_profile_card("test-agent")
    finally:
        for name, original in originals.items():
            setattr(yoriai, name, original)

    assert card["models"]["context_lengths"] == {
        "llama3": 131072,
        "qwen3-235b-a22b": 262144,
    }, card["models"]["context_lengths"]


def test_lmstudio_used_when_mlx_lm_not_running():
    """MLX-LMが動いていない場合は、これまで通りLM Studio側のモデルが
    選ばれることを確認する(MLX-LM優先化がLM Studioを排除していないことの確認)。
    """
    card = _with_patched_backends(
        ollama_installed=[],
        ollama_loaded=[],
        lmstudio_models=["qwen3-235b-a22b"],
        mlx_lm_models=[],
        fn=lambda: llm_stream.build_profile_card("test-agent"),
    )

    assert card["models"]["loaded"][0] == "qwen3-235b-a22b", (
        f"MLX-LM未使用時はLM Studio側が選ばれるはずです: {card['models']['loaded']}"
    )
    assert card["models"]["backends"] == ["lmstudio"], card["models"]["backends"]


def main():
    tests = [
        test_mlx_lm_prioritized_over_lmstudio_when_no_ollama,
        test_ollama_still_wins_over_mlx_lm_and_lmstudio,
        test_build_profile_card_merges_ollama_and_lmstudio_context_lengths,
        test_lmstudio_used_when_mlx_lm_not_running,
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
