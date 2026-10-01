#!/usr/bin/env python3
"""LLMに繋がらないノード(LM Studio/Ollama未導入のraspi4等)を、推論系の
作業候補から一時的に除外することを検証する。

- 疎通に失敗するピアは候補から外れ、成功すれば(次の確認で)戻ること
- 除外されたピアも、ステータス表示(⏳ <device> 待機)には残ること
- 確認の過程で`localhost:1234`への問い合わせが発生しないこと

使い方: python3 tests/test_llm_unavailable_exclusion.py
"""
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _card(name, loaded):
    return {
        "device_name": name,
        "memory": {"free_gb": 10, "total_gb": 16},
        "models": {"installed": loaded, "loaded": loaded, "backends": ["lmstudio"] if loaded else []},
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _fake_network(peer_cards, requested):
    """`/status`(自分+ピア2台のスナップショット)と、ピアごとの`/card`を模擬する。
    peer_cards[address]がNoneのピアは接続失敗(ConnectionError)になる。"""
    snapshot = {
        "self": _card("macmini", ["qwen2.5-coder-14b"]),
        "peers": [
            {"card": _card("macstudio", ["qwen2.5-coder-32b"]), "address": "macstudio", "port": 47120},
            {"card": _card("raspi4", []), "address": "raspi4", "port": 47120},
        ],
    }

    def fake_get(url, **_kwargs):
        requested.append(url)
        if url.endswith("/status"):
            return _Resp(snapshot)
        host = url.split("//")[1].split(":")[0]
        card = peer_cards.get(host)
        if card is None:
            raise ConnectionError(f"refused: {url}")
        return _Resp(card)

    return fake_get


def _select(peer_cards, requested):
    original = yoriai.requests.get
    yoriai.requests.get = _fake_network(peer_cards, requested)
    try:
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            data = yoriai._fetch_org_snapshot(47120, "fp")
    finally:
        yoriai.requests.get = original
    candidates = yoriai._select_chat_candidates(data["self"], data["peers"], 47120)
    return data, candidates, buf.getvalue()


def test_unreachable_peer_is_excluded_and_logged():
    requested = []
    data, candidates, out = _select(
        {"macstudio": _card("macstudio", ["qwen2.5-coder-32b"]), "raspi4": None}, requested,
    )
    labels = {c["label"] for c in candidates}
    assert labels == {"macmini(自分)", "macstudio"}, labels
    assert "raspi4: LLM未検出のため推論タスクの対象外" in out, out
    # ピア自体は一覧に残る(ステータス表示用)
    assert any(p["card"]["device_name"] == "raspi4" for p in data["peers"])


def test_peer_without_loaded_models_is_excluded():
    """カードは返るがLLMがロードされていない(LM Studio未起動)ピアも除外される。"""
    requested = []
    _, candidates, _ = _select(
        {"macstudio": _card("macstudio", ["qwen2.5-coder-32b"]), "raspi4": _card("raspi4", [])}, requested,
    )
    assert "raspi4" not in {c["label"] for c in candidates}


def test_peer_returns_when_llm_comes_back():
    requested = []
    _, candidates, _ = _select(
        {"macstudio": _card("macstudio", ["qwen2.5-coder-32b"]), "raspi4": None}, requested,
    )
    assert "raspi4" not in {c["label"] for c in candidates}
    # LM Studioが起動した次の確認では復帰する(状態を持ち越さない)
    _, candidates, out = _select(
        {"macstudio": _card("macstudio", ["qwen2.5-coder-32b"]), "raspi4": _card("raspi4", ["llama3.2"])}, requested,
    )
    assert "raspi4" in {c["label"] for c in candidates}
    assert "LLM未検出" not in out, out


def test_excluded_peer_still_shown_in_status_panel():
    requested = []
    data, _, _ = _select({"macstudio": _card("macstudio", ["m"]), "raspi4": None}, requested)
    known = {p["card"]["device_name"] for p in data["peers"]}
    board = yoriai._DeviceStatusBoard()
    board.set("macstudio", yoriai._DEVICE_STATUS_WORKING, "storage.py を実装中")
    panel = yoriai._render_status_panel(board, known)
    assert "⏳ raspi4 待機" in panel.split("\n"), panel


def test_no_request_to_localhost_1234_during_peer_check():
    requested = []
    _select({"macstudio": _card("macstudio", ["m"]), "raspi4": None}, requested)
    assert requested, "疎通確認のリクエストが一切発生していません"
    assert not any(":1234" in url for url in requested), requested


def test_quiet_polling_does_not_probe_peers():
    requested = []
    original = yoriai.requests.get
    yoriai.requests.get = _fake_network({"raspi4": None}, requested)
    try:
        yoriai._fetch_org_snapshot(47120, "fp", quiet=True)
    finally:
        yoriai.requests.get = original
    assert len(requested) == 1 and requested[0].endswith("/status"), requested


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
