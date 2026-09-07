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


# ---------------------------------------------------------------------------
# 対話プロトコル(_run_dialogue、合意フェーズ・修正フェーズ・レビューフェーズ・
# 計画のみモードすべてで共通利用される中核関数)へのステータスパネル連携
# ---------------------------------------------------------------------------

_PROPOSER_MARKER = "「提案役」として参加しています"
_CRITIC_MARKER = "「反論役」として参加しています"
_INTEGRATOR_MARKER = "「統合役」として参加しています"


def test_run_dialogue_shows_active_speaker_thinking_and_others_waiting():
    """対話プロトコルは提案役→反論役→統合役の順で1人ずつ直列に発言する
    (各役割が前の役割の発言を踏まえてプロンプトを組むため並列化できない)。
    ステータスパネルは、今喋っている役割の担当デバイスだけが「思考中」に
    なり、それ以外の参加デバイスは「待機」のままであることを反映する
    べきで、それを確認する。
    """
    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2"), _candidate("raspi4", "m3")]
    observed = {}

    def fake_collect(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        snapshot = {
            label: status_kind for label, status_kind, _detail, _started in yoriai._ACTIVE_STATUS_BOARD.snapshot()
        }
        observed.setdefault(candidate["label"], []).append(snapshot)
        if _PROPOSER_MARKER in text:
            return "提案内容", None, False
        if _CRITIC_MARKER in text:
            return "特に問題なし\n評価: 合意", None, False
        if _INTEGRATOR_MARKER in text:
            return "判定: 合意\n\n最終合意内容:\n提案内容", None, False
        raise AssertionError(f"想定外の問い合わせです: {text[:80]}")

    original_collect = yoriai._collect_answer_from_candidate
    yoriai._collect_answer_from_candidate = fake_collect
    yoriai._ACTIVE_STATUS_BOARD.clear()
    try:
        result = yoriai._run_dialogue(
            org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
            output_instruction="形式",
        )
    finally:
        yoriai._collect_answer_from_candidate = original_collect

    assert result["status"] == yoriai.DIALOGUE_STATUS_CONSENSUS, result

    # ラウンド1の提案役(MacStudio)呼び出し時点: 自分は思考中、他は待機。
    macstudio_snapshot = observed["MacStudio"][0]
    assert macstudio_snapshot["MacStudio"] == yoriai._DEVICE_STATUS_THINKING, macstudio_snapshot
    assert macstudio_snapshot["junnoMac-mini"] == yoriai._DEVICE_STATUS_WAITING, macstudio_snapshot
    assert macstudio_snapshot["raspi4"] == yoriai._DEVICE_STATUS_WAITING, macstudio_snapshot

    # ラウンド1の反論役(junnoMac-mini)呼び出し時点: 提案役は待機へ戻り、
    # 反論役だけが思考中になる。
    junno_snapshot = observed["junnoMac-mini"][0]
    assert junno_snapshot["MacStudio"] == yoriai._DEVICE_STATUS_WAITING, junno_snapshot
    assert junno_snapshot["junnoMac-mini"] == yoriai._DEVICE_STATUS_THINKING, junno_snapshot
    assert junno_snapshot["raspi4"] == yoriai._DEVICE_STATUS_WAITING, junno_snapshot

    # ラウンド1の統合役(raspi4)呼び出し時点: 提案役・反論役は待機へ戻る。
    raspi4_snapshot = observed["raspi4"][0]
    assert raspi4_snapshot["MacStudio"] == yoriai._DEVICE_STATUS_WAITING, raspi4_snapshot
    assert raspi4_snapshot["junnoMac-mini"] == yoriai._DEVICE_STATUS_WAITING, raspi4_snapshot
    assert raspi4_snapshot["raspi4"] == yoriai._DEVICE_STATUS_THINKING, raspi4_snapshot


def test_run_dialogue_clears_status_board_after_completion():
    """`_run_dialogue`の終了経路(このテストでは早期の人間確認への
    エスカレーション)を通ったあと、参加デバイス全員がステータスパネルの
    一覧から消えることを確認する(`_finish_dialogue`に集約した後始末の
    検証)。
    """
    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]

    def fake_collect(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            return "とりあえずの案", None, False
        if _CRITIC_MARKER in text:
            return "議論を重ねてもアイデアが出ません。\n評価: 情報不足", None, False
        if _INTEGRATOR_MARKER in text:
            return "判定: 人間に確認\n\n人間への確認事項:\nどの方向性で進めるべきか助言をください。", None, False
        raise AssertionError(f"想定外の問い合わせです: {text[:80]}")

    original_collect = yoriai._collect_answer_from_candidate
    yoriai._collect_answer_from_candidate = fake_collect
    yoriai._ACTIVE_STATUS_BOARD.clear()
    try:
        result = yoriai._run_dialogue(
            org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
            output_instruction="形式",
        )
    finally:
        yoriai._collect_answer_from_candidate = original_collect

    assert result["status"] == yoriai.DIALOGUE_STATUS_NEEDS_HUMAN, result
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
        test_run_dialogue_shows_active_speaker_thinking_and_others_waiting,
        test_run_dialogue_clears_status_board_after_completion,
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
