"""Yoriaiの静的チェック層: ブラウザ向け成果物(HTML+JS)の整合性を、
実際にブラウザを起動せず素早く検出するための軽量チェック集。

背景: `chatbot.js`が`export function ...`(ESモジュール構文)で書かれて
いるのに、`index.html`側は`<script src="chatbot.js"></script>`
(非モジュール指定)で読み込んでおり、実機のブラウザでは
`Unexpected token 'export'`という構文エラーになって動作しなかった
不具合が実機で報告された。自動検証(`node test_verify.js`)は成功して
いたが、これはNode.js(v22系)がESモジュール構文を自動検出して寛容に
解釈してしまうためで、「ロジックが正しいか」は検証できても「実際の
ブラウザで(非モジュールとして)読み込めるか」は一度も検証されていな
かった。ここでは、HTML側の非module指定`<script src="...">`タグが
読み込むJSファイルの中に`export`/`import`文が含まれていないかを、
ブラウザを起動せず静的に(正規表現ベースで)検出する。ヘッドレス
ブラウザでの実際の読み込み確認(Gate 2)は`browser_verification.py`の
`verify_browser_load`が担当する。
"""
import os
import re

# type="module"が付いていないscriptタグを対象にする(module指定タグは
# export/importが正当なため対象外)。属性の並び順に依存しないよう、
# タグ全体の属性文字列だけをまず取り出し、その中でsrc・type="module"の
# 有無を個別に判定する。
_SCRIPT_TAG_PATTERN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
_SRC_ATTR_PATTERN = re.compile(r"""\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_MODULE_TYPE_ATTR_PATTERN = re.compile(r"""\btype\s*=\s*["']module["']""", re.IGNORECASE)

# 行頭(インデントを除く)がexport/importで始まる行を検出する。
_EXPORT_OR_IMPORT_LINE_PATTERN = re.compile(r"^\s*(export|import)\s")

# http(s):・//cdn...のような外部URLはプロジェクト内のファイルではない
# ため対象外にする。
_EXTERNAL_SRC_PATTERN = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*:|//)")


def check_script_module_mismatch(html_path: str) -> list:
    """`html_path`のHTMLファイルを解析し、`type="module"`が付いていない
    `<script src="...">`タグが読み込むJSファイルの中に、モジュール構文
    (`export`/`import`で始まる行)が含まれていないかを検査する。

    該当があれば、`"<JSファイル名>:<行番号>: '<export|import>' は
    モジュール外のscriptタグから読み込むファイルでは使用できません"`
    という形式の文字列のリストを返す(1ファイルに複数該当があれば全て
    列挙する)。該当がなければ空リストを返す。

    `html_path`自体が読めない場合・参照先のJSファイルが見つからない
    場合は、その旨をエラー扱いにはせず単に検査対象から除外する
    (HTMLの構文エラー自体は別のチェック(構文チェック等)の責務のため)。
    """
    try:
        with open(html_path, encoding="utf-8") as f:
            html_text = f.read()
    except OSError:
        return []

    html_dir = os.path.dirname(html_path)
    issues = []
    for tag_match in _SCRIPT_TAG_PATTERN.finditer(html_text):
        tag_attrs = tag_match.group(1)
        if _MODULE_TYPE_ATTR_PATTERN.search(tag_attrs):
            continue
        src_match = _SRC_ATTR_PATTERN.search(tag_attrs)
        if not src_match:
            continue
        src = src_match.group(1)
        if _EXTERNAL_SRC_PATTERN.match(src):
            continue

        js_path = os.path.join(html_dir, src)
        try:
            with open(js_path, encoding="utf-8") as f:
                js_lines = f.readlines()
        except OSError:
            continue

        js_filename = os.path.basename(js_path)
        for lineno, line in enumerate(js_lines, start=1):
            keyword_match = _EXPORT_OR_IMPORT_LINE_PATTERN.match(line)
            if keyword_match:
                keyword = keyword_match.group(1)
                issues.append(
                    f"{js_filename}:{lineno}: '{keyword}' はモジュール外のscriptタグから"
                    "読み込むファイルでは使用できません"
                )
    return issues
