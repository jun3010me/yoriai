#!/usr/bin/env python3
"""「検証専用の、より柔軟なコマンド実行ツールへの刷新」を検証する。

以前のrun_testは拡張子ごとに決め打ちのコマンド(.py→python3、.c→gccなど)
しか実行できず、対応していない言語・状況(HTML/CSS等)ではモデルが見当
違いのコマンドを何度も試してツール呼び出しを無駄にする不具合が繰り返し
報告された。run_commandはコマンド文字列を自由に指定できる代わりに、
実行前に機械的なフィルタ(ブロックリスト方式)を必ず通す設計に置き換えた。

このファイルでは、(1)`_validate_run_command`によるブロックリスト検証
(ネットワークアクセス・破壊的操作・権限昇格・シェル起動・パス脱出・
シェル制御文字・インタプリタのインラインコード実行フラグの拒否、および
gccの-cのような無関係な同名フラグを誤って禁止しないこと)、(2)
`_run_subprocess_with_output_cap`による実行時間・出力量の上限
(タイムアウトでの強制終了、'yes'のように出力が止まらないコマンドでも
メモリを圧迫しないこと)、(3)`_run_project_command`(check_htmlの特別
扱いを含む)、(4)`_execute_project_tool_call`との結線、を検証する。

実機にgcc/node/Playwrightが無い環境でも、それぞれの本体テストは理由を
明示したうえでスキップし、失敗はさせない。

使い方: python3 tests/test_run_command.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


_GCC_AVAILABLE = shutil.which("gcc") is not None or shutil.which("cc") is not None
_NODE_AVAILABLE = shutil.which("node") is not None


# ---------------------------------------------------------------------------
# check_htmlのテスト用ヘルパー
# ---------------------------------------------------------------------------

def _uninstall_fake_playwright():
    """他のテストファイルがsys.modulesにplaywrightの偽物を残していないことを
    保証する(「実機にPlaywrightが無い」ケースを確実にテストするため)。
    """
    sys.modules.pop("playwright", None)
    sys.modules.pop("playwright.sync_api", None)


# ---------------------------------------------------------------------------
# _validate_run_command: ブロックリスト検証
# ---------------------------------------------------------------------------

def test_validate_run_command_rejects_network_tools():
    for cmd in ("curl http://example.com", "wget http://example.com", "ssh host", "scp a b", "git push"):
        argv, error = yoriai._validate_run_command(cmd)
        assert argv is None, (cmd, argv)
        assert error is not None, cmd


def test_validate_run_command_rejects_destructive_file_ops():
    for cmd in ("rm -rf /", "mv a.py b.py", "cp a.py b.py", "dd if=/dev/zero"):
        argv, error = yoriai._validate_run_command(cmd)
        assert argv is None, (cmd, argv)
        assert "write_file" in error or "move_file" in error or "delete_file" in error, error


def test_validate_run_command_rejects_privilege_escalation():
    argv, error = yoriai._validate_run_command("sudo rm -rf /")
    assert argv is None, argv
    assert error is not None


def test_validate_run_command_rejects_shell_invocation():
    for cmd in ("bash -c 'rm -rf /'", "sh script.sh", "cd /tmp"):
        argv, error = yoriai._validate_run_command(cmd)
        assert argv is None, (cmd, argv)


def test_validate_run_command_rejects_wrapper_bypass_commands():
    """'env'や'timeout'のようなラッパー経由でブロックリストを迂回しようと
    しても、コマンド文字列内の全トークンをチェックしているため防げることを
    確認する。
    """
    for cmd in ("env curl http://example.com", "timeout 5 curl http://example.com", "xargs rm -rf"):
        argv, error = yoriai._validate_run_command(cmd)
        assert argv is None, (cmd, argv)


def test_validate_run_command_rejects_shell_metacharacters():
    for cmd in ("python3 a.py; rm -rf /", "python3 a.py && rm -rf /", "python3 a.py | tee out",
                "python3 a.py > out.txt", "echo `whoami`", "echo $(whoami)"):
        argv, error = yoriai._validate_run_command(cmd)
        assert argv is None, (cmd, argv)


def test_validate_run_command_rejects_absolute_and_traversal_paths():
    argv, error = yoriai._validate_run_command("python3 /etc/passwd")
    assert argv is None, argv
    argv, error = yoriai._validate_run_command("gcc ../../evil.c -o ../../evil")
    assert argv is None, argv


def test_validate_run_command_rejects_interpreter_inline_eval_flags():
    argv, error = yoriai._validate_run_command("python3 -c 'import os; os.system(\"ls\")'")
    assert argv is None, argv
    # 仮の判断(統合検証ループへの対応): -mは全面禁止から「許可リストに
    # あるモジュールのみ通す」方式に緩和したため、許可リストに無い
    # モジュール(osはファイル操作等の任意のコード実行につながりうる)を
    # 使って引き続き拒否されることを確認する。python3 -m pytest等の
    # 許可されるケースはtest_validate_run_command_allows_python_dash_m_
    # for_allowlisted_modulesで確認する。
    argv, error = yoriai._validate_run_command("python3 -m os")
    assert argv is None, argv
    argv, error = yoriai._validate_run_command("node -e 'console.log(1)'")
    assert argv is None, argv


def test_validate_run_command_allows_python_dash_m_for_allowlisted_modules():
    """統合検証ループへの対応: python3 -m pytest・python3 -m py_compileの
    ような正当な検証手段は、-mの直後のモジュール名が許可リスト
    (_PYTHON_ALLOWED_MODULES)にあれば通ることを確認する。
    """
    assert yoriai._validate_run_command("python3 -m pytest") == (["python3", "-m", "pytest"], None)
    assert yoriai._validate_run_command("python3 -m py_compile app.py") == (
        ["python3", "-m", "py_compile", "app.py"], None,
    )


def test_validate_run_command_rejects_python_dash_m_for_disallowed_modules():
    """許可リストに無いモジュール(例: os)を-mで実行しようとした場合は
    引き続き拒否されることを確認する(-mの全面禁止から条件付き許可への
    緩和が、無条件の許可になっていないことの確認)。
    """
    argv, error = yoriai._validate_run_command("python3 -m os")
    assert argv is None, argv
    assert error is not None and "os" in error, error


def test_validate_run_command_does_not_block_gccs_unrelated_dash_c_flag():
    """gccの-c(リンクなしコンパイル)は、python3の-c(インラインコード
    実行)とは無関係の正当なフラグであり、誤って禁止されないことを確認する。
    """
    argv, error = yoriai._validate_run_command("gcc -c calc.c -o calc.o")
    assert argv == ["gcc", "-c", "calc.c", "-o", "calc.o"], (argv, error)
    assert error is None, error


def test_validate_run_command_accepts_flexible_validation_commands():
    """依頼の動作確認: .js/.c/.pyの各ファイルに対して、モデルが自分で
    適切なコマンドを選んで実行できることを確認する(拡張子ごとの決め打ち
    ではなく、任意のコマンドとして受理される)。
    """
    assert yoriai._validate_run_command("python3 app.py") == (["python3", "app.py"], None)
    assert yoriai._validate_run_command("node --check app.js") == (["node", "--check", "app.js"], None)
    assert yoriai._validate_run_command("gcc -fsyntax-only calc.c") == (["gcc", "-fsyntax-only", "calc.c"], None)
    assert yoriai._validate_run_command("pytest") == (["pytest"], None)
    assert yoriai._validate_run_command("check_html index.html") == (["check_html", "index.html"], None)
    assert yoriai._validate_run_command("./calc") == (["./calc"], None)


def test_validate_run_command_allows_unrecognized_validator_tools():
    """依頼の動作確認: html5validatorのような、Yoriaiが名前を知らない
    検証ツールでも(ブロックリストに無ければ)自由に試せることを確認する
    (拡張子ごとの決め打ちホワイトリストをやめたことの裏返し)。
    """
    assert yoriai._validate_run_command("html5validator index.html") == (
        ["html5validator", "index.html"], None,
    )
    assert yoriai._validate_run_command("tidy -q -e index.html") == (
        ["tidy", "-q", "-e", "index.html"], None,
    )


def test_validate_run_command_rejects_empty_command():
    argv, error = yoriai._validate_run_command("")
    assert argv is None
    assert error is not None


# ---------------------------------------------------------------------------
# _run_subprocess_with_output_cap: タイムアウト・出力量の上限
# ---------------------------------------------------------------------------

def test_run_subprocess_with_output_cap_kills_infinite_loop_on_timeout():
    """依頼の動作確認: 意図的に無限ループするコマンド('yes')を実行させ、
    タイムアウトによって強制終了することを確認する。読み取り量の上限
    (read_cap_chars)によって、出力を溜め続けてメモリを圧迫しないことも
    合わせて確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        returncode, output, timed_out = yoriai._run_subprocess_with_output_cap(
            ["yes"], out_dir, timeout_sec=1, read_cap_chars=2000,
        )
        assert timed_out is True, (returncode, len(output), timed_out)
        # 1回のos.read()は最大65536バイトまとめて読むため、read_cap_charsを
        # 少し超えることはあるが、無制限に溜め込んでいないことだけを確認する
        # (仮に上限が機能していなければ、1秒間の'yes'の出力は数十MB規模になる)。
        assert len(output) < 200_000, f"出力キャプチャの上限が働いていません(実際: {len(output)}文字)"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_subprocess_with_output_cap_kills_silent_hang_on_timeout():
    """出力をまったく出さないコマンド('sleep')でも、select経由の
    デッドライン確認によって確実にタイムアウトで強制終了できることを
    確認する('yes'の出力量チェックだけに頼った実装では見逃しうるケース)。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        returncode, output, timed_out = yoriai._run_subprocess_with_output_cap(
            ["sleep", "5"], out_dir, timeout_sec=1, read_cap_chars=2000,
        )
        assert timed_out is True, (returncode, output, timed_out)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_subprocess_with_output_cap_returns_normally_when_fast():
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        returncode, output, timed_out = yoriai._run_subprocess_with_output_cap(
            ["echo", "hello"], out_dir, timeout_sec=5, read_cap_chars=2000,
        )
        assert timed_out is False, (returncode, output, timed_out)
        assert returncode == 0, (returncode, output)
        assert "hello" in output, output
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _run_project_command: 実行・タイムアウト・出力の上限・check_html結線
# ---------------------------------------------------------------------------

def test_run_project_command_rejects_curl_before_executing():
    """依頼の動作確認: curlのような危険なコマンドが、実行前に確実に
    拒否されることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        result = json.loads(yoriai._run_project_command(out_dir, "curl http://example.com"))
        assert result["ok"] is False, result
        assert "message" in result, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_rejects_rm_rf_before_executing():
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        marker = os.path.join(out_dir, "keep.txt")
        with open(marker, "w", encoding="utf-8") as f:
            f.write("keep me\n")
        result = json.loads(yoriai._run_project_command(out_dir, "rm -rf ."))
        assert result["ok"] is False, result
        assert os.path.exists(marker), "rm -rf が実行されてしまっています"
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_executes_python_file_when_chosen_by_model():
    """依頼の動作確認: .pyファイルに対して、モデルが自分でpython3を選んで
    実行できることを確認する。
    """
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("print(1 + 2)\n")
        result = json.loads(yoriai._run_project_command(out_dir, "python3 app.py"))
        assert result["ok"] is True, result
        assert "3" in result["output"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_executes_node_check_when_available():
    if not _NODE_AVAILABLE:
        print("  (nodeが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "app.js"), "w", encoding="utf-8") as f:
            f.write("function f() {}\n")
        result = json.loads(yoriai._run_project_command(out_dir, "node --check app.js"))
        assert result["ok"] is True, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_executes_gcc_syntax_only_when_available():
    if not _GCC_AVAILABLE:
        print("  (gccが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "calc.c"), "w", encoding="utf-8") as f:
            f.write("int main(void) { return 0; }\n")
        result = json.loads(yoriai._run_project_command(out_dir, "gcc -fsyntax-only calc.c"))
        assert result["ok"] is True, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_still_compiles_and_runs_c_program_end_to_end():
    if not _GCC_AVAILABLE:
        print("  (gccが見つからないため、このテストの本体はスキップします)")
        return
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "calc.c"), "w", encoding="utf-8") as f:
            f.write('#include <stdio.h>\nint main(void) { printf("%d\\n", 1 + 2); return 0; }\n')
        compile_result = json.loads(yoriai._run_project_command(out_dir, "gcc calc.c -o calc"))
        assert compile_result["ok"] is True, compile_result
        run_result = json.loads(yoriai._run_project_command(out_dir, "./calc"))
        assert run_result["ok"] is True, run_result
        assert "3" in run_result["output"], run_result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_check_html_detects_console_error():
    # コンソールエラーの内容を個々のテストごとに変えたいので、
    # _check_html_with_playwright自体を差し替える(playwrightモジュールの
    # 差し込みだけでは検証したいエラー内容の制御がしづらいため)。
    original = yoriai._check_html_with_playwright
    yoriai._check_html_with_playwright = lambda path: ("error", "id 'preview-frame' not found")
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>")
        result = json.loads(yoriai._run_project_command(out_dir, "check_html index.html"))
        assert result["ok"] is False, result
        assert "preview-frame" in result["output"], result
    finally:
        yoriai._check_html_with_playwright = original
        _uninstall_fake_playwright()
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_check_html_skips_without_playwright():
    _uninstall_fake_playwright()
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html></html>")
        result = json.loads(yoriai._run_project_command(out_dir, "check_html index.html"))
        assert result["ok"] is False, result
        assert "message" in result and "Playwright" in result["message"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_project_command_rejects_running_binary_that_was_not_compiled():
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        result = json.loads(yoriai._run_project_command(out_dir, "./nonexistent"))
        assert result["ok"] is False, result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# _execute_project_tool_call経由の結線
# ---------------------------------------------------------------------------

def test_execute_project_tool_call_dispatches_run_command():
    out_dir = tempfile.mkdtemp(prefix="yoriai_run_command_test_")
    try:
        with open(os.path.join(out_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("print('hi')\n")
        tool_call = {
            "id": "call_1", "type": "function",
            "function": {"name": "run_command", "arguments": {"command": "python3 app.py"}},
        }
        result = json.loads(yoriai._execute_project_tool_call(out_dir, tool_call))
        assert result["ok"] is True, result
        assert "hi" in result["output"], result
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_project_tools_schemas_use_run_command_not_run_test():
    tool_names = {schema["function"]["name"] for schema in yoriai.PROJECT_TOOLS_SCHEMAS}
    assert "run_command" in tool_names, tool_names
    assert "run_test" not in tool_names, tool_names
    assert yoriai.RUN_COMMAND_TOOL_NAME in yoriai.PROJECT_TOOLS_CLIENT_NAMES


def main():
    tests = [
        test_validate_run_command_rejects_network_tools,
        test_validate_run_command_rejects_destructive_file_ops,
        test_validate_run_command_rejects_privilege_escalation,
        test_validate_run_command_rejects_shell_invocation,
        test_validate_run_command_rejects_wrapper_bypass_commands,
        test_validate_run_command_rejects_shell_metacharacters,
        test_validate_run_command_rejects_absolute_and_traversal_paths,
        test_validate_run_command_rejects_interpreter_inline_eval_flags,
        test_validate_run_command_allows_python_dash_m_for_allowlisted_modules,
        test_validate_run_command_rejects_python_dash_m_for_disallowed_modules,
        test_validate_run_command_does_not_block_gccs_unrelated_dash_c_flag,
        test_validate_run_command_accepts_flexible_validation_commands,
        test_validate_run_command_allows_unrecognized_validator_tools,
        test_validate_run_command_rejects_empty_command,
        test_run_subprocess_with_output_cap_kills_infinite_loop_on_timeout,
        test_run_subprocess_with_output_cap_kills_silent_hang_on_timeout,
        test_run_subprocess_with_output_cap_returns_normally_when_fast,
        test_run_project_command_rejects_curl_before_executing,
        test_run_project_command_rejects_rm_rf_before_executing,
        test_run_project_command_executes_python_file_when_chosen_by_model,
        test_run_project_command_executes_node_check_when_available,
        test_run_project_command_executes_gcc_syntax_only_when_available,
        test_run_project_command_still_compiles_and_runs_c_program_end_to_end,
        test_run_project_command_check_html_detects_console_error,
        test_run_project_command_check_html_skips_without_playwright,
        test_run_project_command_rejects_running_binary_that_was_not_compiled,
        test_execute_project_tool_call_dispatches_run_command,
        test_project_tools_schemas_use_run_command_not_run_test,
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
