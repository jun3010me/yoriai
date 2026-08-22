#!/usr/bin/env python3
"""言語非依存の設計への刷新を検証する。

これまでのYoriaiは、構文チェック(ast.parse)・テスト実行(python3/pytest)・
合意フェーズのプロンプト例(storage.py/cli.py)が、暗黙的にPythonを前提に
した作りになっていた。この結果、実機で「HTML/CSSの学習サイトを作って」
のような非Python依頼を送っても、設計担当がPython風のファイル分割を
提案してしまう不具合が見つかった。

このファイルでは、(1)依頼文からの明示的な言語判定
(`_detect_requested_language`)とそれを踏まえた合意フェーズのプロンプト
組み立て(`_build_module_breakdown_prompt`)、(2)実際に生成されたファイルの
拡張子からの言語の逆算(`_infer_language_from_tasks`)とPROGRESS.mdへの
記録・読み込み(後方互換込み)、(3)拡張子に応じた構文チェックの切り替え
(`_check_file_syntax`: Python/HTML・CSS/JavaScript(node --check)/C言語
(gcc -fsyntax-only)/未対応言語)、(4)run_testのホワイトリストの言語ごとの
切り替え(`_parse_run_test_command`・`_run_project_test_command`: node・gcc
追加)、(5)`_ask_organization_collaborate`・`_ask_organization_fix_project`・
`_resume_project`への結線、を検証する。

gcc・nodeのコンパイル/構文チェックに依存するテストは、実行環境にgcc・node
が無い場合でも(スキップの理由を明示したうえで)失敗させない。

使い方: python3 tests/test_language_agnostic.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def _make_card(device_name, model, free_gb):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 64},
        "models": {"installed": [model], "loaded": [model], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _two_member_snapshot():
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peers = [{
        "card": _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20),
        "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0,
    }]
    return {"self": self_card, "peers": peers}


# ---------------------------------------------------------------------------
# 依頼文からの明示的な言語判定
# ---------------------------------------------------------------------------

def test_detect_requested_language_finds_explicit_c():
    assert yoriai._detect_requested_language("C言語で、簡単な電卓プログラムを作って") == "C"


def test_detect_requested_language_finds_html_family():
    assert yoriai._detect_requested_language("高校生向けのHTML/CSS学習サイトを作って") == "HTML/CSS/JavaScript"


def test_detect_requested_language_distinguishes_java_from_javascript():
    """"Java"は"JavaScript"の部分文字列であるため、判定順序を誤ると
    "JavaScriptで作って"が"Java"と誤判定されてしまう。逆に"Javaで作って"は
    正しく"Java"と判定されるべきことも合わせて確認する。
    """
    assert yoriai._detect_requested_language("JavaScriptでゲームを作って") == "HTML/CSS/JavaScript"
    assert yoriai._detect_requested_language("Javaで簡単な計算機を作って") == "Java"


def test_detect_requested_language_returns_empty_when_unspecified():
    assert yoriai._detect_requested_language("ToDoリストのCLIツールを作って") == ""


def test_build_module_breakdown_prompt_embeds_explicit_language_instruction():
    prompt = yoriai._build_module_breakdown_prompt("C言語で、簡単な電卓プログラムを作って")
    assert "必ずC" in prompt, prompt


def test_build_module_breakdown_prompt_lets_architect_decide_when_unspecified():
    """依頼の項目1: 明示的な指定が無い場合は、依頼内容から設計担当自身が
    判断するよう指示するだけで、Yoriai側で言語を決め打ちしないことを
    確認する。
    """
    prompt = yoriai._build_module_breakdown_prompt("ToDoリストのCLIツールを作って")
    assert "Pythonとは限りません" in prompt, prompt


def test_build_module_breakdown_prompt_no_longer_hardcodes_python_example():
    """以前はプロンプト内の出力例が"storage.py:"/"cli.py:"という具体的な
    Python向けファイル名だった。具体例は指示文以上にモデルの出力を支配
    しやすく、これが設計担当をPython風のファイル分割へ引きずる一因に
    なっていたため、言語非依存のプレースホルダーに置き換えたことを確認する。
    """
    prompt = yoriai._build_module_breakdown_prompt("高校生向けのHTML/CSS学習サイトを作って")
    assert "storage.py" not in prompt, prompt
    assert "cli.py" not in prompt, prompt


# ---------------------------------------------------------------------------
# 実際のファイル拡張子からの言語の逆算
# ---------------------------------------------------------------------------

def test_infer_language_from_tasks_detects_html_css_js_trio():
    tasks = [("index.html", "..."), ("style.css", "..."), ("app.js", "...")]
    assert yoriai._infer_language_from_tasks(tasks) == "HTML/CSS/JavaScript"


def test_infer_language_from_tasks_detects_c():
    tasks = [("calc.c", "...")]
    assert yoriai._infer_language_from_tasks(tasks) == "C"


def test_infer_language_from_tasks_defaults_to_python_when_no_recognized_extension():
    """認識できる拡張子が1つも無い場合、この機能追加より前からの既定挙動
    (常にPythonとして扱っていた)を踏襲してPythonを返すことを確認する。
    """
    assert yoriai._infer_language_from_tasks([("README", "...")]) == "Python"
    assert yoriai._infer_language_from_tasks([]) == "Python"


def test_infer_language_from_tasks_prioritizes_actual_files_over_wishful_thinking():
    """依頼文で明示された言語(_detect_requested_language)と、実際に
    生成されたファイルの拡張子が食い違う場合、記録される「使用言語」は
    後者(実際のファイル)を正とすることを確認する(html-css-html-cssの
    ような食い違いを防ぐための設計)。
    """
    tasks = [("main.py", "...")]  # 依頼はC言語だったが、実際はPythonが生成されたと仮定
    assert yoriai._infer_language_from_tasks(tasks) == "Python"


# ---------------------------------------------------------------------------
# PROGRESS.mdへの記録・読み込み(後方互換込み)
# ---------------------------------------------------------------------------

def test_progress_markdown_round_trips_language():
    tasks = [("calc.c", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        yoriai._write_progress_md(out_dir, "C言語で電卓を作って", tasks, checklist, {}, language="C")
        with open(os.path.join(out_dir, yoriai.PROGRESS_FILENAME), encoding="utf-8") as f:
            text = f.read()
        assert "## 使用言語" in text and "\nC\n" in text, text
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "C", parsed
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_progress_markdown_omits_language_section_when_empty():
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        with open(os.path.join(out_dir, yoriai.PROGRESS_FILENAME), encoding="utf-8") as f:
            text = f.read()
        assert "## 使用言語" not in text, text
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_parse_progress_markdown_treats_missing_language_section_as_empty_string():
    """後方互換の確認: この機能追加より前に作られたPROGRESS.md(「使用言語」
    の節が無い)を読み込んでもエラーにならず、空文字列が返ることを確認する。
    """
    tasks = [("a.py", "説明")]
    checklist = yoriai._build_task_checklist(tasks)
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        # 意図的に旧バージョン相当(language引数を渡さない)で書き込む。
        yoriai._write_progress_md(out_dir, "何か作って", tasks, checklist, {})
        parsed = yoriai._parse_progress_markdown(os.path.join(out_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "", parsed
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 拡張子に応じた構文チェックの切り替え(_check_file_syntax)
# ---------------------------------------------------------------------------

def test_check_file_syntax_python_ok_and_error():
    status, _detail = yoriai._check_file_syntax("app.py", "def f():\n    return 1\n")
    assert status == "ok"
    status, detail = yoriai._check_file_syntax("app.py", "def f(:\n    pass\n")
    assert status == "error" and detail, detail


def test_check_file_syntax_skips_html_css_with_explanation():
    for filename in ("index.html", "style.css"):
        status, detail = yoriai._check_file_syntax(filename, "anything")
        assert status == "skipped", (filename, status)
        assert detail, (filename, detail)


def test_check_file_syntax_skips_unknown_extension_without_treating_it_as_error():
    """依頼の項目2: 未対応の言語は構文チェックをスキップし、エラー扱いには
    しないことを確認する。
    """
    status, detail = yoriai._check_file_syntax("main.rs", "fn main() {}")
    assert status == "skipped", status
    assert "対応していません" in detail, detail


def test_check_file_syntax_c_skips_when_gcc_unavailable():
    original_which = yoriai.shutil.which
    yoriai.shutil.which = lambda name: None
    try:
        status, detail = yoriai._check_file_syntax("calc.c", "int main() { return 0; }", file_path="/tmp/does-not-matter.c")
        assert status == "skipped", status
        assert "gcc" in detail, detail
    finally:
        yoriai.shutil.which = original_which


def test_check_file_syntax_js_skips_when_node_unavailable():
    original_which = yoriai.shutil.which
    yoriai.shutil.which = lambda name: None
    try:
        status, detail = yoriai._check_file_syntax("app.js", "function f() {}", file_path="/tmp/does-not-matter.js")
        assert status == "skipped", status
        assert "node" in detail, detail
    finally:
        yoriai.shutil.which = original_which


def test_check_file_syntax_html_skips_inline_script_when_node_unavailable():
    original_which = yoriai.shutil.which
    yoriai.shutil.which = lambda name: None
    try:
        status, detail = yoriai._check_file_syntax("index.html", "<script>function f(\n</script>")
        assert status == "skipped", status
        assert "node" in detail, detail
    finally:
        yoriai.shutil.which = original_which


_GCC_AVAILABLE = shutil.which("gcc") is not None or shutil.which("cc") is not None
_NODE_AVAILABLE = shutil.which("node") is not None


def test_check_file_syntax_c_detects_valid_and_invalid_code_with_real_gcc():
    """依頼の動作確認: 「C言語で作って」の依頼でコンパイルチェックまで
    機能することを確認する。実機にgccが無い環境でも、その旨を明示して
    テスト自体は失敗させない(依頼の「gcc等のコンパイラが実機に存在する
    場合はコンパイルチェック、存在しない場合はスキップ」という要件通り)。
    """
    if not _GCC_AVAILABLE:
        print("  (gccが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        valid_path = os.path.join(out_dir, "ok.c")
        with open(valid_path, "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n")
        status, detail = yoriai._check_file_syntax("ok.c", open(valid_path).read(), file_path=valid_path)
        assert status == "ok", (status, detail)

        broken_path = os.path.join(out_dir, "broken.c")
        with open(broken_path, "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0 \n")  # 閉じ括弧・セミコロン欠落
        status, detail = yoriai._check_file_syntax("broken.c", open(broken_path).read(), file_path=broken_path)
        assert status == "error", (status, detail)
        assert detail, detail
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_check_file_syntax_js_detects_valid_and_invalid_code_with_real_node():
    """依頼1の動作確認: 「node --check」でJavaScriptの構文チェックまで
    機能することを確認する。実機にnodeが無い環境でも、その旨を明示して
    テスト自体は失敗させない。
    """
    if not _NODE_AVAILABLE:
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        valid_path = os.path.join(out_dir, "ok.js")
        with open(valid_path, "w", encoding="utf-8") as f:
            f.write("function add(a, b) { return a + b; }\n")
        status, detail = yoriai._check_file_syntax("ok.js", open(valid_path).read(), file_path=valid_path)
        assert status == "ok", (status, detail)

        broken_path = os.path.join(out_dir, "broken.js")
        with open(broken_path, "w", encoding="utf-8") as f:
            f.write("function add(a, b) { return a + b \n")  # 閉じ括弧欠落
        status, detail = yoriai._check_file_syntax("broken.js", open(broken_path).read(), file_path=broken_path)
        assert status == "error", (status, detail)
        assert detail, detail
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_check_file_syntax_html_detects_inline_script_syntax_errors_with_real_node():
    """依頼の動作確認: HTML/CSSと組み合わせて`<script>`タグに直接書かれた
    JavaScript(外部の.jsファイルに分割されていないインラインコード)も、
    node --checkで構文チェックされることを確認する。実機にnodeが無い
    環境ではその旨を明示してテスト自体は失敗させない。
    """
    if not _NODE_AVAILABLE:
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    valid_html = (
        "<html><head><style>body { color: red; }</style></head><body>\n"
        "<script>\nfunction add(a, b) { return a + b; }\n</script>\n"
        "</body></html>\n"
    )
    status, detail = yoriai._check_file_syntax("index.html", valid_html)
    assert status == "ok", (status, detail)

    broken_html = (
        "<html><body>\n<script>\nfunction add(a, b) { return a + b\n</script>\n</body></html>\n"
    )
    status, detail = yoriai._check_file_syntax("index.html", broken_html)
    assert status == "error", (status, detail)
    assert detail, detail


def test_check_file_syntax_html_ignores_external_and_non_javascript_script_tags():
    """`src`属性を持つ外部スクリプト参照(壊れていてもHTML側では検出
    しない。既にそのファイル自体が.jsとして個別にチェックされるため)と、
    `type="application/json"`のようなJavaScript以外の`<script>`は、
    インラインJSチェックの対象から除外されることを確認する。
    """
    if not _NODE_AVAILABLE:
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    html = (
        '<script src="app.js"></script>\n'
        '<script type="application/json">{"a": 1,}</script>\n'
        "<p>スクリプトなし</p>\n"
    )
    status, detail = yoriai._check_file_syntax("index.html", html)
    assert status == "skipped", (status, detail)


def test_check_file_syntax_html_supports_module_type_inline_script():
    """`type="module"`のインラインスクリプトは、`import`/`export`構文を
    通常のスクリプトと誤判定して構文エラー扱いにしないことを確認する。
    """
    if not _NODE_AVAILABLE:
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    html = '<script type="module">\nexport function add(a, b) { return a + b; }\n</script>\n'
    status, detail = yoriai._check_file_syntax("index.html", html)
    assert status == "ok", (status, detail)


# ---------------------------------------------------------------------------
# run_testのホワイトリスト(言語ごとの切り替え)
# ---------------------------------------------------------------------------

def test_parse_run_test_command_accepts_node():
    assert yoriai._parse_run_test_command("node app.js") == (["node", "app.js"], None)


def test_parse_run_test_command_accepts_gcc_compile_and_run_binary():
    assert yoriai._parse_run_test_command("gcc calc.c -o calc") == (["gcc", "calc.c", "-o", "calc"], None)
    assert yoriai._parse_run_test_command("./calc") == (["./calc"], None)


def test_parse_run_test_command_still_rejects_disallowed_forms():
    argv, error = yoriai._parse_run_test_command("rm -rf /")
    assert argv is None
    assert error is not None
    argv, error = yoriai._parse_run_test_command("gcc calc.c -Wall -o calc")  # -o が想定位置に無い
    assert argv is None, argv


def test_run_project_test_command_compiles_and_runs_c_program_end_to_end():
    """依頼の動作確認: C言語の「コンパイル→実行」の一連の手順が、
    run_testツールの2回の呼び出し(gccでコンパイル→./<出力>で実行)で
    実際に機能することを確認する。
    """
    if not _GCC_AVAILABLE:
        print("  (gccが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        with open(os.path.join(out_dir, "calc.c"), "w", encoding="utf-8") as f:
            f.write(
                '#include <stdio.h>\n'
                'int main(void) { printf("%d\\n", 1 + 2); return 0; }\n'
            )
        compile_result = json.loads(yoriai._run_project_test_command(out_dir, "gcc calc.c -o calc"))
        assert compile_result["ok"] is True, compile_result
        assert os.path.isfile(os.path.join(out_dir, "calc"))

        run_result = json.loads(yoriai._run_project_test_command(out_dir, "./calc"))
        assert run_result["ok"] is True, run_result
        assert "3" in run_result["output"], run_result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_rejects_running_binary_that_was_not_compiled():
    """"./<出力>"は、対象ファイルがプロジェクト内に実在しない限り実行
    できないことを確認する(write_fileはテキストを書き込むだけで実行権限を
    付与しないため、実質的にgccのコンパイル手順を経たファイルしか実行
    できない、という安全設計を裏付ける)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        result = json.loads(yoriai._run_project_test_command(out_dir, "./nonexistent"))
        assert result["ok"] is False, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_rejects_path_traversal_in_gcc_output_name():
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        with open(os.path.join(out_dir, "calc.c"), "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n")
        result = json.loads(yoriai._run_project_test_command(out_dir, "gcc calc.c -o ../../evil"))
        assert result["ok"] is False, result
        assert not os.path.exists(os.path.join(os.path.dirname(out_dir), "evil"))
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_test_command_executes_node_script_when_available():
    if not shutil.which("node"):
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        with open(os.path.join(out_dir, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log(1 + 2);\n")
        result = json.loads(yoriai._run_project_test_command(out_dir, "node app.js"))
        assert result["ok"] is True, result
        assert "3" in result["output"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _ask_organization_collaborateへの結線(統合テスト)
# ---------------------------------------------------------------------------

def test_collaborate_records_c_language_from_explicit_request():
    """依頼の動作確認: 「C言語で、簡単な電卓プログラムを作って」で、
    設計担当への指示にC言語が明示され、PROGRESS.mdにも"C"が記録される
    ことを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    received_breakdown_prompt = {}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        request_text = messages[0]["content"]
        if "ファイルに分割する実装計画" in request_text:
            received_breakdown_prompt["text"] = request_text
            yield {"content": "calc.c: 四則演算を行うmain関数を実装する\n"}
        else:
            yield {"content": "```c\nint main(void) { return 0; }\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(
                47120, "fingerprint", "C言語で、簡単な電卓プログラムを作って", out_dir,
            )
        assert "必ずC" in received_breakdown_prompt.get("text", ""), received_breakdown_prompt

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME,
            yoriai._generate_project_name("C言語で、簡単な電卓プログラムを作って"),
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "C", parsed
        assert os.path.isfile(os.path.join(project_dir, "calc.c"))
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_collaborate_still_defaults_python_regression():
    """依頼の動作確認: これまで通り「ToDoリストのCLIツールを作って」の
    ような依頼(言語が明示されない)も、既存の.py生成物・PROGRESS.mdの
    記録内容ともに問題なく動作することを確認する(既存機能のリグレッション
    が無いことの確認)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        request_text = messages[0]["content"]
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": (
                "storage.py: ToDoをJSONで管理する\n"
                "cli.py: storageを使うCLI\n"
            )}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)

        project_dir = os.path.join(
            out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._generate_project_name("ToDoリストのCLIツールを作って"),
        )
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "Python", parsed
        assert set(os.listdir(project_dir)) == {"storage.py", "cli.py", "PROGRESS.md"}
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _resume_projectへの結線(後方互換の補完)
# ---------------------------------------------------------------------------

def test_resume_project_backfills_language_for_old_progress_md():
    """この機能追加より前に作られたPROGRESS.md(「使用言語」の節が無い)を
    再開すると、モジュール分割案のファイル拡張子から言語が補完されて
    書き戻されることを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()

    def fake_stream_ok(candidate, org_fingerprint, messages, **_kwargs):
        text = messages[0]["content"]
        if "レビュー対象" in text:
            yield {"content": "問題なし"}
        else:
            yield {"content": "```c\nint main(void) { return 0; }\n```"}
        yield {"done": True}

    yoriai._stream_chat_from_candidate = fake_stream_ok

    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        tasks = [("calc.c", "説明")]
        checklist = yoriai._build_task_checklist(tasks)
        project_dir = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME, "calc-project")
        # 意図的に旧バージョン相当(language引数を渡さない)で書き込む。
        yoriai._write_progress_md(project_dir, "電卓を作って", tasks, checklist, {})

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._resume_project(project_dir, 47120, "fingerprint")

        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "C", parsed
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _ask_organization_fix_projectへの結線
# ---------------------------------------------------------------------------

def _write_completed_project(projects_root, name, tasks, request, language):
    checklist = yoriai._build_task_checklist(tasks)
    for filename, _content in tasks:
        yoriai._set_task_status(checklist, filename, "impl", yoriai._TASK_STATUS_COMPLETED)
        yoriai._set_task_status(checklist, filename, "review", yoriai._TASK_STATUS_COMPLETED)
    project_dir = os.path.join(projects_root, name)
    yoriai._write_progress_md(project_dir, request, tasks, checklist, {}, language=language)
    for filename, _content in tasks:
        with open(os.path.join(project_dir, filename), "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n" if filename.endswith(".c") else "pass\n")
    return project_dir


def test_fix_project_embeds_language_in_prompt_and_preserves_it_after_fix():
    """依頼の項目4: //fixでの修正時も、対象プロジェクトの言語設定が
    修正担当へのプロンプトに含まれ、修正後もPROGRESS.mdの「使用言語」が
    消えずに残ることを確認する。
    """
    captured = {}

    def fake_stream(candidate, org_fingerprint, messages, offer_project_tools=False, **_kwargs):
        if not captured:
            captured["prompt"] = messages[0]["content"]
        if not [m for m in messages if m.get("role") == "tool"]:
            yield {"pending_tool_calls": [
                {"id": "call_1", "type": "function", "function": {
                    "name": "write_file",
                    "arguments": {"filename": "calc.c", "content": "int main(void) { return 1 + 2; }\n"},
                }},
            ]}
            return
        yield {"content": "計算処理を修正しました。"}
        yield {"done": True}

    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate
    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _two_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_lang_test_")
    try:
        projects_root = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME)
        project_dir = _write_completed_project(
            projects_root, "calc-project", [("calc.c", "電卓のメイン処理")], "C言語で電卓を作って", language="C",
        )

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            yoriai._ask_organization_fix_project(47120, "fingerprint", "calc-project: 計算結果がおかしいので直して", out_dir)

        assert "C" in captured.get("prompt", ""), captured
        parsed = yoriai._parse_progress_markdown(os.path.join(project_dir, yoriai.PROGRESS_FILENAME))
        assert parsed["language"] == "C", (
            f"修正後もPROGRESS.mdの使用言語が保持されるはずです: {parsed}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_detect_requested_language_finds_explicit_c,
        test_detect_requested_language_finds_html_family,
        test_detect_requested_language_distinguishes_java_from_javascript,
        test_detect_requested_language_returns_empty_when_unspecified,
        test_build_module_breakdown_prompt_embeds_explicit_language_instruction,
        test_build_module_breakdown_prompt_lets_architect_decide_when_unspecified,
        test_build_module_breakdown_prompt_no_longer_hardcodes_python_example,
        test_infer_language_from_tasks_detects_html_css_js_trio,
        test_infer_language_from_tasks_detects_c,
        test_infer_language_from_tasks_defaults_to_python_when_no_recognized_extension,
        test_infer_language_from_tasks_prioritizes_actual_files_over_wishful_thinking,
        test_progress_markdown_round_trips_language,
        test_progress_markdown_omits_language_section_when_empty,
        test_parse_progress_markdown_treats_missing_language_section_as_empty_string,
        test_check_file_syntax_python_ok_and_error,
        test_check_file_syntax_skips_html_css_with_explanation,
        test_check_file_syntax_skips_unknown_extension_without_treating_it_as_error,
        test_check_file_syntax_c_skips_when_gcc_unavailable,
        test_check_file_syntax_c_detects_valid_and_invalid_code_with_real_gcc,
        test_check_file_syntax_js_skips_when_node_unavailable,
        test_check_file_syntax_js_detects_valid_and_invalid_code_with_real_node,
        test_check_file_syntax_html_skips_inline_script_when_node_unavailable,
        test_check_file_syntax_html_detects_inline_script_syntax_errors_with_real_node,
        test_check_file_syntax_html_ignores_external_and_non_javascript_script_tags,
        test_check_file_syntax_html_supports_module_type_inline_script,
        test_parse_run_test_command_accepts_node,
        test_parse_run_test_command_accepts_gcc_compile_and_run_binary,
        test_parse_run_test_command_still_rejects_disallowed_forms,
        test_run_project_test_command_compiles_and_runs_c_program_end_to_end,
        test_run_project_test_command_rejects_running_binary_that_was_not_compiled,
        test_run_project_test_command_rejects_path_traversal_in_gcc_output_name,
        test_run_project_test_command_executes_node_script_when_available,
        test_collaborate_records_c_language_from_explicit_request,
        test_collaborate_still_defaults_python_regression,
        test_resume_project_backfills_language_for_old_progress_md,
        test_fix_project_embeds_language_in_prompt_and_preserves_it_after_fix,
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
