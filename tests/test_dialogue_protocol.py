#!/usr/bin/env python3
"""対話プロトコル(共通基盤、`_run_dialogue`)と、その各フェーズへの
組み込み(`//agree`の設計フェーズ・`//fix`の修正方針・レビューフェーズの
自己説明対話・新設`//plan-only`)を検証する。

対話プロトコルは「最初に思いついた案がそのまま最後まで固定される」問題への
対応として、複数ノードが役割(提案役・反論役・統合役)を持って複数ラウンド
議論し、合意形成する仕組み。実機のOllama/LM Studio/MLX-LMには接続できない
環境のため、既存のテスト群と同様`_stream_chat_from_candidate`を差し替えて
メンバーを模擬する。

使い方: python3 tests/test_dialogue_protocol.py
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _candidate(label, model, free_gb=10, coding=True):
    return {
        "label": label, "model": model, "address": "127.0.0.1", "port": 47120,
        "free_gb": free_gb, "has_coding_model": coding,
        "specialties": [yoriai.DIALOGUE_SPECIALTY_CODING if coding else yoriai.DIALOGUE_SPECIALTY_GENERAL],
    }


def _make_card(device_name, model, free_gb=10):
    return {
        "agent_id": device_name, "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 32},
        "models": {
            "installed": [model], "loaded": [model], "backends": ["ollama"],
            "specialties": yoriai._infer_specialties([model]),
        },
        "generated_at": "2026-01-01T00:00:00",
    }


def _two_member_snapshot():
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peer_card = _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20)
    return {
        "self": self_card,
        "peers": [{"card": peer_card, "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0}],
    }


_PROPOSER_MARKER = "「提案役」として参加しています"
_CRITIC_MARKER = "「反論役」として参加しています"
_INTEGRATOR_MARKER = "「統合役」として参加しています"


def _with_stream(fake_stream, fn, *args, **kwargs):
    original = yoriai._stream_chat_from_candidate
    yoriai._stream_chat_from_candidate = fake_stream
    try:
        return fn(*args, **kwargs)
    finally:
        yoriai._stream_chat_from_candidate = original


# ---------------------------------------------------------------------------
# 役割割り振り(_assign_discourse_roles)
# ---------------------------------------------------------------------------

def test_assign_discourse_roles_empty_candidates_returns_empty_dict():
    assert yoriai._assign_discourse_roles([]) == {}


def test_assign_discourse_roles_single_candidate_plays_all_roles():
    solo = _candidate("raspi4", "llama3.2")
    roles = yoriai._assign_discourse_roles([solo])
    assert roles[yoriai.DIALOGUE_ROLE_PROPOSER]["label"] == "raspi4"
    assert roles[yoriai.DIALOGUE_ROLE_CRITIC]["label"] == "raspi4"
    assert roles[yoriai.DIALOGUE_ROLE_INTEGRATOR]["label"] == "raspi4"


def test_assign_discourse_roles_two_candidates_second_plays_critic_and_integrator():
    a, b = _candidate("MacStudio", "qwen-coder"), _candidate("junnoMac-mini", "qwen-coder")
    roles = yoriai._assign_discourse_roles([a, b])
    assert roles[yoriai.DIALOGUE_ROLE_PROPOSER]["label"] == "MacStudio"
    assert roles[yoriai.DIALOGUE_ROLE_CRITIC]["label"] == "junnoMac-mini"
    assert roles[yoriai.DIALOGUE_ROLE_INTEGRATOR]["label"] == "junnoMac-mini"


def test_assign_discourse_roles_three_or_more_candidates_get_distinct_roles():
    a, b, c = _candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2"), _candidate("raspi4", "m3")
    roles = yoriai._assign_discourse_roles([a, b, c])
    labels = {roles[r]["label"] for r in (yoriai.DIALOGUE_ROLE_PROPOSER, yoriai.DIALOGUE_ROLE_CRITIC, yoriai.DIALOGUE_ROLE_INTEGRATOR)}
    assert labels == {"MacStudio", "junnoMac-mini", "raspi4"}


# ---------------------------------------------------------------------------
# 発言の判定パーサー
# ---------------------------------------------------------------------------

def test_parse_critic_verdict_extracts_marker_and_defaults_to_needs_fix():
    assert yoriai._parse_critic_verdict("特に問題なし\n評価: 合意") == "合意"
    assert yoriai._parse_critic_verdict("評価: 情報不足") == "情報不足"
    assert yoriai._parse_critic_verdict("よくわからない回答") == "要修正"


def test_parse_integrator_verdict_extracts_marker_and_defaults_to_continue():
    assert yoriai._parse_integrator_verdict("判定: 合意") == "合意"
    assert yoriai._parse_integrator_verdict("判定: 人間に確認") == "人間に確認"
    assert yoriai._parse_integrator_verdict("要領を得ない回答") == "継続"


def test_extract_dialogue_section_falls_back_to_full_text_when_heading_missing():
    assert yoriai._extract_dialogue_section("見出しなしの本文", "最終合意内容:") == "見出しなしの本文"
    assert yoriai._extract_dialogue_section("前置き\n最終合意内容:\nこれが中身", "最終合意内容:") == "これが中身"


# ---------------------------------------------------------------------------
# 対話プロトコルの中核(_run_dialogue)
# ---------------------------------------------------------------------------

def test_run_dialogue_requires_min_rounds_even_when_both_agree_immediately():
    """反論役・統合役が最初のラウンドから「合意」と答えても、
    `min_rounds`(既定2)未満では確定させない(最初の思いつきで固定
    されてしまう問題への対応の核心)。
    """
    plan = "storage.py: 実装する"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": plan}
        elif _CRITIC_MARKER in text:
            yield {"content": "特に問題なし\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": f"判定: 合意\n\n最終合意内容:\n{plan}"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:80]}")
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
        output_instruction="ファイル名: 内容の形式で",
    )

    assert result["status"] == yoriai.DIALOGUE_STATUS_CONSENSUS
    assert result["final_content"].strip() == plan
    # 2ラウンド分(提案・反論・統合 × 2)= 6発言のはず。
    assert result["total_utterances"] == 6, result
    assert len(result["transcript"]) == 6
    assert result["transcript"][0]["role"] == yoriai.DIALOGUE_ROLE_PROPOSER
    assert result["transcript"][-1]["round"] == 2


def test_run_dialogue_critic_flags_information_shortage_escalates_to_human():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "とりあえずの案"}
        elif _CRITIC_MARKER in text:
            yield {"content": "議論を重ねてもアイデアが出ません。\n評価: 情報不足"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 人間に確認\n\n人間への確認事項:\nどの方向性で進めるべきか助言をください。"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
        output_instruction="出力形式の指示",
    )

    assert result["status"] == yoriai.DIALOGUE_STATUS_NEEDS_HUMAN
    assert "助言をください" in result["human_message"]
    assert result["total_utterances"] == 3, "1ラウンド目で人間に確認へ切り替わるはずです"


def test_run_dialogue_pauses_at_safety_limit_of_fifty_utterances():
    """暴走防止: 合意に至らない議論が続いても、全ノードの発言回数の
    合計が50回を超えた時点で強制的に一時停止することを確認する。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "改善案"}
        elif _CRITIC_MARKER in text:
            yield {"content": "まだ懸念があります。\n評価: 要修正"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 継続\n\n議論継続中です。"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
        output_instruction="出力形式の指示", max_rounds=100,
    )

    assert result["status"] == yoriai.DIALOGUE_STATUS_SAFETY_LIMIT
    assert result["total_utterances"] == yoriai.DIALOGUE_SAFETY_LIMIT_UTTERANCES == 50
    assert len(result["transcript"]) == 50


def test_run_dialogue_gives_up_after_max_rounds_without_consensus():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "改善案"}
        elif _CRITIC_MARKER in text:
            yield {"content": "まだ懸念があります。\n評価: 要修正"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 継続\n\n議論継続中です。"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
        output_instruction="出力形式の指示", max_rounds=3,
    )

    assert result["status"] == yoriai.DIALOGUE_STATUS_NEEDS_HUMAN
    assert result["total_utterances"] == 9, "3ラウンド分(3発言×3)で打ち切られるはずです"
    assert "アドバイス" in result["human_message"]


def test_run_dialogue_no_engagement_when_proposer_never_answers():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"content": ""}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題", background="背景", candidates=candidates,
        output_instruction="出力形式の指示",
    )

    assert result["status"] == yoriai.DIALOGUE_STATUS_NO_ENGAGEMENT
    assert result["total_utterances"] == 1


# ---------------------------------------------------------------------------
# 議事録・要約の永続化
# ---------------------------------------------------------------------------

def test_dialogue_log_and_summary_files_are_both_written():
    plan = "a.py: 実装する"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": plan}
        elif _CRITIC_MARKER in text:
            yield {"content": "問題なし\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": f"判定: 合意\n\n最終合意内容:\n{plan}"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    result = _with_stream(
        fake_stream, yoriai._run_dialogue,
        org_fingerprint="fp", topic="議題テスト", background="背景", candidates=candidates,
        output_instruction="出力形式の指示",
    )

    out_dir = tempfile.mkdtemp(prefix="yoriai_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._report_dialogue_result(result, out_dir, "sample", "議題テスト")
        log_path = os.path.join(out_dir, "DIALOGUE_LOG_sample.md")
        summary_path = os.path.join(out_dir, "DIALOGUE_SUMMARY_sample.md")
        assert os.path.isfile(log_path), buf.getvalue()
        assert os.path.isfile(summary_path), buf.getvalue()

        with open(log_path, encoding="utf-8") as f:
            log_content = f.read()
        # 議事録は逐語ログ(誰が・どのラウンドで・何を発言したか)を保持する。
        assert "MacStudio" in log_content and "junnoMac-mini" in log_content
        assert "ラウンド1" in log_content and "ラウンド2" in log_content

        with open(summary_path, encoding="utf-8") as f:
            summary_content = f.read()
        assert "合意" in summary_content
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_report_dialogue_result_skips_files_when_no_engagement():
    result = {
        "status": yoriai.DIALOGUE_STATUS_NO_ENGAGEMENT, "transcript": [], "summary": "",
        "final_content": None, "human_message": None, "total_utterances": 0,
    }
    out_dir = tempfile.mkdtemp(prefix="yoriai_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._report_dialogue_result(result, out_dir, "sample", "議題")
        assert os.listdir(out_dir) == [], "応答が全く得られなかった場合は議事録ファイルを作らないはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# //agree の設計フェーズへの組み込み(enable_dialogue=True)
# ---------------------------------------------------------------------------

def test_ask_organization_collaborate_with_dialogue_reaches_consensus_and_implements():
    plan = "storage.py: add_todo(text: str) -> int を実装する\ncli.py: storage.add_todoを呼び出すCLI"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": plan}
        elif _CRITIC_MARKER in text:
            yield {"content": "問題なし\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": f"判定: 合意\n\n最終合意内容:\n{plan}"}
        elif "レビュー対象" in text:
            yield {"content": "問題なし"}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _with_stream(
                fake_stream, yoriai._ask_organization_collaborate,
                47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir, enable_dialogue=True,
            )
        output = buf.getvalue()

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._generate_project_name("ToDoリストのCLIツールを作って"),
        )
        saved = set(os.listdir(project_dir))
        assert {"storage.py", "cli.py", "PROGRESS.md", "DIALOGUE_LOG_design.md", "DIALOGUE_SUMMARY_design.md"} <= saved, saved
        assert "[✅ 全タスク完了" in output, output
        assert "対話プロトコル" in output
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


def test_ask_organization_collaborate_without_dialogue_flag_keeps_single_call_behavior():
    """`enable_dialogue`を渡さない既存の呼び出し(既存の全テスト)は、
    従来通り設計担当への1回きりの相談のままであることを確認する
    (後方互換性の回帰検知)。
    """
    call_count = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "ファイルに分割する実装計画" in text:
            call_count["n"] += 1
            yield {"content": "storage.py: 実装する"}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_dialogue_test_")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            _with_stream(
                fake_stream, yoriai._ask_organization_collaborate,
                47120, "fingerprint", "何かを作って", out_dir,
            )
        assert call_count["n"] == 1, "enable_dialogue無指定では従来通り1回だけ相談するはずです"
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


def test_ask_organization_collaborate_with_dialogue_aborts_without_human_consensus():
    """対話が合意に至らなかった場合、実装フェーズには一切進まず、
    議事録・要約だけを残して終了することを確認する(依頼1.4: 対話
    プロトコルだけで次のフェーズへ自動的に進むことはしない)。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "案"}
        elif _CRITIC_MARKER in text:
            yield {"content": "アイデアが尽きました。\n評価: 情報不足"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 人間に確認\n\n人間への確認事項:\n方向性を決めてください。"}
        else:
            raise AssertionError(f"実装フェーズに進んではいけません: {text[:80]}")
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _with_stream(
                fake_stream, yoriai._ask_organization_collaborate,
                47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir, enable_dialogue=True,
            )
        output = buf.getvalue()
        assert "方向性を決めてください" in output

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._generate_project_name("ToDoリストのCLIツールを作って"),
        )
        saved = set(os.listdir(project_dir))
        assert saved == {"DIALOGUE_LOG_design.md", "DIALOGUE_SUMMARY_design.md"}, (
            f"議事録・要約以外の生成物(コード等)は作られないはずです: {saved}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# //fix の修正方針への組み込み
# ---------------------------------------------------------------------------

def test_run_fix_approach_dialogue_augments_request_on_consensus():
    approach = "helpers.pyへのリネームとcli.pyのimport修正を行う。"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": approach}
        elif _CRITIC_MARKER in text:
            yield {"content": "問題なし\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": f"判定: 合意\n\n最終合意内容:\n{approach}"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            augmented_request, aborted = _with_stream(
                fake_stream, yoriai._run_fix_approach_dialogue,
                "fingerprint", "utils.pyをhelpers.pyにリネームして", candidates, "storage.py: ...", ["storage.py"],
                "Python", out_dir,
            )
        assert aborted is False
        assert "utils.pyをhelpers.pyにリネームして" in augmented_request
        assert approach in augmented_request
        assert {"DIALOGUE_LOG_fix.md", "DIALOGUE_SUMMARY_fix.md"} <= set(os.listdir(out_dir))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_fix_approach_dialogue_falls_back_silently_when_no_engagement():
    """バックエンドへの疎通自体ができていない場合は、対話の不調を理由に
    修正を止めず、元の依頼文のまま安全側にフォールバックすることを
    確認する(`_decide_fix_task_split`の既存の安全側フォールバックと
    一貫した挙動)。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        yield {"content": ""}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1")]
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_dialogue_test_")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            augmented_request, aborted = _with_stream(
                fake_stream, yoriai._run_fix_approach_dialogue,
                "fingerprint", "バグを直して", candidates, "storage.py: ...", ["storage.py"], "Python", out_dir,
            )
        assert aborted is False
        assert augmented_request == "バグを直して"
        assert os.listdir(out_dir) == [], "疎通できない場合は議事録ファイルを作らないはずです"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_fix_approach_dialogue_aborts_when_no_consensus_reached():
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "案"}
        elif _CRITIC_MARKER in text:
            yield {"content": "アイデアが尽きました。\n評価: 情報不足"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 人間に確認\n\n人間への確認事項:\n修正範囲を明確にしてください。"}
        yield {"done": True}

    candidates = [_candidate("MacStudio", "m1"), _candidate("junnoMac-mini", "m2")]
    out_dir = tempfile.mkdtemp(prefix="yoriai_fix_dialogue_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            augmented_request, aborted = _with_stream(
                fake_stream, yoriai._run_fix_approach_dialogue,
                "fingerprint", "何となく直して", candidates, "storage.py: ...", ["storage.py"], "Python", out_dir,
            )
        assert aborted is True
        assert "修正範囲を明確にしてください" in buf.getvalue()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 計画のみモード(//plan-only)
# ---------------------------------------------------------------------------

def test_ask_organization_plan_only_never_writes_code_files():
    plan_text = "- 全体方針: まずCLIの骨格を決めてから永続化を追加する"

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": plan_text}
        elif _CRITIC_MARKER in text:
            yield {"content": "問題なし\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": f"判定: 合意\n\n最終合意内容:\n{plan_text}"}
        else:
            raise AssertionError(f"計画のみモードは実装を行わないはずです: {text[:80]}")
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    out_dir = tempfile.mkdtemp(prefix="yoriai_plan_only_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _with_stream(
                fake_stream, yoriai._ask_organization_plan_only,
                47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir,
            )
        output = buf.getvalue()
        assert plan_text in output

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME,
            yoriai._generate_project_name("ToDoリストのCLIツールを作って") + "-plan",
        )
        saved = set(os.listdir(project_dir))
        assert saved == {"DIALOGUE_LOG_plan.md", "DIALOGUE_SUMMARY_plan.md"}, (
            f"計画のみモードは議事録・要約だけを残すはずです: {saved}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        shutil.rmtree(out_dir, ignore_errors=True)


def test_plan_only_command_constant_is_independent_from_agree_and_fix():
    assert yoriai.PLAN_ONLY_COMMAND == "//plan-only"
    assert yoriai.PLAN_ONLY_COMMAND not in (yoriai.AGREE_COMMAND, yoriai.FIX_PROJECT_COMMAND)


# ---------------------------------------------------------------------------
# レビューフェーズでの再利用(自己説明対話)
# ---------------------------------------------------------------------------

_OWNER = {"label": "MacStudio(自分)", "model": "qwen2.5-coder-32b", "address": "127.0.0.1", "port": 47120}
_REVIEWER = {"label": "junnoMac-mini", "model": "qwen2.5-coder-14b", "address": "127.0.0.1", "port": 47120}
_FULL_PLAN = "storage.py: add_todo(text: str) -> int を実装\ncli.py: storage.add_todoを呼び出すCLI"


def test_review_self_explanation_note_reaches_review_prompt_when_enabled():
    captured = {}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if _PROPOSER_MARKER in text:
            yield {"content": "シンプルさを優先し、辞書のリストで保持する設計にしました。"}
        elif _CRITIC_MARKER in text:
            yield {"content": "特に問題なさそうです。\n評価: 合意"}
        elif _INTEGRATOR_MARKER in text:
            yield {"content": "判定: 合意\n\n最終合意内容:\n並行アクセス時の扱いだけ本番レビューで確認してください。"}
        elif "レビュー対象" in text:
            captured["review_prompt"] = text
            yield {"content": "問題なし"}
        else:
            raise AssertionError(f"想定外の問い合わせです: {text[:80]}")
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_dialogue_test_")
    try:
        ok, feedback = _with_stream(
            fake_stream, yoriai._review_and_fix_one_file,
            filename="storage.py", owner=_OWNER, code="def add_todo(text):\n    return 1\n", reviewer=_REVIEWER,
            reviewer_own_filename="cli.py", reviewer_own_code="import storage\n",
            full_plan=_FULL_PLAN, org_fingerprint="fingerprint", out_dir=out_dir,
            enable_self_explanation=True,
        )
        assert ok is True and feedback is None
        assert "並行アクセス時の扱い" in captured.get("review_prompt", ""), (
            "自己説明対話の申し送り事項が本番のレビュープロンプトに含まれていません"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_review_without_self_explanation_flag_sends_unchanged_prompt():
    """既定(`enable_self_explanation`省略)では、既存のレビュー関連テスト群
    (test_review_phase.py等)と全く同じ、自己説明対話を挟まない従来通りの
    プロンプトのままであることを確認する(後方互換性の回帰検知)。
    """
    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" not in text:
            raise AssertionError(f"レビュー本番以外の問い合わせが発生しています: {text[:80]}")
        assert "申し送り事項" not in text
        yield {"content": "問題なし"}
        yield {"done": True}

    out_dir = tempfile.mkdtemp(prefix="yoriai_review_dialogue_test_")
    try:
        ok, feedback = _with_stream(
            fake_stream, yoriai._review_and_fix_one_file,
            filename="storage.py", owner=_OWNER, code="def add_todo(text):\n    return 1\n", reviewer=_REVIEWER,
            reviewer_own_filename="cli.py", reviewer_own_code="import storage\n",
            full_plan=_FULL_PLAN, org_fingerprint="fingerprint", out_dir=out_dir,
        )
        assert ok is True and feedback is None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 自己紹介カードの得意分野情報
# ---------------------------------------------------------------------------

def test_build_profile_card_includes_specialties_field(monkeypatch=None):
    original_ollama_installed = yoriai.get_ollama_installed_models
    original_ollama_loaded = yoriai.get_ollama_loaded_models
    original_lmstudio = yoriai.get_lmstudio_models
    original_mlx = yoriai.get_mlx_lm_models
    yoriai.get_ollama_installed_models = lambda: ["qwen2.5-coder-32b"]
    yoriai.get_ollama_loaded_models = lambda: ["qwen2.5-coder-32b"]
    yoriai.get_lmstudio_models = lambda: []
    yoriai.get_mlx_lm_models = lambda: []
    try:
        card = yoriai.build_profile_card("agent-1")
        assert card["models"]["specialties"] == [yoriai.DIALOGUE_SPECIALTY_CODING]
    finally:
        yoriai.get_ollama_installed_models = original_ollama_installed
        yoriai.get_ollama_loaded_models = original_ollama_loaded
        yoriai.get_lmstudio_models = original_lmstudio
        yoriai.get_mlx_lm_models = original_mlx


def main():
    tests = [
        test_assign_discourse_roles_empty_candidates_returns_empty_dict,
        test_assign_discourse_roles_single_candidate_plays_all_roles,
        test_assign_discourse_roles_two_candidates_second_plays_critic_and_integrator,
        test_assign_discourse_roles_three_or_more_candidates_get_distinct_roles,
        test_parse_critic_verdict_extracts_marker_and_defaults_to_needs_fix,
        test_parse_integrator_verdict_extracts_marker_and_defaults_to_continue,
        test_extract_dialogue_section_falls_back_to_full_text_when_heading_missing,
        test_run_dialogue_requires_min_rounds_even_when_both_agree_immediately,
        test_run_dialogue_critic_flags_information_shortage_escalates_to_human,
        test_run_dialogue_pauses_at_safety_limit_of_fifty_utterances,
        test_run_dialogue_gives_up_after_max_rounds_without_consensus,
        test_run_dialogue_no_engagement_when_proposer_never_answers,
        test_dialogue_log_and_summary_files_are_both_written,
        test_report_dialogue_result_skips_files_when_no_engagement,
        test_ask_organization_collaborate_with_dialogue_reaches_consensus_and_implements,
        test_ask_organization_collaborate_without_dialogue_flag_keeps_single_call_behavior,
        test_ask_organization_collaborate_with_dialogue_aborts_without_human_consensus,
        test_run_fix_approach_dialogue_augments_request_on_consensus,
        test_run_fix_approach_dialogue_falls_back_silently_when_no_engagement,
        test_run_fix_approach_dialogue_aborts_when_no_consensus_reached,
        test_ask_organization_plan_only_never_writes_code_files,
        test_plan_only_command_constant_is_independent_from_agree_and_fix,
        test_review_self_explanation_note_reaches_review_prompt_when_enabled,
        test_review_without_self_explanation_flag_sends_unchanged_prompt,
        test_build_profile_card_includes_specialties_field,
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
