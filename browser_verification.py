"""Yoriaiの完了ゲート層: `browser_frontend`に分類されたタスクの成果物
(HTML+JS)を、実際にヘッドレスブラウザで読み込んで検証するGate 2。

背景: `static_checks.check_script_module_mismatch`(Gate 1)は静的な
構文パターンの不一致だけを検出する。それだけでは検出できない実行時
エラー(idの不一致による`getElementById`のnull化等、構文としては正しく
実行してみて初めて表面化する不具合)を、実際にChromiumでファイルを
開いて拾うのがこのGate 2の役割。

依頼者(ジュンさん)が実際に行う「HTMLファイルをダブルクリックして直接
開く」という使い方をそのまま再現するため、`page.goto("file://<絶対
パス>")`でファイルを直接開く(ローカルサーバー経由ではない)。

`tools._check_html_with_playwright`は、モデルが`check_html`ツールを
自発的に呼び出した場合にのみ実行される任意の検証であるのに対し、
こちらは`browser_frontend`に分類されたタスクの完了ゲートとして
オーケストレーション層(`yoriai.py`)から強制的に呼び出される点が異なる
(モデルが検証をサボっても素通りしない)。
"""
import os

_PLAYWRIGHT_LOAD_TIMEOUT_MS = 15000
# ページ読み込み完了後の待機時間。非同期の初期化処理(fetch・setTimeout
# 等)がある場合に備え、`tools._check_html_with_playwright`の500msより
# 長めに取る。
_POST_LOAD_WAIT_MS = 3000


class BrowserVerificationUnavailable(RuntimeError):
    """Playwright(ヘッドレスブラウザ操作ツール)が実機に無い、または
    ブラウザの起動・読み込み自体に失敗したため、Gate 2自体を実行できな
    かったことを示す。`gcc`/`node`が実機に無い場合の既存の扱いと同様、
    ツール不在は成果物側の不具合(検出エラー)とは区別する必要がある
    ため、`verify_browser_load`の戻り値(`list[str]`、検出されたエラーの
    一覧)とは別の例外として送出する。呼び出し元はこれを捕捉し、Gate 2を
    スキップした旨を表示する(未検証のまま無条件に成功扱いにはしない)。
    """


def verify_browser_load(html_path: str) -> list:
    """`html_path`を`page.goto(f"file://{絶対パス}")`でヘッドレス
    Chromiumで開き、`console.error`メッセージと未捕捉の例外
    (`pageerror`)を集めて文字列のリストとして返す。問題が無ければ
    空リストを返す。

    Playwrightが実機に無い、またはブラウザの起動・読み込み自体に
    失敗した場合は`BrowserVerificationUnavailable`を送出する(検出
    エラー0件と「そもそも検証できなかった」は区別する必要があるため)。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserVerificationUnavailable(
            "Playwright(ヘッドレスブラウザ操作ツール)が見つからないため、ブラウザ動作検証を実行できません。"
            "'pip install playwright' の後 'playwright install chromium' を実行してください。"
        ) from exc

    if not os.path.isfile(html_path):
        raise BrowserVerificationUnavailable(f"'{html_path}' が見つからないため、ブラウザ動作検証を実行できません。")

    absolute_path = os.path.abspath(html_path)
    console_errors = []
    page_errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(timeout=_PLAYWRIGHT_LOAD_TIMEOUT_MS)
            try:
                page = browser.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.goto(f"file://{absolute_path}", timeout=_PLAYWRIGHT_LOAD_TIMEOUT_MS)
                page.wait_for_timeout(_POST_LOAD_WAIT_MS)
            finally:
                browser.close()
    except Exception as exc:
        raise BrowserVerificationUnavailable(
            f"ヘッドレスブラウザでの検証実行に失敗したため、ブラウザ動作検証をスキップしました: {exc}"
        ) from exc

    filename = os.path.basename(absolute_path)
    return [f"{filename}: {message}" for message in console_errors + page_errors]
