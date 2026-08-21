#!/usr/bin/env python3
"""実行モードの自動判定(単発/比較/協業)と、協業モード(`//agree`)の
「合意フェーズ→並行実装」ロジックを検証する。

依頼の「動作確認」で明示された3パターン(単発/比較/協業それぞれの判定)に
加えて、実験1で見つかった関数名不一致問題に対する`//agree`の狙い
(設計担当が提示したインターフェースが、そのまま各メンバーへの実装依頼に
渡ること)を検証する。実機のOllama/LM Studio/MLX-LMには接続できない環境の
ため、`_fetch_org_snapshot`と`_stream_chat_from_candidate`を差し替えて、
設計担当1台+実装担当2台を模擬する。

使い方: python3 tests/test_auto_mode_and_agree.py
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import yoriai  # noqa: E402


def test_execution_mode_classification_matches_requested_examples():
    """依頼で明示された3つの実行例がそれぞれ想定通りのモードに
    判定されることを確認する。
    """
    assert yoriai._classify_execution_mode("富士山の標高は?") == yoriai.EXECUTION_MODE_SINGLE
    assert yoriai._classify_execution_mode("このライブラリについて意見を聞かせて") == yoriai.EXECUTION_MODE_COMPARE
    assert yoriai._classify_execution_mode("ToDoリストのCLIツールを作って") == yoriai.EXECUTION_MODE_COLLABORATE


def test_parse_module_breakdown_handles_bullets_and_code_fence():
    """設計担当の回答が、箇条書き記号やコードフェンス付きで返ってきても
    正しく解析できることを確認する。
    """
    answer = (
        "```\n"
        "- storage.py: add_todo(text)->int, list_todos()->list を実装\n"
        "- cli.py: storageのadd_todo/list_todosを呼ぶCLI\n"
        "```\n"
    )
    tasks = yoriai._parse_module_breakdown(answer)
    assert tasks == [
        ("storage.py", "add_todo(text)->int, list_todos()->list を実装"),
        ("cli.py", "storageのadd_todo/list_todosを呼ぶCLI"),
    ], tasks


def test_parse_module_breakdown_groups_nested_function_signatures_under_their_file():
    """実機で報告された不具合の再現: 設計担当が「ファイル名を見出しとし、
    その配下に関数シグネチャを箇条書きでぶら下げる」階層的な形式で回答した
    場合、見出し配下の詳細行(関数シグネチャ)までファイルとして誤認識
    せず、見出し(ファイル)単位でグルーピングされることを確認する。

    実機では、この誤認識により9項目のフラットなリストとして解析され、
    本来は関数シグネチャである`load_todos() -> list`が実装依頼の宛先
    (=ファイル名)として扱われてしまい、cli.pyが一度も実装依頼されない
    という不具合が発生した。
    """
    answer = (
        "storage.py: ToDoリストをJSONファイルで管理するモジュール\n"
        "- load_todos() -> list: todos.jsonから読み込んで返す\n"
        "- save_todos(todos: list) -> None: todos.jsonに保存する\n"
        "- add_todo(text: str) -> int: 新しいTodoを追加してIDを返す\n"
        "- complete_todo(id: int) -> bool: 指定IDを完了にする\n"
        "- delete_todo(id: int) -> bool: 指定IDを削除する\n"
        "\n"
        "cli.py: storageモジュールを呼び出すCLI\n"
        "- storage.add_todo, storage.load_todos, storage.complete_todo, storage.delete_todoを呼び出す\n"
        "- python cli.py add \"text\" のようなコマンド体系\n"
    )
    tasks = yoriai._parse_module_breakdown(answer)
    filenames = [fn for fn, _content in tasks]
    assert filenames == ["storage.py", "cli.py"], (
        f"ファイル名だけが見出しとして拾われるべきですが、関数シグネチャまで"
        f"ファイルとして誤認識されています: {filenames}"
    )

    storage_content = dict(tasks)["storage.py"]
    cli_content = dict(tasks)["cli.py"]
    assert "load_todos() -> list" in storage_content, storage_content
    assert "save_todos(todos: list) -> None" in storage_content, storage_content
    assert "add_todo(text: str) -> int" in storage_content, storage_content
    assert "storage.add_todo" in cli_content, cli_content


def test_parse_module_breakdown_is_not_specific_to_the_todo_example():
    """ToDoリストというお題に限定されない汎用的な解析であることを、
    全く異なる構成(電卓アプリ)の例で確認する。
    """
    answer = (
        "calculator.py: 四則演算のロジックを実装\n"
        "- add(a: float, b: float) -> float: 加算する\n"
        "- subtract(a: float, b: float) -> float: 減算する\n"
        "- multiply(a: float, b: float) -> float: 乗算する\n"
        "- divide(a: float, b: float) -> float: 除算する(0除算はValueErrorを送出)\n"
        "\n"
        "display.py: 計算結果の表示を担当\n"
        "- format_result(value: float) -> str: 計算結果を表示用の文字列に整形する\n"
        "- calculator.add/subtract/multiply/divideを呼び出して結果をformat_resultで表示する\n"
    )
    tasks = yoriai._parse_module_breakdown(answer)
    filenames = [fn for fn, _content in tasks]
    assert filenames == ["calculator.py", "display.py"], filenames

    calculator_content = dict(tasks)["calculator.py"]
    display_content = dict(tasks)["display.py"]
    assert "add(a: float, b: float) -> float" in calculator_content, calculator_content
    assert "divide(a: float, b: float) -> float" in calculator_content, calculator_content
    assert "format_result(value: float) -> str" in display_content, display_content
    assert "calculator.add/subtract/multiply/divide" in display_content, display_content


def test_parse_module_breakdown_still_handles_flat_single_line_format():
    """階層形式への対応を追加しても、従来通りの1行1ファイル形式(見出しの
    直後に説明が続く形式)も引き続き正しく解析できることを確認する
    (退行検知)。
    """
    answer = (
        "storage.py: add_todo(text: str) -> int を実装\n"
        "cli.py: storage.add_todoを呼び出すCLI\n"
    )
    tasks = yoriai._parse_module_breakdown(answer)
    assert tasks == [
        ("storage.py", "add_todo(text: str) -> int を実装"),
        ("cli.py", "storage.add_todoを呼び出すCLI"),
    ], tasks


def _make_card(device_name, model, free_gb):
    return {
        "device_name": device_name,
        "os": {"system": "Darwin", "release": "1", "machine": "arm64", "chip": "Apple M2"},
        "memory": {"free_gb": free_gb, "total_gb": 64},
        "models": {"installed": [model], "loaded": [model], "backends": ["lmstudio"]},
        "generated_at": "2026-08-20T00:00:00+0900",
    }


def _three_member_snapshot():
    # 優先順位(free_gb降順): self(MacStudio) > peerA(junnoMac-mini) > peerB(raspi4)
    self_card = _make_card("MacStudio", "qwen2.5-coder-32b", free_gb=40)
    peer_a = _make_card("junnoMac-mini", "qwen2.5-coder-14b", free_gb=20)
    peer_b = _make_card("raspi4", "qwen2.5-coder-7b", free_gb=8)
    return {
        "self": self_card,
        "peers": [
            {"card": peer_a, "address": "127.0.0.1", "port": 47121, "via": "mdns", "last_seen": 0},
            {"card": peer_b, "address": "127.0.0.1", "port": 47122, "via": "mdns", "last_seen": 0},
        ],
    }


def test_collaborate_dispatches_exactly_two_files_when_architect_uses_nested_format():
    """`_parse_module_breakdown`単体のテストだけでなく、
    `_ask_organization_collaborate`を通しで実行し、設計担当が階層的な
    回答(ファイル名見出し+ぶら下がる関数シグネチャ)を返した場合でも、
    実際に問い合わせが飛ぶのはファイル単位(2件)に留まり、関数シグネチャの
    行が誤って実装依頼の宛先にされないことを確認する
    (実機では、9項目に誤分割された結果、storage.pyへの依頼と
    「load_todos() -> list」という架空のファイル名への依頼の2件だけが
    実行され、cli.pyは一度も実装依頼されなかった)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    nested_breakdown_answer = (
        "storage.py: ToDoリストをJSONファイルで管理するモジュール\n"
        "- load_todos() -> list: todos.jsonから読み込んで返す\n"
        "- save_todos(todos: list) -> None: todos.jsonに保存する\n"
        "- add_todo(text: str) -> int: 新しいTodoを追加してIDを返す\n"
        "- complete_todo(id: int) -> bool: 指定IDを完了にする\n"
        "- delete_todo(id: int) -> bool: 指定IDを削除する\n"
        "\n"
        "cli.py: storageモジュールを呼び出すCLI\n"
        "- storage.add_todo, storage.load_todos, storage.complete_todo, storage.delete_todoを呼び出す\n"
        "- python cli.py add \"text\" のようなコマンド体系\n"
    )

    received_requests_log = []

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        label = candidate["label"]
        request_text = messages[0]["content"]
        received_requests_log.append((label, request_text))
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": nested_breakdown_answer}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _three_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_test_")
    try:
        yoriai._ask_organization_collaborate(
            47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir,
        )

        # 実装依頼(【あなたが実装を担当するファイル】を含むプロンプト)の
        # 宛先(ラベル)の集合を確認する。設計担当への相談やレビューフェーズの
        # 問い合わせと区別するため、実装依頼に特有の文言で絞り込む。
        implementation_labels = [
            label for label, req in received_requests_log
            if "あなたが実装を担当するファイル" in req
        ]
        assert len(implementation_labels) == 2, (
            f"ファイル数(2件)ちょうどに実装依頼が飛ぶはずです: {received_requests_log}"
        )
        # 生成物はYoriai本体と混ざらないよう projects/<プロジェクト名>/ 配下に
        # 保存される(プロジェクト名は依頼文から自動生成されるため、実際の
        # 生成ロジックを呼んで期待値を求める)。
        project_dir = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._generate_project_name("ToDoリストのCLIツールを作って"))
        assert set(os.listdir(project_dir)) == {"storage.py", "cli.py", "PROGRESS.md"}, (
            f"storage.py/cli.py/PROGRESS.mdの3件だけが保存されるはずです: {os.listdir(project_dir)}"
        )
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_agree_uses_top_candidate_as_architect_and_propagates_interface_to_implementers():
    """//agree が、(1)最優先候補に設計を相談し、(2)その設計担当の回答に
    含まれるインターフェースの記述が、そのまま実装担当への依頼文に
    含まれて渡ることを確認する(実験1の関数名不一致を防ぐための狙い)。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    breakdown_answer = (
        "storage.py: add_todo(text: str) -> int, list_todos() -> list を実装。"
        "todoは{'id': int, 'text': str, 'done': bool}の辞書。\n"
        "cli.py: storage.add_todo/storage.list_todosを呼び出すCLI。\n"
    )

    # 仮の判断: 設計担当(候補の先頭)には「相談」と「storage.py実装」の
    # 2回問い合わせが飛ぶため、ラベルだけをキーにした辞書では1回目の内容が
    # 上書きされてしまう。時系列で追えるよう、(ラベル, 依頼文)のリストに
    # 記録する。
    received_requests_log = []

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        label = candidate["label"]
        request_text = messages[0]["content"]
        received_requests_log.append((label, request_text))
        if "ファイルに分割する実装計画" in request_text:
            # 設計担当への相談(合意フェーズ)
            yield {"content": breakdown_answer}
            yield {"done": True}
        elif label == "MacStudio(自分)":
            # storage.py の実装依頼(先頭タスク→最優先候補=MacStudio自身に割り当たる)
            yield {"content": "```python\ndef add_todo(text):\n    pass\n```"}
            yield {"done": True}
        elif label == "junnoMac-mini":
            # cli.py の実装依頼(2番目のタスク→2番目の優先候補)
            yield {"content": "```python\nimport storage\nstorage.add_todo('x')\nstorage.list_todos()\n```"}
            yield {"done": True}
        else:
            yield {"content": "```python\npass\n```"}
            yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _three_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_test_")
    try:
        yoriai._ask_organization_collaborate(
            47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir,
        )

        # 設計担当(最優先候補=MacStudio自身)に相談が飛んでいること
        architect_calls = [req for label, req in received_requests_log if "ファイルに分割する実装計画" in req]
        assert len(architect_calls) == 1, (
            f"設計担当への相談は1回だけのはずです: {received_requests_log}"
        )
        assert "ToDoリストのCLIツールを作って" in architect_calls[0], (
            "元の依頼文が設計担当への相談プロンプトに含まれていません"
        )

        # raspi4(3番目の候補)にはタスクが2件しか無いため問い合わせが飛ばないこと
        assert not any(label == "raspi4" for label, _req in received_requests_log), (
            f"タスクが2件しかないのに3台目まで問い合わせています: {received_requests_log}"
        )

        # cli.py担当(junnoMac-mini)への実装依頼文に、設計担当が決めた
        # インターフェース(add_todo/list_todosという具体的な関数名)が
        # そのまま引き継がれていることを確認する(実験1の関数名不一致の再発防止)。
        # レビューフェーズでもjunnoMac-miniへの問い合わせ(storage.pyのレビュー)が
        # 発生するため、実装依頼特有の文言で絞り込む。
        cli_requests = [
            req for label, req in received_requests_log
            if label == "junnoMac-mini" and "あなたが実装を担当するファイル" in req
        ]
        assert len(cli_requests) == 1, received_requests_log
        assert "add_todo" in cli_requests[0] and "list_todos" in cli_requests[0], (
            f"設計担当が決めたインターフェースが実装依頼に引き継がれていません: {cli_requests[0]!r}"
        )

        project_dir = os.path.join(out_dir, yoriai.PROJECTS_SUBDIR_NAME, yoriai._generate_project_name("ToDoリストのCLIツールを作って"))
        with open(os.path.join(project_dir, "cli.py"), encoding="utf-8") as f:
            saved_cli = f.read()
        assert "import storage" in saved_cli

        with open(os.path.join(project_dir, "storage.py"), encoding="utf-8") as f:
            saved_storage = f.read()
        assert "def add_todo" in saved_storage
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_agree_aborts_cleanly_when_architect_response_is_unparseable():
    """設計担当の回答が期待した「ファイル名: 内容」形式で1件も得られない
    場合、実装依頼を送らずに中断することを確認する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    call_count = {"n": 0}

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        call_count["n"] += 1
        yield {"content": "すみません、うまく分割案を作れませんでした。"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _three_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_test_")
    try:
        yoriai._ask_organization_collaborate(47120, "fingerprint", "何か作って", out_dir)
        assert call_count["n"] == 1, (
            f"設計担当への相談は1回だけのはずですが、実装依頼まで進んでしまいました(呼び出し回数: {call_count['n']})"
        )
        assert os.listdir(out_dir) == [], "解析失敗時はファイルを保存しないはずです"
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def test_implementer_receives_full_plan_even_when_architect_output_is_self_inconsistent():
    """実機で実際に報告された不具合(設計担当の回答内で、cli.py行は
    add_task/remove_task/update_task/list_tasksという名前、storage.py行は
    save_todos/load_todosという全く別の名前で書かれていた)を再現し、
    このような「設計担当自身の回答が最初から矛盾している」ケースでも、
    各実装担当への依頼文には実装計画全体(=矛盾する両方の記述)が
    そのまま埋め込まれることを確認する。

    これは矛盾そのものを自動解消する仕組みではないが、(1)実装担当が
    他ファイルの想定インターフェースに実装時点で気づける可能性を高める、
    (2)対話モードの標準出力に生の回答をそのまま表示するため、
    ユーザーが「設計担当の時点で既に矛盾していた」と判別できるようにする、
    という2点を検証する。
    """
    original_snapshot = yoriai._fetch_org_snapshot
    original_stream = yoriai._stream_chat_from_candidate

    # 実機で報告された、設計担当の回答自体が既に矛盾しているケースを再現する。
    inconsistent_breakdown_answer = (
        "storage.py: save_todos(todos, filepath) と load_todos(filepath) を実装する\n"
        "cli.py: storageから add_task, remove_task, update_task, list_tasks をimportして使う\n"
    )

    received_requests_log = []

    def fake_stream(candidate, org_fingerprint, messages, **_kwargs):
        label = candidate["label"]
        request_text = messages[0]["content"]
        received_requests_log.append((label, request_text))
        if "ファイルに分割する実装計画" in request_text:
            yield {"content": inconsistent_breakdown_answer}
        else:
            yield {"content": "```python\npass\n```"}
        yield {"done": True}

    yoriai._fetch_org_snapshot = lambda port, fp, fail_fast=False: _three_member_snapshot()
    yoriai._stream_chat_from_candidate = fake_stream

    out_dir = tempfile.mkdtemp(prefix="yoriai_agree_test_")
    try:
        yoriai._ask_organization_collaborate(47120, "fingerprint", "ToDoリストのCLIツールを作って", out_dir)

        # 仮の判断: タスクキュー方式では、cli.pyの実装担当がどのメンバーに
        # なるかはワーカースレッドの実行順序に左右され、必ずしも
        # junnoMac-miniになるとは限らない(候補が3台・タスクが2件のため、
        # 手が空いたメンバーが両方のタスクを処理することもありうる)。
        # そのため、担当者のラベルではなく「あなたが実装を担当するファイル」
        # セクションでcli.pyが指定されている実装依頼、という内容そのもので
        # 絞り込む(レビューフェーズの問い合わせはこの文言を含まないため、
        # 混同しない)。
        cli_requests = [
            req for _label, req in received_requests_log
            if "あなたが実装を担当するファイル】\ncli.py:" in req
        ]
        assert len(cli_requests) == 1, received_requests_log
        cli_request = cli_requests[0]

        # 修正前は「cli.py行の内容だけ」しか実装担当に渡らなかったため、
        # storage.py側の実際の関数名(save_todos/load_todos)を実装担当が
        # 目にする機会が無かった。修正後は計画全体が埋め込まれるため、
        # 矛盾していてもstorage.py側の名前がcli.py実装依頼に含まれるはず。
        assert "save_todos" in cli_request and "load_todos" in cli_request, (
            f"storage.py側の関数名が実装計画全体としてcli.py担当への依頼に含まれていません: {cli_request!r}"
        )
        # cli.py行自身の内容も引き続き含まれること
        assert "add_task" in cli_request, cli_request
    finally:
        yoriai._fetch_org_snapshot = original_snapshot
        yoriai._stream_chat_from_candidate = original_stream
        shutil.rmtree(out_dir, ignore_errors=True)


def main():
    tests = [
        test_execution_mode_classification_matches_requested_examples,
        test_parse_module_breakdown_handles_bullets_and_code_fence,
        test_parse_module_breakdown_groups_nested_function_signatures_under_their_file,
        test_parse_module_breakdown_is_not_specific_to_the_todo_example,
        test_parse_module_breakdown_still_handles_flat_single_line_format,
        test_collaborate_dispatches_exactly_two_files_when_architect_uses_nested_format,
        test_agree_uses_top_candidate_as_architect_and_propagates_interface_to_implementers,
        test_agree_aborts_cleanly_when_architect_response_is_unparseable,
        test_implementer_receives_full_plan_even_when_architect_output_is_self_inconsistent,
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
