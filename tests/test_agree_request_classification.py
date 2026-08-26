#!/usr/bin/env python3
"""`//agree`への依頼の種類判定(コンテンツ生成/ソフトウェア実装)を検証する。

背景: `//agree`は依頼の種類に関わらず常にソフトウェア実装用のタスク分解
(`_MODULE_BREAKDOWN_PROMPT_TEMPLATE`)を適用していたため、「Webページを
作って」のようなコンテンツ生成系の依頼でも、実質的な調査内容を伴わない
骨組みだけの成果物が生成される問題があった。この段階的な改修の第一段として、
`_classify_agree_request_type`が依頼文をキーワードベースで
`AGREE_REQUEST_TYPE_CONTENT`/`AGREE_REQUEST_TYPE_SOFTWARE`に正しく
分類できることを検証する。

使い方: python3 tests/test_agree_request_classification.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_content_keywords_are_classified_as_content():
    """「Webページ」「まとめ」「記事」「ブログ」「解説」「レポート」等の
    コンテンツ生成キーワードのみを含む依頼はCONTENTに分類される。
    """
    requests = [
        "ObsidianでPKMを構築するための知識をまとめたWebページを作って",
        "最近読んだ本の感想をまとめて記事にして",
        "旅行の思い出をブログにまとめて",
        "量子コンピュータについて解説してほしい",
        "先月の売上についてレポートを書いて",
    ]
    for request in requests:
        assert yoriai._classify_agree_request_type(request) == yoriai.AGREE_REQUEST_TYPE_CONTENT, request


def test_software_keywords_are_classified_as_software():
    """「アプリ」「ツール」「CLI」「スクリプト」「API」等のソフトウェア
    実装キーワードを含む依頼はSOFTWAREに分類される。
    """
    requests = [
        "ToDoリストのCLIツールを作って",
        "天気を取得するAPIを作って",
        "ファイル整理用のスクリプトを書いて",
        "簡単な電卓アプリを作って",
    ]
    for request in requests:
        assert yoriai._classify_agree_request_type(request) == yoriai.AGREE_REQUEST_TYPE_SOFTWARE, request


def test_ambiguous_or_unmatched_requests_default_to_software():
    """両方のキーワードに該当する・どちらにも該当しない曖昧な依頼は、
    既存のソフトウェア実装フローを壊さないよう、後方互換性を優先して
    SOFTWAREをデフォルトとする。
    """
    # コンテンツ・ソフトウェア双方のキーワードを含む(曖昧)
    both = "ツールの使い方をまとめた解説記事を作って"
    assert yoriai._classify_agree_request_type(both) == yoriai.AGREE_REQUEST_TYPE_SOFTWARE, both

    # どちらのキーワードにも該当しない
    neither = "富士山の標高について教えて"
    assert yoriai._classify_agree_request_type(neither) == yoriai.AGREE_REQUEST_TYPE_SOFTWARE, neither


def test_explicit_research_instruction_overrides_keyword_classification():
    """依頼文中に「Web検索して」「調査して」という明示的な指示があれば、
    キーワード分類の結果に関わらずCONTENTを優先する(ユーザーの明示的な
    意図を最優先する)。
    """
    # ソフトウェア実装キーワード(ツール)を含むが、明示的な調査指示がある
    request_with_tool_keyword = "最新のCLIツールについてWeb検索して、比較したレポートを作って"
    assert yoriai._classify_agree_request_type(request_with_tool_keyword) == yoriai.AGREE_REQUEST_TYPE_CONTENT

    # コンテンツ/ソフトウェアどちらのキーワードにも該当しないが、
    # 「調査して」という明示的な指示がある
    request_with_no_keyword = "生成AIの最新動向について調査して教えて"
    assert yoriai._classify_agree_request_type(request_with_no_keyword) == yoriai.AGREE_REQUEST_TYPE_CONTENT


# ---------------------------------------------------------------------------
# ステップ3: コンテンツ生成用タスク分解テンプレートの新設
# ---------------------------------------------------------------------------
#
# `_build_module_breakdown_prompt`・`_build_design_dialogue_output_
# instruction`が、ステップ1の分類結果(`request_type`)に応じて
# `_CONTENT_BREAKDOWN_PROMPT_TEMPLATE`(コンテンツ制作用)/
# `_MODULE_BREAKDOWN_PROMPT_TEMPLATE`(ソフトウェア実装用、既存)を
# 正しく使い分け、コンテンツ系の場合はステップ2のリサーチフェーズの結果
# (`research_notes`)がプロンプトに実際に埋め込まれることを検証する。

def test_build_module_breakdown_prompt_uses_content_template_and_embeds_research_notes():
    """`request_type`が`AGREE_REQUEST_TYPE_CONTENT`の場合、コンテンツ制作用の
    役割説明(ソフトウェア設計ではないことの明示)と、渡した`research_notes`
    の内容がそのままプロンプトに埋め込まれることを確認する。
    """
    research_notes = "Obsidianはバックリンクでノート同士を関連付けられる(調査結果の具体例)"
    prompt = yoriai._build_module_breakdown_prompt(
        "ObsidianでPKMを構築するための知識をまとめたWebページを作って",
        yoriai.AGREE_REQUEST_TYPE_CONTENT,
        research_notes,
    )
    assert "コンテンツ制作の構成案を担当します" in prompt, prompt
    assert research_notes in prompt, prompt
    # ソフトウェア設計用テンプレートの「関数シグネチャ」要求は出てこない。
    assert "関数のシグネチャ" not in prompt, prompt


def test_build_module_breakdown_prompt_still_uses_software_template_for_software_requests():
    """`request_type`が`AGREE_REQUEST_TYPE_SOFTWARE`の場合は、既存の
    ソフトウェア実装用テンプレートがそのまま使われ、コンテンツ用の文言は
    出てこないことを確認する(既存のソフトウェア実装系`//agree`の挙動が
    変わっていないことの回帰確認)。
    """
    prompt = yoriai._build_module_breakdown_prompt(
        "ToDoリストのCLIツールを作って", yoriai.AGREE_REQUEST_TYPE_SOFTWARE,
    )
    assert "関数のシグネチャ" in prompt, prompt
    assert "コンテンツ制作の構成案を担当します" not in prompt, prompt


def test_build_design_dialogue_output_instruction_embeds_research_notes_for_content():
    """対話プロトコル(計画フェーズ)側の出力指示も、コンテンツ系の場合は
    `research_notes`を埋め込み、ソフトウェア用の「関数シグネチャ」要求を
    出さないことを確認する。
    """
    research_notes = "生成AIの最新動向: 具体的な調査結果のダミーテキスト"
    instruction = yoriai._build_design_dialogue_output_instruction(
        "生成AIの最新動向について調査してまとめて", yoriai.AGREE_REQUEST_TYPE_CONTENT, research_notes,
    )
    assert research_notes in instruction, instruction
    assert "関数のシグネチャ" not in instruction, instruction


def test_build_design_dialogue_output_instruction_defaults_to_software_behavior():
    """`request_type`を省略した場合は既定でソフトウェア実装用の指示になる
    (既存呼び出し元との後方互換性)ことを確認する。
    """
    instruction = yoriai._build_design_dialogue_output_instruction("ToDoリストのCLIツールを作って")
    assert "関数のシグネチャ" in instruction, instruction


def main():
    tests = [
        test_content_keywords_are_classified_as_content,
        test_software_keywords_are_classified_as_software,
        test_ambiguous_or_unmatched_requests_default_to_software,
        test_explicit_research_instruction_overrides_keyword_classification,
        test_build_module_breakdown_prompt_uses_content_template_and_embeds_research_notes,
        test_build_module_breakdown_prompt_still_uses_software_template_for_software_requests,
        test_build_design_dialogue_output_instruction_embeds_research_notes_for_content,
        test_build_design_dialogue_output_instruction_defaults_to_software_behavior,
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
