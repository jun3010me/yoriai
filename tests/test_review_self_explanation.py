#!/usr/bin/env python3
"""`_run_review_self_explanation`(レビュー本番前の軽量な自己説明対話)の
申し送り事項消失バグへの対応を検証する。

実機の実行ログで、反論役が2ラウンドにわたり具体的な懸念点を複数指摘し、
統合役も「判定: 継続」(合意に至らず)と判断したにもかかわらず、
`_run_review_self_explanation`が`_run_dialogue`の戻り値から
`final_content`(合意時のみ入る)と`summary`(合意に至らなかった場合は
反論役の発言内容とは無関係な定型文になる)しか見ておらず、反論役の
具体的な指摘内容が本番レビューへ一切引き継がれずに消えてしまう不具合が
確認された。反論役が仕事をすればするほど(=合意しにくくなるほど)
情報が失われるという設計上のミスマッチだったため、合意に至らなかった
場合は反論役の直近の発言(`_latest_critic_utterance`)を優先して使うよう
`_run_review_self_explanation`側にのみ修正を加えた。`_run_dialogue`
本体・`_summarize_dialogue`の挙動(他の4箇所の呼び出し元に共通)には
一切手を入れていない。

使い方: python3 tests/test_review_self_explanation.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


_OWNER = {"label": "MacStudio(自分)", "model": "qwen2.5-coder-32b", "address": "127.0.0.1", "port": 47120}
_REVIEWER = {"label": "junnoMac-mini", "model": "qwen2.5-coder-14b", "address": "127.0.0.1", "port": 47120}
_FULL_PLAN = "storage.py: add_todo(text: str) -> int を実装\ncli.py: storage.add_todoを呼び出すCLI"
_CODE = "def add_todo(text):\n    return 1\n"


def _utterance(round_num, role, speaker_label, content):
    return {"round": round_num, "role": role, "speaker_label": speaker_label, "content": content}


def _with_fake_run_dialogue(fake_result, fn, *args, **kwargs):
    original = yoriai._run_dialogue
    yoriai._run_dialogue = lambda *_a, **_kw: fake_result
    try:
        return fn(*args, **kwargs)
    finally:
        yoriai._run_dialogue = original


# ---------------------------------------------------------------------------
# _latest_critic_utterance単体
# ---------------------------------------------------------------------------

def test_latest_critic_utterance_returns_most_recent_round_critic_content():
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", "Moment.jsを使う設計にしました。"),
        _utterance(1, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "Moment.jsは非推奨なので代替を検討すべきです。"),
        _utterance(1, yoriai.DIALOGUE_ROLE_INTEGRATOR, "junnoMac-mini", "判定: 継続"),
        _utterance(2, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", "Prism.jsを同期ロードする設計にしました。"),
        _utterance(2, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "Prism.jsは非同期ロードにすべきで、CSPの懸念もあります。"),
        _utterance(2, yoriai.DIALOGUE_ROLE_INTEGRATOR, "junnoMac-mini", "判定: 継続"),
    ]
    result = yoriai._latest_critic_utterance(transcript)
    assert result == "Prism.jsは非同期ロードにすべきで、CSPの懸念もあります。"


def test_latest_critic_utterance_returns_empty_string_when_no_critic_utterances():
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "raspi4", "設計方針を説明しました。"),
        _utterance(1, yoriai.DIALOGUE_ROLE_INTEGRATOR, "raspi4", "判定: 合意\n\n最終合意内容:\n特になし"),
    ]
    assert yoriai._latest_critic_utterance(transcript) == ""


def test_latest_critic_utterance_returns_empty_string_for_empty_transcript():
    assert yoriai._latest_critic_utterance([]) == ""


# ---------------------------------------------------------------------------
# _run_review_self_explanation: 合意時は既存動作のまま(非破壊確認)
# ---------------------------------------------------------------------------

def test_run_review_self_explanation_uses_final_content_on_consensus():
    fake_result = {
        "status": yoriai.DIALOGUE_STATUS_CONSENSUS,
        "transcript": [
            _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", "辞書のリストで保持する設計です。"),
            _utterance(1, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "特に問題なさそうです。"),
        ],
        "final_content": "並行アクセス時の扱いだけ本番レビューで確認してください。",
        "human_message": None,
        "total_utterances": 2,
        "summary": "2件の発言を経て合意に達しました。\n\n並行アクセス時の扱いだけ本番レビューで確認してください。",
    }
    note = _with_fake_run_dialogue(
        fake_result, yoriai._run_review_self_explanation,
        "storage.py", _OWNER, _REVIEWER, _CODE, _FULL_PLAN, "fingerprint",
    )
    assert note == "並行アクセス時の扱いだけ本番レビューで確認してください。"


# ---------------------------------------------------------------------------
# _run_review_self_explanation: 合意に至らなかった場合、反論役の直近の
# 指摘内容を優先する(申し送り事項消失バグの再現テスト)
# ---------------------------------------------------------------------------

def test_run_review_self_explanation_falls_back_to_latest_critic_utterance_when_needs_human():
    critic_concerns = (
        "Moment.jsの代替、Prism.jsの非同期ロード、アクセシビリティ、ファイル名競合対策、"
        "SEO/構造化データ、CSPについて懸念があります。"
    )
    fake_result = {
        "status": yoriai.DIALOGUE_STATUS_NEEDS_HUMAN,
        "transcript": [
            _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", "設計方針の説明その1"),
            _utterance(1, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "初回の懸念点です。"),
            _utterance(1, yoriai.DIALOGUE_ROLE_INTEGRATOR, "junnoMac-mini", "判定: 継続"),
            _utterance(2, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", "設計方針の説明その2"),
            _utterance(2, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", critic_concerns),
            _utterance(2, yoriai.DIALOGUE_ROLE_INTEGRATOR, "junnoMac-mini", "判定: 継続"),
        ],
        "final_content": None,
        "human_message": "議論を重ねましたが、まだアイデアが不足しており合意に至りませんでした。アドバイスをください。",
        "total_utterances": 6,
        "summary": "議論を重ねましたが、まだアイデアが不足しており合意に至りませんでした。アドバイスをください。",
    }
    note = _with_fake_run_dialogue(
        fake_result, yoriai._run_review_self_explanation,
        "workflow.html", _OWNER, _REVIEWER, _CODE, _FULL_PLAN, "fingerprint",
    )
    assert note == critic_concerns
    assert "議論を重ねましたが、まだアイデアが不足しており" not in note


# ---------------------------------------------------------------------------
# _run_review_self_explanation: 反論役の発言が1件も無い場合はsummaryへ
# フォールバックする
# ---------------------------------------------------------------------------

def test_run_review_self_explanation_falls_back_to_summary_when_no_critic_utterance_exists():
    fake_result = {
        "status": yoriai.DIALOGUE_STATUS_NEEDS_HUMAN,
        "transcript": [
            _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "raspi4", "設計方針の説明"),
            _utterance(1, yoriai.DIALOGUE_ROLE_INTEGRATOR, "raspi4", "判定: 人間に確認"),
        ],
        "final_content": None,
        "human_message": "合意に至りませんでした。",
        "total_utterances": 2,
        "summary": "合意に至りませんでした。",
    }
    note = _with_fake_run_dialogue(
        fake_result, yoriai._run_review_self_explanation,
        "storage.py", _OWNER, _REVIEWER, _CODE, _FULL_PLAN, "fingerprint",
    )
    assert note == "合意に至りませんでした。"


# ---------------------------------------------------------------------------
# _run_review_self_explanation: 応答なし(DIALOGUE_STATUS_NO_ENGAGEMENT)は
# 従来通り空文字列
# ---------------------------------------------------------------------------

def test_run_review_self_explanation_returns_empty_string_on_no_engagement():
    fake_result = {
        "status": yoriai.DIALOGUE_STATUS_NO_ENGAGEMENT,
        "transcript": [
            _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio(自分)", ""),
        ],
        "final_content": None,
        "human_message": None,
        "total_utterances": 1,
        "summary": "議論への応答が得られませんでした。",
    }
    note = _with_fake_run_dialogue(
        fake_result, yoriai._run_review_self_explanation,
        "storage.py", _OWNER, _REVIEWER, _CODE, _FULL_PLAN, "fingerprint",
    )
    assert note == ""


def main():
    tests = [
        test_latest_critic_utterance_returns_most_recent_round_critic_content,
        test_latest_critic_utterance_returns_empty_string_when_no_critic_utterances,
        test_latest_critic_utterance_returns_empty_string_for_empty_transcript,
        test_run_review_self_explanation_uses_final_content_on_consensus,
        test_run_review_self_explanation_falls_back_to_latest_critic_utterance_when_needs_human,
        test_run_review_self_explanation_falls_back_to_summary_when_no_critic_utterance_exists,
        test_run_review_self_explanation_returns_empty_string_on_no_engagement,
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
