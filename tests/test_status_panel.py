#!/usr/bin/env python3
"""対話モード(`--chat`)のステータスパネル(参加デバイス全員の「今何を
しているか」を、入力行の直上に常時表示するパネル)を検証する。

対象は`_DeviceStatusBoard`(ジョブ参加状況を保持するスレッドセーフな共有
レジストリ)、`_LiveOrgMembers`(定期ポーリングで把握した現在オンラインの
組織メンバーを保持するスレッドセーフな共有レジストリ)、
`_format_device_status_line`(1デバイス分の表示行の組み立て)、
`_render_status_panel`(両者を突き合わせたパネル全体のテキスト組み立て)、
`_fetch_known_member_labels`(組織スナップショットからラベル集合を作る)の
5つ。

仮の判断(実機バグ報告への対応): 当初`bottom_toolbar`を使っていたが、
`patch_stdout()`が高頻度に`print()`する状況(協業モードの思考過程表示等)
では`prompt_toolkit`自体のCPR(カーソル位置問い合わせ)依存の挙動により
パネルが二度と表示されなくなる不具合を実機・疑似端末の両方で確認した。
現在は`_attach_status_panel`が、既存のレイアウト全体をCPRに依存しない
自前の`Window`でラップする方式に変更している(詳細は`yoriai.py`の
`_attach_status_panel`・ステータスパネルのセクション冒頭のコメントを参照)。

- 参加デバイス数の増減に応じて行数が変わること
- 状態種別(思考中🧠・実装中💻・待機中⏳)ごとにアイコン・文言が正しいこと
- 経過時間の表示フォーマット(思考中のみ"(Ns)"が付く)が正しいこと
- デバイスが離脱した場合(`remove`)、一覧から正しく消えること
- 複数のバックグラウンドスレッドから同時に更新しても壊れないこと
  (スレッドセーフ性)
- ジョブに参加していない(が組織には接続している)デバイスも、待機中の
  1行としてパネルに表示され続けること(参加台数のリアルタイム増減対応)
に加え、実際のタスクキュー方式(`_run_collaborative_task_queue`)を
1台構成で走らせた後、ステータスパネルの一覧が空に戻ることと、
`_create_repl_prompt_session`(本番のセッション構築処理そのもの)が
レイアウトを組み替えた後も実際に入力を受け付けられることを確認する。

使い方: python3 tests/test_status_panel.py
"""
import contextlib
import io
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402
from _repl_test_support import run_repl_session_with_keys  # noqa: E402

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402


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


# ---------------------------------------------------------------------------
# 参加台数のリアルタイム増減対応(_LiveOrgMembers・_fetch_known_member_labels・
# _render_status_panelのknown_labels引数)
# ---------------------------------------------------------------------------

def test_live_org_members_update_and_snapshot():
    members = yoriai._LiveOrgMembers()
    assert members.snapshot() == set()
    members.update({"MacStudio", "raspi4"})
    assert members.snapshot() == {"MacStudio", "raspi4"}
    members.update({"junnoMac-mini"})
    assert members.snapshot() == {"junnoMac-mini"}, "updateのたびに集合全体が置き換わるはず"


def test_fetch_known_member_labels_matches_build_chat_candidate_label_format():
    """`_fetch_known_member_labels`が組み立てるラベルが、
    `_build_chat_candidate`(`_ACTIVE_STATUS_BOARD`が実際に使うラベルの
    出処)と完全に同じ形式(自分は"(自分)"付き、ピアはそのまま)である
    ことを確認する。ここがずれると、同一デバイスが2行として重複表示
    されてしまう。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False, quiet=False: {
        "self": {"device_name": "MacStudio"},
        "peers": [
            {"card": {"device_name": "junnoMac-mini"}},
            {"card": {"device_name": "raspi4"}},
        ],
    }
    try:
        labels = yoriai._fetch_known_member_labels(47120, "fingerprint")
    finally:
        yoriai._fetch_org_snapshot = original_snapshot

    assert labels == {"MacStudio(自分)", "junnoMac-mini", "raspi4"}, labels


def test_fetch_known_member_labels_returns_empty_set_when_snapshot_unavailable():
    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False, quiet=False: None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            labels = yoriai._fetch_known_member_labels(47120, "fingerprint")
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
    assert labels == set()


def test_fetch_org_snapshot_quiet_mode_prints_nothing_on_connection_failure():
    """`_poll_org_members_forever`のような数秒おきの繰り返し呼び出しで
    キッチンに接続できない場合でも、`quiet=True`なら失敗の案内文を
    一切出力しないことを確認する(実際の`requests.get`を、到達不能な
    ポートに対して本物のまま実行する。詳細は`_fetch_org_snapshot`の
    `quiet`引数のコメントを参照: これが無いと、対話モードの画面が
    キッチン一時停止のたびに延々とエラー表示で埋め尽くされてしまう)。
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = yoriai._fetch_org_snapshot(47120, "fingerprint", quiet=True)
    assert result is None
    assert buf.getvalue() == "", repr(buf.getvalue())


def test_panel_shows_idle_known_members_alongside_active_job_participants():
    """ジョブに参加していない(が組織には接続している)デバイスも、
    待機中の1行としてパネルに表示され続けることを確認する(依頼の
    「参加台数の行数をちゃんと確保してほしい」への対応の中核)。
    """
    board = yoriai._DeviceStatusBoard()
    board.set("MacStudio", yoriai._DEVICE_STATUS_WORKING, "storage.py を実装中")
    # raspi4はジョブには参加していないが、組織には接続中(known_labelsのみに存在)。
    known_labels = {"MacStudio", "raspi4"}

    panel = yoriai._render_status_panel(board, known_labels)
    lines = panel.split("\n")
    assert len(lines) == 2, panel
    assert any("MacStudio" in line and "実装中" in line for line in lines), panel
    assert any(line == "⏳ raspi4 待機" for line in lines), panel


def test_panel_known_labels_defaults_to_board_only_for_backward_compatibility():
    """`known_labels`を渡さない既存の呼び出し(`test_task_queue_clears_
    status_board_after_completion`等)が、これまで通り`board`の内容
    だけで完結することを確認する(後方互換性の回帰検知)。
    """
    board = yoriai._DeviceStatusBoard()
    board.set("MacStudio", yoriai._DEVICE_STATUS_WAITING)
    assert yoriai._render_status_panel(board) == "⏳ MacStudio 待機"


def test_panel_union_shows_board_only_labels_even_if_poll_has_not_caught_up():
    """`known_labels`にまだ反映されていない(ポーリングが追いついて
    いない)デバイスでも、`board`に載っていれば表示から漏れないことを
    確認する(和集合を取っていることの検証)。
    """
    board = yoriai._DeviceStatusBoard()
    board.set("new-device", yoriai._DEVICE_STATUS_THINKING, "")
    panel = yoriai._render_status_panel(board, known_labels=set())
    assert "new-device" in panel, panel


# ---------------------------------------------------------------------------
# _create_repl_prompt_session本番のセッション構築処理そのものが、フルスクリーン
# 化した後も実際に入力を受け付けられることの回帰検知
# ---------------------------------------------------------------------------

def test_create_repl_prompt_session_still_accepts_input_after_layout_wrap():
    """`_attach_full_screen_chat_ui`が`session.app.layout`を組み替え、
    `full_screen=True`にした後も、デフォルトバッファ(入力欄)への
    フォーカスが正しく保たれ、実際にメッセージを送信できることを
    確認する(`bottom_toolbar`→自前レイアウト→フルスクリーン化と、
    土台を作り変えるたびに既存の入力機能への回帰が無いことを検証する)。
    """
    yoriai._ACTIVE_ORG_MEMBERS.update(set())
    text, terminate = run_repl_session_with_keys("こんにちは\r", message_count=1)[0]
    assert terminate is False
    assert text == "こんにちは", repr(text)


def test_create_repl_prompt_session_still_configures_prompt_and_multiline():
    """実機バグの再発防止(pty経由の疑似端末検証で発見): `Application`を
    セッションの生存期間中1つだけ動かし続ける設計に変更した際、以前は
    `session.prompt(message=..., multiline=True, ...)`の引数として渡して
    いた`message`("Yoriai> ")・`multiline`・`prompt_continuation`が、
    `.prompt()`自体を呼ばなくなったことで渡す場所を失い、黙って既定値
    (`message=""`・`multiline=False`)に戻ってしまっていた。この結果、
    入力プロンプト"Yoriai> "が実機の画面に一切表示されなくなる不具合が
    発生したが、`DummyOutput`を使う既存のテスト群(実際の描画内容を
    見ない)では検出できず、pty(疑似端末)+生バイト列の検証で初めて
    発見できた。`_create_repl_prompt_session()`がこれらを正しく
    コンストラクタ引数として設定していることを直接確認する(実際の
    描画までは検証しないが、設定漏れの再発は確実に検知できる)。
    """
    with create_pipe_input() as pipe_input, create_app_session(input=pipe_input, output=DummyOutput()):
        session = yoriai._create_repl_prompt_session()
        assert session.message == yoriai._REPL_PROMPT, session.message
        assert session.multiline is True, session.multiline
        assert session.prompt_continuation is yoriai._repl_prompt_continuation


# ---------------------------------------------------------------------------
# _ChatOutputRouter・_chat_output_context・ログ欄バッファ(patch_stdout廃止後の
# フルスクリーンUIへの出力差し込み)
# ---------------------------------------------------------------------------

def test_chat_output_router_forces_output_creation_before_swapping_stdout():
    """`_ChatOutputRouter`のコンストラクタが、`sys.stdout`を差し替える前に
    `AppSession.output`(遅延生成)を強制的に確定させておくことを確認する。

    仮の判断(実機バグ修正・重要): これが無いと、`PromptSession`が後で
    初めて`.output`にアクセスする時点では既に`sys.stdout`がこの
    `_ChatOutputRouter`(`isatty()`は常に`False`)に差し替わっており、
    実端末ではなく非対話的な出力先だと誤判定されて画面が一切描画され
    なくなる(疑似端末を使った実機同等の再現で「1バイトも出力されない」
    不具合として実際に特定した)。`patch_stdout.StdoutProxy.__init__`も
    全く同じ理由で`sys.stdout`を差し替える前に`self.app_session.output`
    を参照しており、同じ対策をここでも踏襲している。
    """
    with create_app_session() as app_session:
        # 親セッションが既にOutputを持っている場合があるため、前提条件
        # (=まだ誰も`.output`にアクセスしていない状態)を明示的に作る。
        app_session._output = None
        yoriai._ChatOutputRouter(sys.stdout)
        assert app_session._output is not None, (
            "コンストラクタの時点でAppSession.outputの遅延生成を強制するべきです"
        )


def test_chat_output_router_writes_to_log_buffer_and_invalidates_when_app_running():
    """フルスクリーンApplicationが動いている間(`app_session.app`が
    設定されている間)は、印字内容がログ欄のバッファへ追記され、
    `Application.invalidate()`が呼ばれることを確認する。
    """
    yoriai._reset_chat_log_buffer()
    invalidated = {"count": 0}

    class _FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

    class _FakeApp:
        loop = _FakeLoop()

        def invalidate(self):
            invalidated["count"] += 1

    with create_app_session() as app_session:
        app_session._output = object()  # .outputへのアクセスで例外にならないよう仮の値を入れる
        router = yoriai._ChatOutputRouter(sys.stdout)
        app_session.app = _FakeApp()
        try:
            router.write("[議題] こんにちは\n")
        finally:
            app_session.app = None

    assert "[議題] こんにちは" in yoriai._CHAT_LOG_BUFFER.text
    assert invalidated["count"] == 1


def test_chat_output_router_writes_to_real_output_when_app_not_running():
    """フルスクリーンApplicationが動いていない間(単発質問等の同期処理中)
    は、印字内容がログ欄のバッファに記録されつつ、その場で見えるよう
    素の出力先へも直接書き出されることを確認する。
    """
    yoriai._reset_chat_log_buffer()

    class _FakeRealOutput:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)

        def flush(self):
            pass

    fake_real_output = _FakeRealOutput()
    with create_app_session() as app_session:
        app_session._output = object()
        router = yoriai._ChatOutputRouter(fake_real_output)
        assert app_session.app is None
        router.write("実行中のYoriaiエージェントに接続できませんでした。\n")

    assert "実行中のYoriaiエージェントに接続できませんでした。" in yoriai._CHAT_LOG_BUFFER.text
    assert fake_real_output.written == ["実行中のYoriaiエージェントに接続できませんでした。\n"]


def test_reset_chat_log_buffer_clears_content():
    yoriai._CHAT_LOG_BUFFER.set_document(
        yoriai.Document("残っていてはいけない古い内容"), bypass_readonly=True,
    )
    yoriai._reset_chat_log_buffer()
    assert yoriai._CHAT_LOG_BUFFER.text == ""


def test_chat_log_buffer_caps_size_by_dropping_oldest_content():
    """長時間の`--chat`セッションでログ欄が際限なく肥大化しないよう、
    上限を超えたら古い方から切り捨てられることを確認する。
    """
    yoriai._reset_chat_log_buffer()

    class _FakeLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, fn, *args):
            fn(*args)

    class _FakeApp:
        loop = _FakeLoop()

        def invalidate(self):
            pass

    with create_app_session() as app_session:
        app_session._output = object()
        router = yoriai._ChatOutputRouter(sys.stdout)
        app_session.app = _FakeApp()  # 実際のsys.stdoutへ書き出させないため
        try:
            router.write("A" * (yoriai._CHAT_LOG_BUFFER_MAX_CHARS - 10))
            router.write("B" * 100)
        finally:
            app_session.app = None

    text = yoriai._CHAT_LOG_BUFFER.text
    assert len(text) == yoriai._CHAT_LOG_BUFFER_MAX_CHARS
    assert text.endswith("B" * 100), "末尾(新しい内容)は保持されるはずです"
    assert "A" * (yoriai._CHAT_LOG_BUFFER_MAX_CHARS - 10) not in text, "先頭(古い内容)は切り捨てられるはずです"


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
        test_live_org_members_update_and_snapshot,
        test_fetch_known_member_labels_matches_build_chat_candidate_label_format,
        test_fetch_known_member_labels_returns_empty_set_when_snapshot_unavailable,
        test_fetch_org_snapshot_quiet_mode_prints_nothing_on_connection_failure,
        test_panel_shows_idle_known_members_alongside_active_job_participants,
        test_panel_known_labels_defaults_to_board_only_for_backward_compatibility,
        test_panel_union_shows_board_only_labels_even_if_poll_has_not_caught_up,
        test_create_repl_prompt_session_still_accepts_input_after_layout_wrap,
        test_create_repl_prompt_session_still_configures_prompt_and_multiline,
        test_chat_output_router_forces_output_creation_before_swapping_stdout,
        test_chat_output_router_writes_to_log_buffer_and_invalidates_when_app_running,
        test_chat_output_router_writes_to_real_output_when_app_not_running,
        test_reset_chat_log_buffer_clears_content,
        test_chat_log_buffer_caps_size_by_dropping_oldest_content,
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
