#!/bin/bash
# Yoriaiの初回セットアップ(venv作成・依存関係インストール・トークン設定・
# 常駐化の案内)をまとめて行うスクリプト。
#
# 使い方: ./scripts/setup.sh
# (リポジトリのルートにある install.sh 経由で `curl | bash` 一発インストールの
#  一部としても呼び出される。詳細はREADMEの「セットアップ」を参照)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

# 仮の判断: `curl | bash` 経由で実行されると標準入力はスクリプト自体を読み込む
# パイプに使われてしまい、対話的な入力(read/Pythonのinput())がスクリプトの
# 残り部分を誤って"入力"として食べてしまう恐れがある。そのため対話的な入力は
# 常に明示的に/dev/ttyから読むようにし、TTYが無い(CI等の非対話環境や、
# 制御端末を持たないプロセスから実行された場合)は代わりに/dev/nullを使って
# その場で確実にEOFにする(スクリプト本体を汚染しない)。
# `-r /dev/tty` はパーミッションのチェックに過ぎず、実際にopenできるかどうか
# (制御端末が存在するか)は分からないため、実際に開いてみて確認する。
if { : < /dev/tty; } 2>/dev/null; then
    TTY_DEV=/dev/tty
else
    TTY_DEV=/dev/null
fi

echo "=== 1/5: Python仮想環境(.venv)を用意します ==="
if [[ ! -d "$REPO_DIR/.venv" ]]; then
    python3 -m venv "$REPO_DIR/.venv"
    echo ".venv を作成しました。"
else
    echo ".venv は既に存在します(スキップ)。"
fi
PYTHON_BIN="$REPO_DIR/.venv/bin/python3"
PIP_BIN="$REPO_DIR/.venv/bin/pip"

echo ""
echo "=== 2/5: 依存関係をインストールします ==="
"$PIP_BIN" install -q -r requirements.txt
echo "依存関係のインストールが完了しました。"

echo ""
echo "=== 3/5: トークン(組織の合言葉)を設定します ==="
if "$PYTHON_BIN" -c "import config, sys; sys.exit(0 if config.load_token() else 1)" 2>/dev/null; then
    echo "既にトークンが設定されています(スキップします)。"
else
    echo "1) 新しい組織を作る"
    echo "2) 既存の組織に参加する(トークンを持っている)"
    echo "3) 今はスキップする(あとで自分で設定する)"
    # 仮の判断: TTY_DEVが/dev/nullの場合、readは即座にEOFとなり非0で終了する
    # (set -eの対象になってしまう)。 `|| true` でその失敗を許容し、
    # 変数は空文字列のまま後続の分岐(該当なし=スキップ扱い)に委ねる。
    read -r -p "選んでください [1/2/3]: " CHOICE < "$TTY_DEV" || true
    case "$CHOICE" in
        1)
            "$PYTHON_BIN" yoriai.py --init < "$TTY_DEV"
            ;;
        2)
            read -r -p "参加するトークン(合言葉)を入力してください: " TOKEN < "$TTY_DEV" || true
            if [[ -z "$TOKEN" ]]; then
                echo "トークンが空だったため、スキップしました。あとで下記を実行してください。"
                echo "  python3 yoriai.py --join=<トークン>"
            else
                YORIAI_TOKEN="$TOKEN" "$PYTHON_BIN" -c "
import os
import config
config.save_token(os.environ['YORIAI_TOKEN'])
print(f'トークンを保存しました: {config.CONFIG_FILE}')
"
            fi
            ;;
        *)
            echo "スキップしました。あとで以下のいずれかを実行してください。"
            echo "  python3 yoriai.py --init"
            echo "  python3 yoriai.py --join=<トークン>"
            ;;
    esac
fi

echo ""
echo "=== 4/5: 言語別ツール(gcc・Node.js)の確認 ==="
# 仮の判断: gcc・nodeは、実機に無ければ機械的な構文チェック(gcc -fsyntax-only・
# node --check)がその場でスキップされるだけで、Yoriai自体は問題無く動く
# (詳細はREADMEの構文チェックの節を参照)。そのため、トークンのinit/joinと
# 同様に「無断でシステムにインストールしない・本人の意思を確認する」方針を
# 踏襲し、未インストールの場合のみ個別に確認する。既にインストール済みの
# 場合は質問すらせず状態表示のみに留める。

_has_gcc() { command -v gcc >/dev/null 2>&1 || command -v cc >/dev/null 2>&1; }
_has_node() { command -v node >/dev/null 2>&1; }

_ask_yes_no() {
    local prompt="$1" answer
    read -r -p "$prompt" answer < "$TTY_DEV" || true
    [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# 仮の判断: `set -e`下でもパッケージマネージャーのコマンド(ネットワーク
# 断・権限不足等で失敗し得る)がスクリプト全体を中断させないよう、
# 呼び出し元は必ず `if ! _run_privileged ...; then` の形で結果を判定する。
_run_privileged() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "root権限が必要ですが、sudoが見つかりませんでした。"
        return 1
    fi
}

_detect_linux_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then echo "apt"
    elif command -v dnf >/dev/null 2>&1; then echo "dnf"
    elif command -v yum >/dev/null 2>&1; then echo "yum"
    elif command -v pacman >/dev/null 2>&1; then echo "pacman"
    elif command -v zypper >/dev/null 2>&1; then echo "zypper"
    elif command -v apk >/dev/null 2>&1; then echo "apk"
    else echo ""
    fi
}

echo ""
echo "--- gcc(C言語の構文チェック(gcc -fsyntax-only)に使われます) ---"
if _has_gcc; then
    echo "gcc: 検出済み"
elif _ask_yes_no "gccが見つかりませんでした。インストールしますか? [y/N]: "; then
    case "$(uname -s)" in
        Linux)
            case "$(_detect_linux_pkg_manager)" in
                apt)    _run_privileged apt-get update && _run_privileged apt-get install -y build-essential || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                dnf)    _run_privileged dnf install -y gcc || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                yum)    _run_privileged yum install -y gcc || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                pacman) _run_privileged pacman -Sy --noconfirm base-devel || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                zypper) _run_privileged zypper install -y gcc || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                apk)    _run_privileged apk add build-base || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                *)      echo "対応しているパッケージマネージャーが見つかりませんでした。お使いのディストリビューションの手順で手動インストールしてください。" ;;
            esac
            ;;
        Darwin)
            echo "Xcodeコマンドラインツールのインストールダイアログを開きます。"
            xcode-select --install || true
            read -r -p "インストールが完了したらEnterキーを押してください(既にインストール済みの場合もEnterでスキップできます): " _ < "$TTY_DEV" || true
            ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "Windows環境では自動インストールに対応していません。MinGW-w64等のCコンパイラを手動で導入してください。"
            ;;
        *)
            echo "このOSでの自動インストールには対応していません。手動でインストールしてください。"
            ;;
    esac
    if _has_gcc; then
        echo "gccのインストールが完了しました。"
    else
        echo "gccのインストールを確認できませんでした。後で必要になれば手動でインストールしてください。"
    fi
else
    echo "スキップしました。後で必要になれば、公式サイトから手動でインストールしてください。"
fi

echo ""
echo "--- Node.js(JavaScriptの構文チェック(node --check)に使われます) ---"
if _has_node; then
    echo "node: 検出済み"
elif _ask_yes_no "nodeが見つかりませんでした。インストールしますか? [y/N]: "; then
    case "$(uname -s)" in
        Linux)
            case "$(_detect_linux_pkg_manager)" in
                apt)    _run_privileged apt-get update && _run_privileged apt-get install -y nodejs npm || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                dnf)    _run_privileged dnf install -y nodejs || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                yum)    _run_privileged yum install -y nodejs || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                pacman) _run_privileged pacman -Sy --noconfirm nodejs npm || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                zypper) _run_privileged zypper install -y nodejs || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                apk)    _run_privileged apk add nodejs npm || echo "インストールに失敗しました。手動でインストールしてください。" ;;
                *)      echo "対応しているパッケージマネージャーが見つかりませんでした。以下から手動でインストールしてください: https://nodejs.org/" ;;
            esac
            ;;
        Darwin)
            if command -v brew >/dev/null 2>&1; then
                brew install node || echo "インストールに失敗しました。手動でインストールしてください。"
            else
                echo "Homebrewが見つかりませんでした。以下から手動でインストールしてください: https://nodejs.org/"
            fi
            ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "Windows環境では自動インストールに対応していません。以下から公式インストーラーを入手してください: https://nodejs.org/"
            ;;
        *)
            echo "このOSでの自動インストールには対応していません。手動でインストールしてください。"
            ;;
    esac
    if _has_node; then
        echo "Node.jsのインストールが完了しました。"
    else
        echo "Node.jsのインストールを確認できませんでした。後で必要になれば手動でインストールしてください。"
    fi
else
    echo "スキップしました。後で必要になれば、公式サイトから手動でインストールしてください。"
fi

echo ""
echo "=== 5/5: 常駐化(自動起動)の設定 ==="
case "$(uname -s)" in
    Darwin) DAEMON_INSTALL_SCRIPT="./scripts/launchd/install.sh" ;;
    Linux)  DAEMON_INSTALL_SCRIPT="./scripts/systemd/install.sh" ;;
    *)      DAEMON_INSTALL_SCRIPT="" ;;
esac

if [[ -n "$DAEMON_INSTALL_SCRIPT" ]]; then
    read -r -p "ログアウト後も動き続けるよう常駐化しますか? [y/N]: " DAEMON_ANSWER < "$TTY_DEV" || true
    # 仮の判断: ${var,,}(小文字変換)はbash 4以降の構文で、macOS標準の
    # /bin/bash(3.2、ライセンス上の理由で更新されていない)では
    # "bad substitution" エラーになる。macOS/Linux両対応が要件のため、
    # bash 3.2でも動く正規表現マッチ(=~)で大文字・小文字どちらも許容する。
    if [[ "$DAEMON_ANSWER" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        if "$PYTHON_BIN" -c "import config, sys; sys.exit(0 if config.load_token() else 1)" 2>/dev/null; then
            "$DAEMON_INSTALL_SCRIPT" < "$TTY_DEV"
        else
            echo "トークン未設定のため常駐化はスキップしました。先にトークンを設定してから"
            echo "  $DAEMON_INSTALL_SCRIPT"
            echo "を実行してください(常駐化前に必須です)。"
        fi
    else
        echo "常駐化はスキップしました。あとで必要になったら次を実行してください: $DAEMON_INSTALL_SCRIPT"
    fi
else
    echo "このOS向けの常駐化スクリプトは用意していません。"
fi

echo ""
echo "=== セットアップが完了しました ==="
echo ""
echo "今すぐフォアグラウンドで起動する場合:"
echo "  source .venv/bin/activate && python3 yoriai.py"
echo ""
echo "今後アップデートする場合:"
echo "  ./scripts/update.sh"
