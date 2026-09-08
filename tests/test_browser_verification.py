#!/usr/bin/env python3
"""ブラウザ向け成果物(HTML+JS)の完了ゲート Gate 2(実ブラウザ読み込み
確認)を検証する。

背景: Gate 1(`static_checks.check_script_module_mismatch`)は静的な
構文パターンの不一致だけを検出できる。idの不一致による
`getElementById`のnull化のような、構文としては正しく実行してみて
初めて表面化する実行時エラーは、実際にヘッドレスブラウザでファイルを
開いてコンソール・例外を拾う`browser_verification.verify_browser_load`
でしか検出できない。依頼者(ジュンさん)が実際に行う「HTMLファイルを
ダブルクリックして直接開く」使い方をそのまま再現するため、
`file://`で直接開いて検証する。

実機にPlaywright(・対応するブラウザ本体)が無い環境でも、本体テストは
理由を明示したうえでスキップし、失敗はさせない
(`tests/test_run_command.py`の既存の扱いと同じ方針)。

使い方: python3 -m pytest tests/test_browser_verification.py
        python3 tests/test_browser_verification.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import browser_verification  # noqa: E402


def _write(dirpath: str, filename: str, content: str) -> str:
    path = os.path.join(dirpath, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def test_console_error_is_detected():
    """意図的に`console.error`を出すHTML/JSを実際にヘッドレスブラウザで
    開き、そのメッセージが検出されることを確認する。

    実機にPlaywright(・対応するブラウザ本体)が無い環境では、本体は
    スキップし失敗はさせない(`tests/test_run_command.py`のgcc/node同様)。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        html_path = _write(
            tmp_dir, "index.html",
            "<html><body><script>console.error('boom: something went wrong');</script></body></html>",
        )
        try:
            issues = browser_verification.verify_browser_load(html_path)
        except browser_verification.BrowserVerificationUnavailable as exc:
            print(f"  ({exc} のため、このテストの本体はスキップします)")
            return

        assert len(issues) == 1, issues
        assert "boom: something went wrong" in issues[0], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_uncaught_exception_is_detected():
    """意図的に未定義関数を呼んで未捕捉の例外(pageerror)を出す
    HTML/JSを実際にヘッドレスブラウザで開き、そのエラーが検出される
    ことを確認する。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        html_path = _write(
            tmp_dir, "index.html",
            "<html><body><script>thisFunctionDoesNotExist();</script></body></html>",
        )
        try:
            issues = browser_verification.verify_browser_load(html_path)
        except browser_verification.BrowserVerificationUnavailable as exc:
            print(f"  ({exc} のため、このテストの本体はスキップします)")
            return

        assert len(issues) == 1, issues
        assert "thisFunctionDoesNotExist" in issues[0], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_normally_working_page_reports_no_issues():
    """コンソールエラー・未捕捉例外のいずれも起きない正常なHTML/JSを
    開いた場合は、空リストが返る(誤検知しない)ことを確認する。
    """
    tmp_dir = tempfile.mkdtemp()
    try:
        html_path = _write(
            tmp_dir, "index.html",
            "<html><body><script>\n"
            "function greet(name) { return 'こんにちは、' + name; }\n"
            "document.title = greet('ジュン');\n"
            "</script></body></html>",
        )
        try:
            issues = browser_verification.verify_browser_load(html_path)
        except browser_verification.BrowserVerificationUnavailable as exc:
            print(f"  ({exc} のため、このテストの本体はスキップします)")
            return

        assert issues == [], issues
    finally:
        shutil.rmtree(tmp_dir)


def test_missing_file_raises_unavailable():
    """存在しないHTMLファイルを指定した場合は、検出エラー0件(問題無し)
    と混同しないよう`BrowserVerificationUnavailable`が送出されることを
    確認する。
    """
    try:
        browser_verification.verify_browser_load("/no/such/file/index.html")
    except browser_verification.BrowserVerificationUnavailable:
        pass
    else:
        raise AssertionError("BrowserVerificationUnavailableが送出されませんでした")


def main():
    tests = [
        test_console_error_is_detected,
        test_uncaught_exception_is_detected,
        test_normally_working_page_reports_no_issues,
        test_missing_file_raises_unavailable,
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
