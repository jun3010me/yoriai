#!/usr/bin/env python3
"""同一デバイスがmDNSとTailscaleの両方の経路で発見された場合に、
`PeerRegistry`が1つのメンバーとして統合することを検証する。

実機で、同一LAN内かつ同じTailnetにいるデバイス(junnoMac-mini)が
mDNS経由・Tailscale経由の両方で発見され、--statusに別々のメンバーとして
2件表示される不具合が報告された。原因は、以前の実装が発見経路ごとに
`agent_id`(常駐エージェントのプロセス起動のたびにランダム生成され、
永続化されない値)をキーにしていたため、常駐エージェントの再起動などで
この値がずれると、本来同一のデバイスでも別々のエントリとして扱われて
しまうことだった。自己紹介カードの`device_name`(短いホスト名)を
一意な識別子とみなして統合するよう変更したことを検証する。

使い方: python3 tests/test_peer_dedup.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _card(device_name):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": 20, "total_gb": 32},
        "models": {"installed": ["qwen2.5-14b"], "loaded": ["qwen2.5-14b"], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def test_same_device_seen_via_mdns_and_tailscale_merges_into_one_peer():
    """同一デバイス(同じdevice_name・同じagent_id)がmDNS経由・Tailscale経由の
    両方でupsertされても、snapshot()には1件しか現れないことを確認する。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)

    snapshot = registry.snapshot()
    assert len(snapshot) == 1, (
        f"同一デバイスなのに複数エントリになっています: {snapshot}"
    )
    assert snapshot[0]["via"] == ["mDNS", "Tailscale"], (
        f"両方の経路がviaに記録されているはずです: {snapshot[0]['via']}"
    )


def test_mdns_address_is_preferred_when_both_paths_present():
    """両方の経路で見えている場合、実際の接続先(address/port)はmDNS側が
    優先されることを確認する(同一LAN内の直接発見を優先する方針)。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)

    snapshot = registry.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["address"] == "192.168.1.10", (
        f"mDNS経由のアドレスが優先されるはずです: {snapshot[0]}"
    )


def test_device_restart_with_new_agent_id_does_not_create_duplicate_entry():
    """常駐エージェントが再起動してagent_idが変わっても(hostnameは同じ)、
    同じ経路(mDNS)からの再登録であれば1件に保たれることを確認する
    (実機で報告された不具合の主要な原因の再現)。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    # 再起動前
    registry.upsert("agent-old", card, "mDNS", "192.168.1.10", 47120)
    # 再起動後、新しいagent_idで同じホスト名・同じ経路から再登録される
    registry.upsert("agent-new", card, "mDNS", "192.168.1.10", 47120)

    snapshot = registry.snapshot()
    assert len(snapshot) == 1, (
        f"再起動でagent_idが変わっても、同一デバイスは1件のはずです: {snapshot}"
    )


def test_removing_one_path_keeps_entry_if_other_path_still_present():
    """mDNS側の発見情報が消えても(remove_serviceイベント相当)、
    Tailscale経由でまだ見えている場合はエントリ自体は残ることを確認する。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)

    registry.remove("agent-x")  # mDNS側のremove_serviceが呼ぶ想定

    snapshot = registry.snapshot()
    assert len(snapshot) == 1, snapshot
    assert snapshot[0]["via"] == ["Tailscale"], snapshot[0]["via"]
    assert snapshot[0]["address"] == "100.64.0.5", snapshot[0]


def test_removing_both_paths_removes_entry_entirely():
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)
    registry.remove("agent-x")

    assert registry.snapshot() == []


def test_sync_tailscale_removes_stale_tailscale_path_but_keeps_mdns():
    """Tailscaleの最新スキャンで見つからなくなったデバイスは、Tailscale側の
    発見情報だけが消え、mDNS側でまだ見えていればエントリは残ることを確認する。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)

    registry.sync_tailscale([])  # 最新スキャンでは見つからなかった

    snapshot = registry.snapshot()
    assert len(snapshot) == 1, snapshot
    assert snapshot[0]["via"] == ["mDNS"], snapshot[0]["via"]


def test_sync_tailscale_keeps_path_when_still_found():
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)

    registry.sync_tailscale(["agent-x"])  # 引き続き見つかっている

    snapshot = registry.snapshot()
    assert len(snapshot) == 1
    assert snapshot[0]["via"] == ["Tailscale"]


def test_distinct_devices_are_not_merged():
    """異なるデバイス(異なるdevice_name)は、当然ながら別々のエントリの
    ままであることを確認する(過剰な統合が起きていないことの回帰検知)。
    """
    registry = yoriai.PeerRegistry()
    registry.upsert("agent-a", _card("MacStudio"), "mDNS", "192.168.1.10", 47120)
    registry.upsert("agent-b", _card("junnoMac-mini"), "mDNS", "192.168.1.11", 47120)

    snapshot = registry.snapshot()
    assert len(snapshot) == 2, snapshot
    device_names = {p["card"]["device_name"] for p in snapshot}
    assert device_names == {"MacStudio", "junnoMac-mini"}, device_names


def test_status_display_label_shows_both_paths():
    """--status表示で使うラベル組み立てロジック(via一覧を'/'区切りで
    '<経路>経由'にする)が、依頼で例示された形式になることを確認する。
    """
    registry = yoriai.PeerRegistry()
    card = _card("junnoMac-mini")
    registry.upsert("agent-x", card, "mDNS", "192.168.1.10", 47120)
    registry.upsert("agent-x", card, "Tailscale", "100.64.0.5", 47120)

    peer = registry.snapshot()[0]
    via_list = peer.get("via") or ["unknown"]
    label = f"({' / '.join(f'{v}経由' for v in via_list)})"
    assert label == "(mDNS経由 / Tailscale経由)", label


def main():
    tests = [
        test_same_device_seen_via_mdns_and_tailscale_merges_into_one_peer,
        test_mdns_address_is_preferred_when_both_paths_present,
        test_device_restart_with_new_agent_id_does_not_create_duplicate_entry,
        test_removing_one_path_keeps_entry_if_other_path_still_present,
        test_removing_both_paths_removes_entry_entirely,
        test_sync_tailscale_removes_stale_tailscale_path_but_keeps_mdns,
        test_sync_tailscale_keeps_path_when_still_found,
        test_distinct_devices_are_not_merged,
        test_status_display_label_shows_both_paths,
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
