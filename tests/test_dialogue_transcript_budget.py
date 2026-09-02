#!/usr/bin/env python3
"""`yoriai.py`の`_format_dialogue_transcript_for_prompt`(対話プロトコルの
議事録を、後続ラウンドのプロンプトに埋め込む際に圧縮する処理)を検証する。

背景: 実機の対話ログ(DIALOGUE_LOG_design.md)を調査した結果、ラウンド4の
「統合役」の発言のみが文字化け判定(`_looks_garbled`)により打ち切られ、
ログにも記録されていないことが分かった。原因は、以前の
`_format_dialogue_transcript_for_prompt`が「直近2ラウンド分は全文保持」
というラウンド数基準の圧縮ルールだったため。`_decide_max_output_tokens`に
より1ラウンドあたりの発言量(特に提案役・反論役の発言)が大きく増える
余地ができたことで、直近2ラウンド分の全文だけでも実機で20万字前後に
達し、これを丸ごと1回の問い合わせに詰め込む統合役の入力が、他の役の
入力よりもはるかに大きくなっていた。この対応として、「新しい発言から
遡って、合計文字数が予算(`_DIALOGUE_TRANSCRIPT_TOTAL_CHAR_BUDGET`)を
超えるまでは全文を保持し、超えた時点より古い発言は要点のみに圧縮する」
という文字数基準に変更した。

使い方: python3 tests/test_dialogue_transcript_budget.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm_stream  # noqa: E402
import yoriai  # noqa: E402


def _utterance(round_num, role, speaker_label, content):
    return {"round": round_num, "role": role, "speaker_label": speaker_label, "content": content}


def test_format_dialogue_transcript_compresses_by_total_char_budget_not_round_count():
    """1ラウンドあたりの発言が非常に長く、直近1ラウンド分だけで予算を
    超える場合は、直近1ラウンド分しか全文保持されないことを確認する。
    以前のラウンド数基準(直近2ラウンド全文保持)なら2ラウンド分が全文
    保持されるはずだったケースが、文字数基準では1ラウンドしか保持され
    ないことを示す。
    """
    huge = "あ" * (yoriai._DIALOGUE_TRANSCRIPT_TOTAL_CHAR_BUDGET + 1000)
    # 圧縮の有無を目視で確認できるよう、古いラウンドの発言も
    # `_DIALOGUE_TRANSCRIPT_OLDER_ROUND_CHARS`(200字)を超える長さにする
    # (短い発言は「古い」と判定されても、そもそも切り詰める必要が無いため
    # そのまま残る)。
    round1_proposal = "ラウンド1の提案です。" * 30
    round1_critique = "ラウンド1の反論です。" * 30
    round2_proposal = "ラウンド2の提案です。" * 30
    round2_critique = "ラウンド2の反論です。" * 30
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", round1_proposal),
        _utterance(1, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", round1_critique),
        _utterance(2, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", round2_proposal),
        _utterance(2, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", round2_critique),
        _utterance(3, yoriai.DIALOGUE_ROLE_INTEGRATOR, "MacStudio", huge),
    ]

    formatted = yoriai._format_dialogue_transcript_for_prompt(transcript)

    assert huge in formatted, "直近ラウンド(予算超過の発言自体)は全文保持されるはず"
    assert round1_proposal not in formatted
    assert round1_critique not in formatted
    assert round2_proposal not in formatted
    assert round2_critique not in formatted
    assert formatted.count("...(以下省略。古いラウンドのため要点のみ表示)") == 4


def test_format_dialogue_transcript_keeps_many_short_rounds_full_within_budget():
    """1ラウンドあたりの発言が短ければ、3ラウンド以上前の発言でも予算内
    であれば全文保持されることを確認する(ラウンド数基準より多くの履歴
    を保持できるケースがあることを示す)。
    """
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", "ラウンド1の提案です"),
        _utterance(1, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "ラウンド1の反論です"),
        _utterance(2, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", "ラウンド2の提案です"),
        _utterance(2, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "ラウンド2の反論です"),
        _utterance(3, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", "ラウンド3の提案です"),
        _utterance(3, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "ラウンド3の反論です"),
        _utterance(4, yoriai.DIALOGUE_ROLE_INTEGRATOR, "MacStudio", "ラウンド4の統合です"),
    ]

    formatted = yoriai._format_dialogue_transcript_for_prompt(transcript)

    for utterance in transcript:
        assert utterance["content"] in formatted, utterance["content"]
    assert "...(以下省略。古いラウンドのため要点のみ表示)" not in formatted


def test_format_dialogue_transcript_always_keeps_at_least_latest_utterance_full():
    """最新の発言1件だけで予算を超える場合でも、その発言は全文保持される
    ことを確認する(全て圧縮されて空にならないようにする)。
    """
    huge = "あ" * (yoriai._DIALOGUE_TRANSCRIPT_TOTAL_CHAR_BUDGET * 2)
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_INTEGRATOR, "MacStudio", huge),
    ]

    formatted = yoriai._format_dialogue_transcript_for_prompt(transcript)

    assert huge in formatted
    assert "...(以下省略。古いラウンドのため要点のみ表示)" not in formatted


def test_format_dialogue_transcript_preserves_chronological_order():
    """圧縮の内部走査は新しい方から遡るが、出力する行の並び順は既存通り
    古い順のままであることを確認する。
    """
    transcript = [
        _utterance(1, yoriai.DIALOGUE_ROLE_PROPOSER, "MacStudio", "最初の発言"),
        _utterance(2, yoriai.DIALOGUE_ROLE_CRITIC, "junnoMac-mini", "次の発言"),
        _utterance(3, yoriai.DIALOGUE_ROLE_INTEGRATOR, "MacStudio", "最後の発言"),
    ]

    formatted = yoriai._format_dialogue_transcript_for_prompt(transcript)

    assert formatted.index("最初の発言") < formatted.index("次の発言") < formatted.index("最後の発言")


def main():
    tests = [
        test_format_dialogue_transcript_compresses_by_total_char_budget_not_round_count,
        test_format_dialogue_transcript_keeps_many_short_rounds_full_within_budget,
        test_format_dialogue_transcript_always_keeps_at_least_latest_utterance_full,
        test_format_dialogue_transcript_preserves_chronological_order,
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
