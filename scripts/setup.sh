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

echo "=== 1/4: Python仮想環境(.venv)を用意します ==="
if [[ ! -d "$REPO_DIR/.venv" ]]; then
    python3 -m venv "$REPO_DIR/.venv"
    echo ".venv を作成しました。"
else
    echo ".venv は既に存在します(スキップ)。"
fi
PYTHON_BIN="$REPO_DIR/.venv/bin/python3"
PIP_BIN="$REPO_DIR/.venv/bin/pip"

echo ""
echo "=== 2/4: 依存関係をインストールします ==="
"$PIP_BIN" install -q -r requirements.txt
echo "依存関係のインストールが完了しました。"

echo ""
echo "=== 3/4: トークン(組織の合言葉)を設定します ==="
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
echo "=== 4/4: 常駐化(自動起動)の設定 ==="
case "$(uname -s)" in
    Darwin) DAEMON_INSTALL_SCRIPT="./scripts/launchd/install.sh" ;;
    Linux)  DAEMON_INSTALL_SCRIPT="./scripts/systemd/install.sh" ;;
    *)      DAEMON_INSTALL_SCRIPT="" ;;
esac

if [[ -n "$DAEMON_INSTALL_SCRIPT" ]]; then
    read -r -p "ログアウト後も動き続けるよう常駐化しますか? [y/N]: " DAEMON_ANSWER < "$TTY_DEV" || true
    if [[ "${DAEMON_ANSWER,,}" == "y" || "${DAEMON_ANSWER,,}" == "yes" ]]; then
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
