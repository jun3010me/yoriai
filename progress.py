"""Yoriaiのタスクチェックリスト・PROGRESS.md永続化層。

仮の判断(モジュール分割第四弾への対応): これまで`yoriai.py`単一ファイルに
実装されていた、組織自身が立てた計画を最後まで完遂させるためのタスク
チェックリスト(`_build_task_checklist`等)と、その進行状況をプロジェクト
ディレクトリのPROGRESS.mdへ永続化する層(`_write_progress_md`・
`_parse_progress_markdown`等)を、依存が少なく独立性の高い塊としてここに
切り出した。関数のロジックは`yoriai.py`から一切変更していない
(コピー＆import配線の変更のみ)。

ここに定義された関数は、タスクキュー方式・`//resume-all`・`//fix`等の
まだ`yoriai.py`側に残っているオーケストレーション層から広く呼ばれるため、
`yoriai.py`側で`from progress import ...`して使う。逆に
`_parse_progress_markdown`は、まだ`yoriai.py`側に残っている
`_parse_module_breakdown`(対話プロトコル・合意フェーズ側のパーサー)を
呼ぶ必要があるが、`yoriai.py`が本モジュールをimportしているため、
モジュール読み込み時点でのトップレベルimportは循環importになって
しまう。そのため、実際に呼ばれる関数の内部で遅延import(呼び出し時には
両モジュールとも読み込みが完了しているため問題なく解決できる)している。
"""

import os
import re

from yoriai_types import PROGRESS_FILENAME, PROJECTS_SUBDIR_NAME

# ---------------------------------------------------------------------------
# 組織自身が立てた計画を最後まで完遂させるためのタスクチェックリスト
# ---------------------------------------------------------------------------
#
# 実機で、合意フェーズが「4ファイル構成が適切」と自ら提案したにもかかわらず、
# 実装フェーズで候補不足等によりconfig.pyがスキップされたまま
# 「✅ レビュー完了」と表示されてしまう不具合が見つかった。合意フェーズの
# 構成案は人間が直接指定したものではなく組織自身(設計担当)が生み出した
# 計画だが、「組織側が4つ必要だと判断した以上、その4つを完遂する責任は
# 組織側にある」という前提のもと、合意フェーズが確定した瞬間にその計画を
# 「必ず完遂すべきタスクリスト」として固定し、Claude CodeのTodoWriteと
# 同様に進行状況を追跡したうえで、最後に必ず全タスクの完了を確認してから
# でなければ「完了」を報告しない仕組みを追加した。

_TASK_STATUS_PENDING = "pending"
_TASK_STATUS_IN_PROGRESS = "in_progress"
_TASK_STATUS_COMPLETED = "completed"

_TASK_STATUS_SYMBOLS = {
    _TASK_STATUS_PENDING: "⬜",
    _TASK_STATUS_IN_PROGRESS: "⏳",
    _TASK_STATUS_COMPLETED: "✅",
}


def _build_task_checklist(tasks: list) -> list:
    """合意フェーズで確定したファイル一覧(`tasks`: `[(ファイル名, 内容), ...]`)
    から、ファイルごとに「実装」「レビュー」の2タスクを持つチェックリストを
    作る。各タスクは`{"filename":, "kind": "impl"|"review", "label":, "status":}`
    の辞書で、生成した時点ではすべて`_TASK_STATUS_PENDING`。
    """
    checklist = []
    for filename, _content in tasks:
        checklist.append({"filename": filename, "kind": "impl", "label": f"{filename} の実装", "status": _TASK_STATUS_PENDING})
        checklist.append({"filename": filename, "kind": "review", "label": f"{filename} のレビュー", "status": _TASK_STATUS_PENDING})
    return checklist


def _set_task_status(checklist: list, filename: str, kind: str, status: str) -> None:
    for task in checklist:
        if task["filename"] == filename and task["kind"] == kind:
            task["status"] = status
            return


def _format_checklist_lines(checklist: list) -> list:
    return [f"{_TASK_STATUS_SYMBOLS[task['status']]} {task['label']}" for task in checklist]


def _format_task_checklist(checklist: list) -> str:
    return "\n".join(["[📝 タスク状況(組織が自ら立てた計画)]"] + _format_checklist_lines(checklist))


def _incomplete_task_labels(checklist: list) -> list:
    return [task["label"] for task in checklist if task["status"] != _TASK_STATUS_COMPLETED]


# ---------------------------------------------------------------------------
# 進行状況の永続化(PROGRESS.md)
# ---------------------------------------------------------------------------
#
# 協業モードは合意フェーズ→タスクキュー方式による実装・レビューと複数の
# フェーズにまたがり、実機では数分単位の時間がかかることもある。途中で
# プロセスが終了した場合(--chatを再起動した、非常口で強制終了した等)、
# それまでの進行状況(元の依頼・確定した計画・タスクの状態・直近の
# レビュー指摘)が失われ、最初からやり直すしかなかった。read_fileツール
# (実装依頼元がディスク上のファイルを都度読み直す設計)と同じ「ディスク上の
# 実際の状態を正として扱う」方針に基づき、プロジェクトディレクトリに
# PROGRESS.mdを作成・更新し続けることで、後から(`//resume-all`等で)
# 未完了タスクだけを対象に再開できるようにする。tools.py側の
# `_resolve_safe_project_path`・`_delete_project_file`からも参照されるため
# `yoriai_types.py`でPROGRESS_FILENAMEを定義している(ファイル冒頭のimportを参照)。

_PROGRESS_SECTION_REQUEST = "## 元の依頼"
_PROGRESS_SECTION_LANGUAGE = "## 使用言語"
_PROGRESS_SECTION_PLAN = "## モジュール分割案"
# 仮の判断(統合検証ループへの対応): 合意フェーズが決めた検証コマンドを
# 「モジュール分割案」と同じ節にまとめず独立の節にする。検証コマンドは
# ファイル単位の情報ではなくプロジェクト全体に対する1つの値のため、
# `_parse_module_breakdown`(ファイル単位のリストを組み立てるパーサー)に
# 混ぜて解析させるより、`_extract_progress_section`で単純に1行読み出す
# 方が素直だと判断した。
_PROGRESS_SECTION_VERIFY_COMMAND = "## 検証コマンド"
_PROGRESS_SECTION_CHECKLIST = "## タスク状況"
_PROGRESS_SECTION_AUTO_RESUME_COUNT = "## 自動再開の試行回数"
# 仮の判断: 統合検証の結果(成功/失敗・試行回数・失敗時の出力冒頭)も、
# 「自動再開の試行回数」と同じくPROGRESS.md自身に直接記録する(別ファイルは
# 持たせない)。`//resume-all`が「実装は完了しているが統合検証だけ失敗した
# ままのプロジェクト」を検出する(`_project_has_pending_work`)ために、
# ディスク上の状態を正として扱う既存方針をそのまま踏襲する。
_PROGRESS_SECTION_VERIFICATION = "## 統合検証"
_PROGRESS_SECTION_REVIEW = "## 直近のレビュー指摘"
_PROGRESS_SECTION_CHANGELOG = "## 更新履歴"
# 仮の判断(バグ報告への対応): //fixのタスク分割(_run_fix_task_queue)が
# ツール呼び出しの往復回数の上限等で一部のサブタスクを完了させられずに
# 終わった場合、この2節に「元の修正依頼」と「まだ完了していないサブ
# タスクの一覧」を記録する。これが無いと、//resume-allが従来通り
# モジュール分割案のチェックリスト(_PROGRESS_SECTION_CHECKLIST、合意
# フェーズ由来で//fixでは書き換えない)しか見ないため、大規模な//fixが
# 一部のサブタスクを残したまま終わっても「未完了のプロジェクトなし」
# として扱われ、再開する手段が無くなってしまう。
_PROGRESS_SECTION_PENDING_FIX_REQUEST = "## 未完了の修正依頼"
_PROGRESS_SECTION_PENDING_FIX_SUBTASKS = "## 未完了の修正サブタスク"


_VERIFICATION_OUTPUT_RECORD_CHARS = 500


def _format_verification_result(verification: dict) -> list:
    """統合検証の結果(`{"success": bool, "attempts": int, "output": str}`)を、
    PROGRESS.mdの「統合検証」節の本文行のリストにする。失敗時のみ、
    最後の出力の冒頭(`_VERIFICATION_OUTPUT_RECORD_CHARS`文字まで)を
    続けて記録する(成功時は出力を残す実益が薄いため省く)。
    """
    if verification["success"]:
        return [f"✅ 成功 ({verification['attempts']}回目の試行で成功)"]
    lines = [f"❌ 失敗 ({verification['attempts']}回試行)"]
    output = (verification.get("output") or "").strip()
    if output:
        lines.append("")
        lines.append(output[:_VERIFICATION_OUTPUT_RECORD_CHARS])
    return lines


_VERIFICATION_SUCCESS_PATTERN = re.compile(r"^✅ 成功 \((\d+)回目の試行で成功\)$")
_VERIFICATION_FAILURE_PATTERN = re.compile(r"^❌ 失敗 \((\d+)回試行\)$")


def _parse_verification_result(text: str):
    """`_format_verification_result`の逆変換。節が無い・想定した形式で
    解析できない場合は`None`を返す(「まだ統合検証を実行していない」
    ことを表す。旧バージョンのPROGRESS.mdとの後方互換にもなる)。
    """
    lines = text.splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    success_match = _VERIFICATION_SUCCESS_PATTERN.match(first)
    if success_match:
        return {"success": True, "attempts": int(success_match.group(1)), "output": ""}
    failure_match = _VERIFICATION_FAILURE_PATTERN.match(first)
    if failure_match:
        output = "\n".join(lines[1:]).strip("\n")
        return {"success": False, "attempts": int(failure_match.group(1)), "output": output}
    return None


def _format_progress_markdown(
    request: str, tasks: list, checklist: list, review_feedback: dict, auto_resume_count: int = 0,
    changelog: list = None, language: str = "",
    pending_fix_request: str = "", pending_fix_subtasks: list = None,
    verify_command: str = "", verification: dict = None,
) -> str:
    """PROGRESS.mdの内容を組み立てる。

    仮の判断: 「モジュール分割案」セクションは、既存の設計担当への
    問い合わせ結果を表示する際と同じ`f"{filename}: {content}"`という
    フラットな1行1ファイル形式で書き出す。この形式は既存の
    `_parse_module_breakdown`がそのまま解析できるため、再開時
    (`_parse_progress_markdown`)に新しいパーサーを書き起こす必要がなく、
    書き込み・読み込みの両方で実績のある同じコードを再利用できる。

    仮の判断: 「自動再開の試行回数」も、この試行回数だけを内容とする
    シンプルなセクションとしてPROGRESS.mdに直接記録する(別ファイルや
    JSON等の付随データファイルは持たせない)。read_fileツール・
    PROGRESS.md全体で踏襲している「ディスク上の実際の状態を正として
    扱う」方針により、対話モードのプロセスを再起動しても試行回数の
    記録が失われないようにするため。

    仮の判断: 「更新履歴」(既存プロジェクトへの修正依頼の記録)も、
    `"- YYYY-MM-DD: <内容> (<ファイル名>)"`という行の単純なリストとして
    記録する。新しいエントリは末尾に追記されるだけで、既存のエントリを
    書き換えることはない(`_ask_organization_fix_project`参照)。

    仮の判断: 「使用言語」は、合意フェーズが確定したモジュール分割案の
    ファイル拡張子から導出した値(`_infer_language_from_tasks`)を渡す
    (依頼側で明示された言語と、実際に生成されたファイルが食い違う
    リスクを避けるため、常に「実際のファイル」を正とする)。空文字列
    (旧バージョンのPROGRESS.mdや、この節を省きたい呼び出し元)の場合は
    この節自体を出力しない(既存の「直近のレビュー指摘」等と同じ、
    値が無ければ節ごと省略する方針)。

    仮の判断: 「検証コマンド」「統合検証」も同じく、値が無ければ節ごと
    省略する(`verify_command`が空文字列、または`verification`が`None`
    (まだ統合検証を実行していない)の場合)。
    """
    lines = ["# プロジェクト進行状況", "", _PROGRESS_SECTION_REQUEST, "", request, ""]
    if language:
        lines.append(_PROGRESS_SECTION_LANGUAGE)
        lines.append("")
        lines.append(language)
        lines.append("")
    lines.append(_PROGRESS_SECTION_PLAN)
    lines.append("")
    for filename, content in tasks:
        lines.append(f"{filename}: {content}")
    lines.append("")
    if verify_command:
        lines.append(_PROGRESS_SECTION_VERIFY_COMMAND)
        lines.append("")
        lines.append(verify_command)
        lines.append("")
    lines.append(_PROGRESS_SECTION_CHECKLIST)
    lines.append("")
    lines.extend(_format_checklist_lines(checklist))
    lines.append("")
    lines.append(_PROGRESS_SECTION_AUTO_RESUME_COUNT)
    lines.append("")
    lines.append(str(auto_resume_count))
    lines.append("")
    if verification is not None:
        lines.append(_PROGRESS_SECTION_VERIFICATION)
        lines.append("")
        lines.extend(_format_verification_result(verification))
        lines.append("")
    if review_feedback:
        lines.append(_PROGRESS_SECTION_REVIEW)
        lines.append("")
        for filename, feedback in review_feedback.items():
            lines.append(f"### {filename}")
            lines.append("")
            lines.append(feedback)
            lines.append("")
    if changelog:
        lines.append(_PROGRESS_SECTION_CHANGELOG)
        lines.append("")
        lines.extend(changelog)
        lines.append("")
    if pending_fix_subtasks:
        lines.append(_PROGRESS_SECTION_PENDING_FIX_REQUEST)
        lines.append("")
        lines.append(pending_fix_request)
        lines.append("")
        lines.append(_PROGRESS_SECTION_PENDING_FIX_SUBTASKS)
        lines.append("")
        lines.extend(f"- {subtask}" for subtask in pending_fix_subtasks)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _write_progress_md(
    project_dir: str, request: str, tasks: list, checklist: list, review_feedback: dict, auto_resume_count: int = 0,
    changelog: list = None, language: str = "",
    pending_fix_request: str = "", pending_fix_subtasks: list = None,
    verify_command: str = "", verification: dict = None,
) -> None:
    os.makedirs(project_dir, exist_ok=True)
    content = _format_progress_markdown(
        request, tasks, checklist, review_feedback, auto_resume_count, changelog, language,
        pending_fix_request, pending_fix_subtasks, verify_command, verification,
    )
    with open(os.path.join(project_dir, PROGRESS_FILENAME), "w", encoding="utf-8") as f:
        f.write(content)


def _extract_progress_section(text: str, heading: str) -> str:
    """`text`(PROGRESS.md全体)から、`heading`(例: "## モジュール分割案")の
    直後から次の"## "見出し(または末尾)までの本文を取り出す。見出しが
    見つからない場合は空文字列を返す。
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == heading:
            start = i + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[start:end]).strip("\n")


# 仮の判断: チェックリストの各行のラベルは`_build_task_checklist`が
# `f"{filename} の実装"`/`f"{filename} のレビュー"`という固定の形式で
# 生成しており、PROGRESS.mdにもこの形式のまま書き出される。再開時は、
# この形式を逆算してfilename/kindを復元する(新しい機械可読フォーマットを
# 別途持たせるのではなく、人間にも読めるこの文言をそのまま構造化データの
# 源として再利用する)。
_CHECKLIST_LABEL_PATTERN = re.compile(r"^(.+) の(実装|レビュー)$")
_CHECKLIST_KIND_FROM_JA = {"実装": "impl", "レビュー": "review"}
_CHECKLIST_STATUS_FROM_SYMBOL = {v: k for k, v in _TASK_STATUS_SYMBOLS.items()}


def _parse_checklist_markdown(text: str) -> list:
    checklist = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        symbol, sep, rest = line.partition(" ")
        if not sep:
            continue
        status = _CHECKLIST_STATUS_FROM_SYMBOL.get(symbol)
        if status is None:
            continue
        match = _CHECKLIST_LABEL_PATTERN.match(rest)
        if not match:
            continue
        filename, kind_ja = match.groups()
        checklist.append({
            "filename": filename, "kind": _CHECKLIST_KIND_FROM_JA[kind_ja],
            "label": rest, "status": status,
        })
    return checklist


def _parse_review_feedback_markdown(text: str) -> dict:
    """PROGRESS.md全体(`text`)から「## 直近のレビュー指摘」セクション
    (`_PROGRESS_SECTION_REVIEW`)を取り出し、`_format_progress_markdown`が
    `f"### {filename}"`という見出しで区切って書き出した内容を
    `{filename: feedback}`の辞書として読み戻す。セクション自体が
    無い(旧バージョンのPROGRESS.md・未完了ファイルが1つも無いプロジェクト)
    場合は空の辞書を返す。

    仮の判断: `_extract_progress_section`と同じ「見出し行から次の見出し
    行(またはセクション末尾)までを本文として取り出す」ロジックを、
    "## "見出しではなく"### "見出しの粒度でこの関数内に書き起こす
    (`_extract_progress_section`自体は"## "しか見ないため、そのまま
    再利用はできない)。
    """
    section = _extract_progress_section(text, _PROGRESS_SECTION_REVIEW)
    if not section:
        return {}
    review_feedback = {}
    filename = None
    body_lines = []
    for line in section.splitlines():
        if line.startswith("### "):
            if filename is not None:
                review_feedback[filename] = "\n".join(body_lines).strip("\n")
            filename = line[len("### "):].strip()
            body_lines = []
        else:
            body_lines.append(line)
    if filename is not None:
        review_feedback[filename] = "\n".join(body_lines).strip("\n")
    return review_feedback


def _find_repeated_review_feedback(previous: dict, current: dict, checklist: list) -> list:
    """直近の自動再開の前後(`previous`→`current`、いずれも
    `{filename: feedback}`の辞書)を比較し、`checklist`上でまだ完了して
    いないファイルのうち、レビュー指摘の文言が(前後の空白を除いて)
    一字一句変化していないファイル名の一覧をファイル名でソートして返す。

    仮の判断: `previous`・`current`のどちらか一方にしか該当ファイルの
    記録が無い場合は「変化なし」とはみなさない。初回の自動再開など、
    比較対象がそもそも存在しないケースまで「変化していない」と誤検知
    してしまうと、1回もレビュー指摘を受け取っていないファイルまで
    早期打ち切りの対象になりかねないため。
    """
    incomplete_filenames = {
        task["filename"] for task in checklist if task["status"] != _TASK_STATUS_COMPLETED
    }
    repeated = []
    for filename in incomplete_filenames:
        if filename not in previous or filename not in current:
            continue
        if previous[filename].strip() == current[filename].strip():
            repeated.append(filename)
    return sorted(repeated)


def _parse_auto_resume_count(text: str) -> int:
    """「自動再開の試行回数」セクションの内容を整数として読み取る。
    セクションが無い(旧バージョンのPROGRESS.md)・内容が数値として
    解釈できない場合は、まだ1回も自動再開を試みていないとみなして
    `0`を返す。
    """
    section = _extract_progress_section(text, _PROGRESS_SECTION_AUTO_RESUME_COUNT).strip()
    try:
        return int(section)
    except ValueError:
        return 0


def _parse_changelog_markdown(text: str) -> list:
    return [line for line in text.splitlines() if line.strip()]


def _parse_bullet_lines(text: str) -> list:
    """箇条書き(-・*)の各行の内容だけを、件数による足切りをせず
    そのまま取り出す。//fixのタスク分割の判定担当への問い合わせ結果を
    解釈する`_parse_fix_split_subtasks`(2件未満は「分割不要」とみなし
    空リストに切り捨てる)とは異なり、こちらはPROGRESS.mdに自分自身が
    書き出した「未完了の修正サブタスク」を読み戻すための単純な抽出用
    途で、1件しか無くても正しく読み戻せる必要がある。
    """
    items = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line and line[0] in "-・*":
            content = line.lstrip("-・*").strip()
            if content:
                items.append(content)
    return items


def _parse_progress_markdown(path: str):
    """PROGRESS.mdを読み込み、`{"request":, "language":, "tasks":,
    "checklist":, "auto_resume_count":, "changelog":, "pending_fix_request":,
    "pending_fix_subtasks":, "verify_command":, "verification":,
    "review_feedback":}`の辞書として返す。ファイルが存在しない・想定した
    形式で解析できない場合は`None`を返す(呼び出し元は、そのプロジェクトの
    再開をスキップすべきというシグナルとして扱う)。

    仮の判断(繰り返しレビュー指摘の早期検知への対応): `review_feedback`
    (この節が無ければ空の辞書)も同じ後方互換の方針。従来この節は
    `_format_progress_markdown`による書き込み専用で、`_maybe_auto_resume`が
    「直近の自動再開の前後でレビュー指摘の文言が一字一句変化していないか」
    を比較する必要が出てきたため、読み戻しに対応した。

    仮の判断: `language`は、この節が無い旧バージョンのPROGRESS.mdでは
    空文字列になる(この機能追加より前に作られた既存プロジェクトを
    エラー扱いにしない後方互換のため)。空文字列の場合、構文チェックは
    ファイルの拡張子で判定するため実害は無く、`_ask_organization_
    fix_project`のプロンプトでは「不明」として扱う。

    仮の判断: `pending_fix_subtasks`も同じ理由でこの節が無いPROGRESS.md
    では空リストになる(//fixのタスク分割機能より前に作られたプロジェクト・
    分割が発生したことのないプロジェクトのどちらも、単に「未完了の
    修正サブタスクは無い」として扱われる)。

    仮の判断: `verify_command`(この節が無ければ空文字列)・`verification`
    (この節が無い、または統合検証がまだ実行されていなければ`None`)も
    同じ後方互換の方針。統合検証機能より前に作られたプロジェクトは、
    `//resume-all`で再開しても単に統合検証がスキップされるだけで、
    エラー扱いにはならない。
    """
    from yoriai import _parse_module_breakdown
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None

    request = _extract_progress_section(text, _PROGRESS_SECTION_REQUEST).strip()
    tasks = _parse_module_breakdown(_extract_progress_section(text, _PROGRESS_SECTION_PLAN))
    checklist = _parse_checklist_markdown(_extract_progress_section(text, _PROGRESS_SECTION_CHECKLIST))
    if not tasks or not checklist:
        return None
    return {
        "request": request, "tasks": tasks, "checklist": checklist,
        "language": _extract_progress_section(text, _PROGRESS_SECTION_LANGUAGE).strip(),
        "auto_resume_count": _parse_auto_resume_count(text),
        "changelog": _parse_changelog_markdown(_extract_progress_section(text, _PROGRESS_SECTION_CHANGELOG)),
        "pending_fix_request": _extract_progress_section(text, _PROGRESS_SECTION_PENDING_FIX_REQUEST).strip(),
        "pending_fix_subtasks": _parse_bullet_lines(_extract_progress_section(text, _PROGRESS_SECTION_PENDING_FIX_SUBTASKS)),
        "verify_command": _extract_progress_section(text, _PROGRESS_SECTION_VERIFY_COMMAND).strip(),
        "verification": _parse_verification_result(_extract_progress_section(text, _PROGRESS_SECTION_VERIFICATION)),
        "review_feedback": _parse_review_feedback_markdown(text),
    }


def _progress_checklist_is_incomplete(checklist: list) -> bool:
    return any(task["status"] != _TASK_STATUS_COMPLETED for task in checklist)


def _project_has_pending_work(parsed: dict) -> bool:
    """`_parse_progress_markdown`の解析結果から、このプロジェクトに
    再開すべき作業が残っているかどうかを判定する。従来の(合意フェーズ
    由来の)モジュールタスクのチェックリストの未完了に加え、//fixの
    タスク分割(`_run_fix_task_queue`)が往復回数の上限等で完了させ
    られなかった「未完了の修正サブタスク」(`pending_fix_subtasks`)も
    合わせて確認する。

    仮の判断(バグ報告への対応): 大規模な//fixが一部のサブタスクを
    残したまま終わった場合、モジュールタスクのチェックリスト自体は
    (//fixでは書き換えないため)完了したままになっている。この関数を
    使わず`_progress_checklist_is_incomplete`だけで判定していた箇所
    (`_find_incomplete_projects`等)では、この未完了状態が一切見えず、
    `//resume-all`から再開する手段が無くなってしまっていた。

    仮の判断(統合検証ループへの対応): 全タスクのチェックリストが完了
    していても、統合検証(`verification`)が記録されていて、その結果が
    失敗のままの場合も「再開すべき作業が残っている」とみなす。統合検証は
    ファイル単位のチェックリストとは独立した結果のため、これが無いと
    「実装は完了しているが統合検証に失敗したまま放置されたプロジェクト」
    が`//resume-all`から永久に拾えなくなってしまう。統合検証が一度も
    実行されていない(`verification`が`None`。検証コマンドが「なし」
    だった場合を含む)場合は、それ自体は「失敗」ではないため対象外とする。
    """
    verification = parsed.get("verification")
    verification_failed = bool(verification) and not verification.get("success", True)
    return (
        _progress_checklist_is_incomplete(parsed["checklist"])
        or bool(parsed.get("pending_fix_subtasks"))
        or verification_failed
    )


def _pending_tasks_from_checklist(tasks: list, checklist: list) -> list:
    """`tasks`(全ファイル)のうち、チェックリスト上いずれかのタスク
    (実装・レビュー)が未完了のファイルだけを返す。

    仮の判断: 再開の粒度はファイル単位とする。実装は完了しているが
    レビューだけが未完了のファイルも、既存のタスクキュー方式
    (`_run_collaborative_task_queue`)にそのまま乗せて実装からやり直す
    (「レビューだけを再開する」という専用の経路は用意しない)。実装
    済みのコードが再度実装し直されるのは無駄ではあるが、既存の
    タスクキュー方式・レビューフェーズを変更せずにそのまま再利用できる
    (依頼の「既存のタスクキュー方式・レビューフェーズをそのまま使い」
    という要件に沿う)。
    """
    incomplete_filenames = {
        task["filename"] for task in checklist if task["status"] != _TASK_STATUS_COMPLETED
    }
    return [(filename, content) for filename, content in tasks if filename in incomplete_filenames]


def _find_incomplete_projects(out_dir: str) -> list:
    """`<out_dir>/projects/`配下で、PROGRESS.mdのチェックリストに未完了の
    タスクが残っているプロジェクトディレクトリの一覧を返す(ディスク上の
    実際の状態を正として扱う。呼び出し側で状態をキャッシュしない)。
    """
    projects_root = os.path.join(out_dir, PROJECTS_SUBDIR_NAME)
    if not os.path.isdir(projects_root):
        return []
    incomplete = []
    for name in sorted(os.listdir(projects_root)):
        project_dir = os.path.join(projects_root, name)
        parsed = _parse_progress_markdown(os.path.join(project_dir, PROGRESS_FILENAME))
        if parsed is not None and _project_has_pending_work(parsed):
            incomplete.append(project_dir)
    return incomplete
