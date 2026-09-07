#!/usr/bin/env python3
"""対話モード(`--chat`)のステータスパネル(参加デバイス全員の「今何を
しているか」を、入力行の直上に常時表示する`bottom_toolbar`)を検証する。

対象は`_DeviceStatusBoard`(状態を保持するスレッドセーフな共有レジストリ)、
`_format_device_status_line`(1デバイス分の表示行の組み立て)、
`_render_status_panel`(パネル全体のテキスト組み立て)の3つ。

- 参加デバイス数の増減に応じて行数が変わること
- 状態種別(思考中🧠・実装中💻・待機中⏳)ごとにアイコン・文言が正しいこと
- 経過時間の表示フォーマット(思考中のみ"(Ns)"が付く)が正しいこと
- デバイスが離脱した場合(`remove`)、一覧から正しく消えること
- 複数のバックグラウンドスレッドから同時に更新しても壊れないこと
  (スレッドセーフ性)
に加え、実際のタスクキュー方式(`_run_collaborative_task_queue`)を
1台構成で走らせた後、ステータスパネルの一覧が空に戻ることも確認する。

使い方: python3 tests/test_status_panel.py
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_panel_empty_when_no_devices():
    board = yoriai._DeviceStatusBoard()
    assert yoriai._render_status_panel(board) == ""


def test_panel_line_count_scales_with_participant_count():
    """参加デバイス数の増減に応じて、パネルの行数が正しく変わることを
    確認する(2台構成・5台構成の両方)。
    """
    board = yoriai._DeviceStatusBoard()
    two_devices = ["MacStudio", "raspi4"]
    for label in two_devices:
        board.set(label, yoriai._DEVICE_STATUS_WAITING)
    panel = yoriai._render_status_panel(board)
    assert len(panel.split("\n")) == 2, panel

    five_devices = ["MacStudio", "raspi4", "junnoMac-mini", "orin-nano", "pi-zero"]
    for label in five_devices:
        board.set(label, yoriai._DEVICE_STATUS_WAITING)
    panel = yoriai._render_status_panel(board)
    assert len(panel.split("\n")) == 5, panel


def test_status_icons_and_text_per_kind():
    """状態種別ごとにアイコン・文言が正しく生成されることを確認する。"""
    line = yoriai._format_device_status_line(
        "MacStudio", yoriai._DEVICE_STATUS_THINKING, "", elapsed_seconds=42,
    )
    assert line == "🧠 MacStudio が 処理中... (42s)", line

    line = yoriai._format_device_status_line(
        "junnoMac-mini", yoriai._DEVICE_STATUS_WORKING, "storage.py を実装中", elapsed_seconds=99,
    )
    assert line == "💻 junnoMac-mini が storage.py を実装中", line

    line = yoriai._format_device_status_line(
        "raspi4", yoriai._DEVICE_STATUS_WAITING, "", elapsed_seconds=0,
    )
    assert line == "⏳ raspi4 待機", line


def test_elapsed_time_is_shown_only_for_thinking():
    """経過時間の表示は「思考中」にのみ付き、「実装中」「待機中」には
    付かないことを確認する(依頼の「混在してよい」という要件への対応)。
    """
    thinking_line = yoriai._format_device_status_line(
        "MacStudio", yoriai._DEVICE_STATUS_THINKING, "", elapsed_seconds=7,
    )
    assert "(7s)" in thinking_line, thinking_line

    working_line = yoriai._format_device_status_line(
        "junnoMac-mini", yoriai._DEVICE_STATUS_WORKING, "cli.py を実装中", elapsed_seconds=7,
    )
    assert "(7s)" not in working_line, working_line
    assert "7" not in working_line, working_line

    waiting_line = yoriai._format_device_status_line(
        "raspi4", yoriai._DEVICE_STATUS_WAITING, "", elapsed_seconds=7,
    )
    assert "(7s)" not in waiting_line, waiting_line


def test_elapsed_seconds_reflect_render_time():
    """`_render_status_panel`が実際の経過時間(呼び出し時点との差分)を
    計算して埋め込むことを確認する。
    """
    board = yoriai._DeviceStatusBoard()
    board.set("MacStudio", yoriai._DEVICE_STATUS_THINKING, "")
    time.sleep(1.1)
    panel = yoriai._render_status_panel(board)
    assert "(1s)" in panel or "(2s)" in panel, panel


def test_device_removed_from_panel_when_it_leaves():
    """デバイスが離脱した(=`remove`が呼ばれた)場合、一覧から正しく
    消えることを確認する。
    """
    board = yoriai._DeviceStatusBoard()
    board.set("MacStudio", yoriai._DEVICE_STATUS_WORKING, "storage.py を実装中")
    board.set("raspi4", yoriai._DEVICE_STATUS_WAITING)
    assert len(yoriai._render_status_panel(board).split("\n")) == 2

    board.remove("MacStudio")
    panel = yoriai._render_status_panel(board)
    assert "MacStudio" not in panel, panel
    assert "raspi4" in panel, panel
    assert len(panel.split("\n")) == 1


def test_remove_of_unknown_label_is_a_no_op():
    board = yoriai._DeviceStatusBoard()
    board.set("MacStudio", yoriai._DEVICE_STATUS_WAITING)
    board.remove("no-such-device")
    assert "MacStudio" in yoriai._render_status_panel(board)


def test_clear_removes_all_devices():
    board = yoriai._DeviceStatusBoard()
    for label in ("MacStudio", "raspi4", "junnoMac-mini"):
        board.set(label, yoriai._DEVICE_STATUS_WAITING)
    board.clear()
    assert yoriai._render_status_panel(board) == ""


def test_concurrent_updates_from_many_threads_do_not_corrupt_state():
    """複数のバックグラウンドスレッドから同時に`set`・`remove`が呼ばれても
    (`_run_collaborative_task_queue`等の実際のワーカー構成を想定)、
    例外を起こさず最終状態が一貫していることを確認する(スレッド
    セーフ性)。
    """
    board = yoriai._DeviceStatusBoard()
    device_count = 20
    updates_per_device = 200
    labels = [f"device-{i}" for i in range(device_count)]
    errors = []

    def hammer(label):
        try:
            for i in range(updates_per_device):
                kind = (yoriai._DEVICE_STATUS_THINKING, yoriai._DEVICE_STATUS_WORKING, yoriai._DEVICE_STATUS_WAITING)[i % 3]
                board.set(label, kind, f"detail-{i}")
                board.snapshot()
            board.remove(label)
        except Exception as exc:  # pragma: no cover - このテストで検出したい失敗そのもの
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(label,)) for label in labels]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    # 全スレッドが最後に`remove`するため、最終的には空になっているはず。
    assert yoriai._render_status_panel(board) == ""


def _candidate(label, model):
    return {"label": label, "model": model, "address": "127.0.0.1", "port": 47120}


def _fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
    text = messages[0]["content"]
    if "レビュー対象" in text:
        yield {"content": "問題なし"}
    else:
        yield {"content": "```python\npass\n```"}
    yield {"done": True}


def test_task_queue_clears_status_board_after_completion():
    """実際の`_run_collaborative_task_queue`(協業モードのタスクキュー)を
    1台構成で走らせた後、ステータスパネルの一覧が空に戻る(=モジュール
    単一のステータスボードにこのジョブの残骸が残らない)ことを確認する。
    """
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = _fake_stream_ok
    yoriai._ACTIVE_STATUS_BOARD.clear()

    tasks = [("app.py", "アプリ本体")]
    candidates = [_candidate("SoloMember", "model-a")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_status_panel_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._run_collaborative_task_queue(tasks, candidates, "fingerprint", out_dir, checklist)
    finally:
        yoriai._stream_chat_from_candidate = original_stream
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)

    assert yoriai._render_status_panel(yoriai._ACTIVE_STATUS_BOARD) == ""


def main():
    tests = [
        test_panel_empty_when_no_devices,
        test_panel_line_count_scales_with_participant_count,
        test_status_icons_and_text_per_kind,
        test_elapsed_time_is_shown_only_for_thinking,
        test_elapsed_seconds_reflect_render_time,
        test_device_removed_from_panel_when_it_leaves,
        test_remove_of_unknown_label_is_a_no_op,
        test_clear_removes_all_devices,
        test_concurrent_updates_from_many_threads_do_not_corrupt_state,
        test_task_queue_clears_status_board_after_completion,
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
