#!/bin/bash
# Yoriaiの初回セットアップ(venv作成・依存関係インストール・トークン設定・
# 常駐化の案内)をまとめて行うスクリプト。
#
# 使い方: ./scripts/setup.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

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
    read -r -p "選んでください [1/2/3]: " CHOICE
    case "$CHOICE" in
        1)
            "$PYTHON_BIN" yoriai.py --init
            ;;
        2)
            read -r -p "参加するトークン(合言葉)を入力してください: " TOKEN
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
    read -r -p "ログアウト後も動き続けるよう常駐化しますか? [y/N]: " DAEMON_ANSWER
    if [[ "${DAEMON_ANSWER,,}" == "y" || "${DAEMON_ANSWER,,}" == "yes" ]]; then
        if "$PYTHON_BIN" -c "import config, sys; sys.exit(0 if config.load_token() else 1)" 2>/dev/null; then
            "$DAEMON_INSTALL_SCRIPT"
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
