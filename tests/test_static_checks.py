#!/usr/bin/env python3
"""ブラウザ向け成果物(HTML+JS)の完了ゲート Gate 1(静的モジュール整合性
チェック)を検証する。

背景: `chatbot.js`が`export function ...`(ESモジュール構文)で書かれて
いるのに、`index.html`側は`<script src="chatbot.js"></script>`
(非モジュール指定)で読み込んでおり、実機のブラウザでは
`Unexpected token 'export'`という構文エラーになって動作しなかった
不具合が実機で報告された。`static_checks.check_script_module_mismatch`
が、この「非moduleのscriptタグ」と「exportを含むJS」の食い違いを機械的に
検出できることを確認する。

使い方: python3 -m pytest tests/test_static_checks.py
        python3 tests/test_static_checks.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import static_checks  # noqa: E402


def _write(dirpath: str, filename: str, content: str) -> str:
    path = os.path.join(dirpath, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_export_from_non_module_script_tag_is_detected():
    """`export function`を含むJSファイルを、`type="module"`が付いて
    いない`<script src="...">`タグから読み込んでいる場合、その行が
    検出されることを確認する(今回の実機不具合そのもの)。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        _write(
            tmp_dir, "chatbot.js",
            "function greet() {\n  return 'hi';\n}\n\nexport function sendMessage(text) {\n  return greet();\n}\n",
        )
        html_path = _write(
            tmp_dir, "index.html",
            '<html><body><script src="chatbot.js"></script></body></html>',
        )

        issues = static_checks.check_script_module_mismatch(html_path)

        assert len(issues) == 1, issues
        assert issues[0] == (
            "chatbot.js:5: 'export' はモジュール外のscriptタグから読み込むファイルでは使用できません"
        ), issues
    finally:
        shutil.rmtree(tmp_dir)


def test_module_script_tag_is_not_detected():
    """同じ`export function`を含むJSでも、`<script type="module"
    src="...">`のようにmodule指定のタグから読み込んでいる場合は、
    exportが正当なため検出されないことを確認する。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        _write(tmp_dir, "chatbot.js", "export function sendMessage(text) {\n  return text;\n}\n")
        html_path = _write(
            tmp_dir, "index.html",
            '<html><body><script type="module" src="chatbot.js"></script></body></html>',
        )

        issues = static_checks.check_script_module_mismatch(html_path)

        assert issues == [], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_js_without_export_or_import_is_not_detected():
    """`export`/`import`を一切含まない通常のJSファイルを非moduleの
    scriptタグから読み込んでいる、ごく普通のケースでは何も検出されない
    ことを確認する(誤検知しないことの回帰確認)。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        _write(tmp_dir, "chatbot.js", "function sendMessage(text) {\n  console.log(text);\n}\n")
        html_path = _write(
            tmp_dir, "index.html",
            '<html><body><script src="chatbot.js"></script></body></html>',
        )

        issues = static_checks.check_script_module_mismatch(html_path)

        assert issues == [], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_multiple_offending_lines_are_all_reported():
    """1つのJSファイル内に複数のexport/import行がある場合、全て列挙
    されることを確認する(1件だけ検出して打ち切ってしまわないこと)。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        _write(
            tmp_dir, "chatbot.js",
            "import { helper } from './helper.js';\n\nexport function sendMessage(text) {\n  return helper(text);\n}\n",
        )
        html_path = _write(
            tmp_dir, "index.html",
            '<html><body><script src="chatbot.js"></script></body></html>',
        )

        issues = static_checks.check_script_module_mismatch(html_path)

        assert len(issues) == 2, issues
        assert issues[0].startswith("chatbot.js:1: 'import'"), issues
        assert issues[1].startswith("chatbot.js:3: 'export'"), issues
    finally:
        shutil.rmtree(tmp_dir)


def test_external_script_src_is_ignored():
    """CDN等の外部URL(`https://...`)を指す`<script src="...">`は、
    プロジェクト内のファイルではないため検査対象外になることを確認
    する。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        html_path = _write(
            tmp_dir, "index.html",
            '<html><body><script src="https://cdn.example.com/lib.js"></script></body></html>',
        )

        issues = static_checks.check_script_module_mismatch(html_path)

        assert issues == [], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_missing_html_file_returns_empty_list():
    """`html_path`自体が存在しない場合はエラーにせず空リストを返す
    (HTMLの存在確認自体は別チェックの責務のため)。
    """
    issues = static_checks.check_script_module_mismatch("/no/such/file/index.html")
    assert issues == [], issues


def main():
    tests = [
        test_export_from_non_module_script_tag_is_detected,
        test_module_script_tag_is_not_detected,
        test_js_without_export_or_import_is_not_detected,
        test_multiple_offending_lines_are_all_reported,
        test_external_script_src_is_ignored,
        test_missing_html_file_returns_empty_list,
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
