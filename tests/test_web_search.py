#!/usr/bin/env python3
"""ウェブ検索バックエンドをDuckDuckGoのHTMLスクレイピングからSearXNGのJSON APIに
置き換えたことを検証するテスト。

`web_search()`の戻り値の形式・関数シグネチャは変えていないため
(`[{"title":..., "url":..., "snippet":...}, ...]`)、呼び出し元
(`_execute_tool_call`)は変更していない。ここでは(1)SearXNGへの問い合わせが
期待通りのURL/パラメータで行われること、(2)SearXNGのJSONレスポンス
(`results`の各要素の`title`/`url`/`content`)から期待通りの形式に変換される
こと(`content`→`snippet`)、(3)max_resultsで件数が絞られること、
(4)タイトルが空の結果は除外されること、(5)接続失敗・不正なレスポンス時は
例外を投げずに空リストを返すこと、を確認する。

実機のSearXNGインスタンスに接続できない環境のため、`requests.get`を
差し替えて模擬する。

使い方: python3 tests/test_web_search.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200, json_error=None):
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} Client Error")

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


def _patch_requests_get(fake_get):
    original = yoriai.requests.get
    yoriai.requests.get = fake_get
    return original


def test_web_search_queries_searxng_json_api_with_expected_params():
    """SearXNGへの問い合わせが、設定されたベースURLの/searchエンドポイントに
    対し、検索語とformat=jsonをパラメータとして渡していることを確認する。
    """
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse({"query": "obsidian markdown", "results": []})

    original = _patch_requests_get(fake_get)
    try:
        yoriai.web_search("obsidian markdown")
    finally:
        yoriai.requests.get = original

    assert captured["url"] == f"{yoriai.SEARXNG_BASE_URL}/search", captured["url"]
    assert captured["params"] == {"q": "obsidian markdown", "format": "json"}, captured["params"]
    assert captured["timeout"] == (yoriai.CHAT_CONNECT_TIMEOUT_SEC, yoriai.WEB_SEARCH_TIMEOUT_SEC), captured["timeout"]


def test_web_search_maps_content_field_to_snippet():
    """SearXNGのJSONレスポンスの各resultsの`content`が`snippet`に
    マッピングされ、`title`/`url`はそのまま使われることを確認する。
    """
    fake_response = _FakeResponse({
        "query": "obsidian markdown",
        "results": [
            {"title": "Obsidian公式", "url": "https://obsidian.md/", "content": "Obsidianの公式サイト", "engine": "google"},
            {"title": "Markdown記法", "url": "https://example.com/md", "content": "Markdownの書き方", "engine": "bing"},
        ],
        "answers": [], "corrections": [], "infoboxes": [], "suggestions": [],
        "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
    })

    original = _patch_requests_get(lambda url, params=None, timeout=None: fake_response)
    try:
        results = yoriai.web_search("obsidian markdown")
    finally:
        yoriai.requests.get = original

    assert results == [
        {"title": "Obsidian公式", "url": "https://obsidian.md/", "snippet": "Obsidianの公式サイト"},
        {"title": "Markdown記法", "url": "https://example.com/md", "snippet": "Markdownの書き方"},
    ], results


def test_web_search_respects_max_results():
    fake_response = _FakeResponse({
        "results": [
            {"title": f"結果{i}", "url": f"https://example.com/{i}", "content": f"説明{i}"}
            for i in range(10)
        ],
    })

    original = _patch_requests_get(lambda url, params=None, timeout=None: fake_response)
    try:
        results = yoriai.web_search("test", max_results=3)
    finally:
        yoriai.requests.get = original

    assert len(results) == 3, results


def test_web_search_skips_results_without_title():
    fake_response = _FakeResponse({
        "results": [
            {"title": "", "url": "https://example.com/empty", "content": "タイトルなし"},
            {"title": "有効な結果", "url": "https://example.com/ok", "content": "説明"},
        ],
    })

    original = _patch_requests_get(lambda url, params=None, timeout=None: fake_response)
    try:
        results = yoriai.web_search("test")
    finally:
        yoriai.requests.get = original

    assert results == [{"title": "有効な結果", "url": "https://example.com/ok", "snippet": "説明"}], results


def test_web_search_returns_empty_list_on_connection_failure():
    """SearXNGインスタンスに接続できない場合(未起動・タイムアウト等)、
    例外を投げずに空リストを返すことを確認する。
    """
    def fake_get(url, params=None, timeout=None):
        raise Exception("Connection refused")

    original = _patch_requests_get(fake_get)
    try:
        results = yoriai.web_search("test")
    finally:
        yoriai.requests.get = original

    assert results == [], results


def test_web_search_returns_empty_list_on_http_error_status():
    original = _patch_requests_get(lambda url, params=None, timeout=None: _FakeResponse(status_code=500))
    try:
        results = yoriai.web_search("test")
    finally:
        yoriai.requests.get = original

    assert results == [], results


def test_web_search_returns_empty_list_on_invalid_json():
    fake_response = _FakeResponse(json_error=ValueError("Expecting value: line 1 column 1"))
    original = _patch_requests_get(lambda url, params=None, timeout=None: fake_response)
    try:
        results = yoriai.web_search("test")
    finally:
        yoriai.requests.get = original

    assert results == [], results


def test_execute_tool_call_passes_through_web_search_results_unchanged():
    """呼び出し元(_execute_tool_call)は変更していないため、web_searchの
    戻り値の形式さえ変わらなければ従来通りJSON化されて返ることを確認する。
    """
    import json as _json

    fake_response = _FakeResponse({
        "results": [{"title": "結果", "url": "https://example.com", "content": "説明"}],
    })
    original = _patch_requests_get(lambda url, params=None, timeout=None: fake_response)
    try:
        result_str = yoriai._execute_tool_call({
            "function": {"name": "web_search", "arguments": {"query": "test"}}
        })
    finally:
        yoriai.requests.get = original

    parsed = _json.loads(result_str)
    assert parsed == {
        "query": "test",
        "results": [{"title": "結果", "url": "https://example.com", "snippet": "説明"}],
    }, parsed


def main():
    tests = [
        test_web_search_queries_searxng_json_api_with_expected_params,
        test_web_search_maps_content_field_to_snippet,
        test_web_search_respects_max_results,
        test_web_search_skips_results_without_title,
        test_web_search_returns_empty_list_on_connection_failure,
        test_web_search_returns_empty_list_on_http_error_status,
        test_web_search_returns_empty_list_on_invalid_json,
        test_execute_tool_call_passes_through_web_search_results_unchanged,
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
