#!/usr/bin/env python3
"""改善依頼への対応を検証する: 「HTMLのプレビューが機能してない」という
修正依頼で、MacStudioが.js/.cssファイルに対してgcc(C言語用)を実行する
など、見当違いのツールを何度も試してツール呼び出し上限に達した不具合
への対応を検証する。

(1)run_testツールのコマンドとファイル拡張子の組み合わせを事前に検証し、
不適切な場合は「代わりに〇〇を使ってください」という具体的な誘導
メッセージを返すこと(_parse_run_test_command)、(2)HTML/CSSの動作検証
手段として追加した`check_html <HTMLファイル名>`が、実機にPlaywright
(ヘッドレスブラウザ操作ツール)があればコンソールエラー・実行時エラーを
検出し、無ければ(gcc/nodeと同じく)エラー扱いにせずスキップすること
(_check_html_with_playwright・_run_project_test_command)を検証する。

実機にPlaywrightが無い環境のため、「無い場合」はそのままの実機の状態で
検証し、「有る場合」は`sys.modules`にplaywright.sync_apiの偽物を差し込んで
検証する(実際のブラウザは起動しない)。

使い方: python3 tests/test_run_test_validation.py
"""
import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


# ---------------------------------------------------------------------------
# playwright.sync_apiの偽物(実際のブラウザを起動せずにロジックを検証する)
# ---------------------------------------------------------------------------

class _FakeConsoleMessage:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class _FakePage:
    def __init__(self, console_messages, page_errors):
        self._console_messages = console_messages
        self._page_errors = page_errors
        self._console_cb = None
        self._pageerror_cb = None

    def on(self, event, callback):
        if event == "console":
            self._console_cb = callback
        elif event == "pageerror":
            self._pageerror_cb = callback

    def goto(self, url, timeout=None):
        for msg in self._console_messages:
            if self._console_cb:
                self._console_cb(msg)
        for err in self._page_errors:
            if self._pageerror_cb:
                self._pageerror_cb(err)

    def wait_for_timeout(self, ms):
        pass


class _FakeBrowser:
    def __init__(self, console_messages, page_errors):
        self._console_messages = console_messages
        self._page_errors = page_errors

    def new_page(self):
        return _FakePage(self._console_messages, self._page_errors)

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, console_messages, page_errors):
        self._console_messages = console_messages
        self._page_errors = page_errors

    def launch(self, timeout=None):
        return _FakeBrowser(self._console_messages, self._page_errors)


class _FakePlaywrightContext:
    def __init__(self, console_messages, page_errors):
        self.chromium = _FakeChromium(console_messages, page_errors)


class _FakeSyncPlaywrightCM:
    def __init__(self, console_messages, page_errors):
        self._console_messages = console_messages
        self._page_errors = page_errors

    def __enter__(self):
        return _FakePlaywrightContext(self._console_messages, self._page_errors)

    def __exit__(self, *args):
        return False


def _install_fake_playwright(console_messages=(), page_errors=()):
    fake_playwright_pkg = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: _FakeSyncPlaywrightCM(list(console_messages), list(page_errors))
    fake_playwright_pkg.sync_api = fake_sync_api
    sys.modules["playwright"] = fake_playwright_pkg
    sys.modules["playwright.sync_api"] = fake_sync_api


def _uninstall_fake_playwright():
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)


# ---------------------------------------------------------------------------
# 依頼の項目1: コマンドとファイル拡張子の組み合わせの事前検証
# ---------------------------------------------------------------------------

def test_parse_run_test_command_rejects_gcc_against_js_file_with_helpful_redirect():
    """実機の再現: 「HTMLのプレビューが機能してない」修正依頼で、gccを
    .jsファイルに対して試みる不適切なツール実行を、実行前に防ぐ。
    """
    argv, error = yoriai._parse_run_test_command("gcc app.js -o app")
    assert argv is None, argv
    assert "gcc" in error and "使用できません" in error, error
    assert "node" in error, error


def test_parse_run_test_command_rejects_gcc_against_css_file():
    argv, error = yoriai._parse_run_test_command("gcc style.css -o style")
    assert argv is None, argv
    assert "check_html" in error, error


def test_parse_run_test_command_rejects_node_against_html_file():
    argv, error = yoriai._parse_run_test_command("node index.html")
    assert argv is None, argv
    assert "check_html" in error, error


def test_parse_run_test_command_rejects_python3_against_js_file():
    argv, error = yoriai._parse_run_test_command("python3 app.js")
    assert argv is None, argv
    assert "node" in error, error


def test_parse_run_test_command_rejects_node_against_py_file():
    argv, error = yoriai._parse_run_test_command("node app.py")
    assert argv is None, argv
    assert "python3" in error, error


def test_parse_run_test_command_still_accepts_matching_extensions():
    assert yoriai._parse_run_test_command("python3 app.py") == (["python3", "app.py"], None)
    assert yoriai._parse_run_test_command("node app.js") == (["node", "app.js"], None)
    assert yoriai._parse_run_test_command("gcc calc.c -o calc") == (["gcc", "calc.c", "-o", "calc"], None)
    assert yoriai._parse_run_test_command("pytest") == (["pytest"], None)
    assert yoriai._parse_run_test_command("pytest test_app.py") == (["pytest", "test_app.py"], None)


def test_parse_run_test_command_accepts_check_html():
    assert yoriai._parse_run_test_command("check_html index.html") == (["check_html", "index.html"], None)


def test_parse_run_test_command_rejects_check_html_against_non_html_file():
    argv, error = yoriai._parse_run_test_command("check_html style.css")
    assert argv is None, argv
    assert "check_html" in error, error


# ---------------------------------------------------------------------------
# 依頼の項目2: check_html(Playwrightによる簡易動作検証)
# ---------------------------------------------------------------------------

def test_check_html_with_playwright_skips_when_not_installed():
    """このテスト環境には実際にPlaywrightが入っていないため、実機の
    「無い場合」の挙動をそのまま検証できる。gcc/nodeと同じく、ツール
    不在はエラー扱いにしない。
    """
    _uninstall_fake_playwright()
    status, detail = yoriai._check_html_with_playwright("/tmp/does-not-matter.html")
    assert status == "skipped", (status, detail)
    assert "Playwright" in detail, detail


def test_check_html_with_playwright_detects_console_error_from_id_mismatch():
    """依頼の動作確認: id名の不一致のようなJS実行時エラー(コンソール
    エラー)が検出できることを確認する。
    """
    _install_fake_playwright(console_messages=[
        _FakeConsoleMessage("error", "Uncaught TypeError: Cannot set properties of null (setting 'textContent')"),
    ])
    try:
        status, detail = yoriai._check_html_with_playwright("/tmp/does-not-matter.html")
    finally:
        _uninstall_fake_playwright()
    assert status == "error", (status, detail)
    assert "Cannot set properties of null" in detail, detail


def test_check_html_with_playwright_detects_uncaught_page_error():
    _install_fake_playwright(page_errors=["ReferenceError: initGame is not defined"])
    try:
        status, detail = yoriai._check_html_with_playwright("/tmp/does-not-matter.html")
    finally:
        _uninstall_fake_playwright()
    assert status == "error", (status, detail)
    assert "initGame" in detail, detail


def test_check_html_with_playwright_ignores_non_error_console_messages():
    _install_fake_playwright(console_messages=[_FakeConsoleMessage("log", "デバッグ出力です")])
    try:
        status, detail = yoriai._check_html_with_playwright("/tmp/does-not-matter.html")
    finally:
        _uninstall_fake_playwright()
    assert status == "ok", (status, detail)


def test_check_html_with_playwright_reports_ok_when_no_errors():
    _install_fake_playwright()
    try:
        status, detail = yoriai._check_html_with_playwright("/tmp/does-not-matter.html")
    finally:
        _uninstall_fake_playwright()
    assert status == "ok", (status, detail)


# ---------------------------------------------------------------------------
# _run_project_test_command経由でのcheck_htmlの結線
# ---------------------------------------------------------------------------

def test_run_project_test_command_check_html_detects_console_error():
    _install_fake_playwright(console_messages=[
        _FakeConsoleMessage("error", "Cannot read properties of null (reading 'addEventListener'): id \"preview-frame\""),
    ])
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_test_html_")
    try:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html><body></body></html>")
        result = json.loads(yoriai._run_project_test_command(out_dir, "check_html index.html"))
        assert result["ok"] is False, result
        assert "preview-frame" in result["output"], result
    finally:
        _uninstall_fake_playwright()
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_check_html_reports_ok_when_no_console_errors():
    _install_fake_playwright()
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_test_html_")
    try:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html><body></body></html>")
        result = json.loads(yoriai._run_project_test_command(out_dir, "check_html index.html"))
        assert result["ok"] is True, result
    finally:
        _uninstall_fake_playwright()
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_check_html_skips_without_playwright_available():
    """実機にPlaywrightが無い場合、エラー扱いにはせずスキップされる
    (gcc/nodeが無い場合と同じ扱い)ことを確認する。
    """
    _uninstall_fake_playwright()
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_test_html_")
    try:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html><body></body></html>")
        result = json.loads(yoriai._run_project_test_command(out_dir, "check_html index.html"))
        assert result["ok"] is False, result
        assert "message" in result and "Playwright" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_check_html_requires_existing_file():
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_test_html_")
    try:
        result = json.loads(yoriai._run_project_test_command(out_dir, "check_html missing.html"))
        assert result["ok"] is False, result
        assert "見つかりません" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 実機の再現シナリオ: 見当違いのツール実行が実行前に弾かれること
# ---------------------------------------------------------------------------

def test_run_project_test_command_rejects_gcc_against_js_before_running_gcc():
    """実機で報告された不具合の再現: 「HTMLのプレビューが機能してない」
    修正依頼で、gccを.jsファイルに対して実行しようとした場合、実際に
    gccを起動する前に(ファイルの存在確認すら行わずに)拒否されることを
    確認する。gccが実機に無い環境でもこのテストは失敗しない
    (バリデーションが構文チェックより前段で完結するため)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_test_html_")
    try:
        with open(os.path.join(out_dir, "app.js"), "w", encoding="utf-8") as f:
            f.write("function f() {}\n")
        result = json.loads(yoriai._run_project_test_command(out_dir, "gcc app.js -o app"))
        assert result["ok"] is False, result
        assert "node" in result["message"], result
        assert not os.path.exists(os.path.join(out_dir, "app")), (
            "拒否されたコマンドがgccを実行してしまっています"
        )
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_parse_run_test_command_rejects_gcc_against_js_file_with_helpful_redirect,
        test_parse_run_test_command_rejects_gcc_against_css_file,
        test_parse_run_test_command_rejects_node_against_html_file,
        test_parse_run_test_command_rejects_python3_against_js_file,
        test_parse_run_test_command_rejects_node_against_py_file,
        test_parse_run_test_command_still_accepts_matching_extensions,
        test_parse_run_test_command_accepts_check_html,
        test_parse_run_test_command_rejects_check_html_against_non_html_file,
        test_check_html_with_playwright_skips_when_not_installed,
        test_check_html_with_playwright_detects_console_error_from_id_mismatch,
        test_check_html_with_playwright_detects_uncaught_page_error,
        test_check_html_with_playwright_ignores_non_error_console_messages,
        test_check_html_with_playwright_reports_ok_when_no_errors,
        test_run_project_test_command_check_html_detects_console_error,
        test_run_project_test_command_check_html_reports_ok_when_no_console_errors,
        test_run_project_test_command_check_html_skips_without_playwright_available,
        test_run_project_test_command_check_html_requires_existing_file,
        test_run_project_test_command_rejects_gcc_against_js_before_running_gcc,
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
