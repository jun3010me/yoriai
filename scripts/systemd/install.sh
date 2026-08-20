#!/bin/bash
# YoriaiをLinuxのsystemd(システムサービス)に登録し、起動時に自動起動・
# クラッシュ時は自動再起動されるようにするインストールスクリプト。
# ログアウト後も動き続けるよう、ユーザーセッションのsystemdではなく
# システム全体のsystemd(/etc/systemd/system)に登録する(sudoが必要)。
#
# 前提: あらかじめ `python3 yoriai.py --init` または `--join=<トークン>` で
# トークンを保存しておくこと。トークン未設定のまま登録すると、
# yoriai.pyが起動のたびにすぐ終了し、systemdが再起動を繰り返してしまう。
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "このスクリプトはLinux専用です(systemd)。macOSでは scripts/launchd/install.sh を使ってください。" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctlが見つかりません。systemdが使えない環境では常駐化スクリプトは使えません。" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SERVICE_NAME="yoriai.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
RUN_USER="$(id -un)"
LOG_DIR="$HOME/.yoriai/logs"

# 仮の判断: リポジトリ直下に .venv があればそちらのpython3を優先する
# (READMEのセットアップ手順がvenvを使う前提のため)。無ければPATH上のpython3を使う。
if [[ -x "$REPO_DIR/.venv/bin/python3" ]]; then
    PYTHON_BIN="$REPO_DIR/.venv/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
    echo "python3が見つかりませんでした。先にセットアップを完了してください。" >&2
    exit 1
fi

mkdir -p "$LOG_DIR"

TMP_SERVICE_FILE="$(mktemp)"
trap 'rm -f "$TMP_SERVICE_FILE"' EXIT

sed \
    -e "s|__USER__|$RUN_USER|g" \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    -e "s|__YORIAI_SCRIPT__|$REPO_DIR/yoriai.py|g" \
    -e "s|__WORKING_DIR__|$REPO_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$SCRIPT_DIR/yoriai.service.template" > "$TMP_SERVICE_FILE"

echo "systemdサービスファイルを $SERVICE_PATH に配置します(sudoのパスワードが求められる場合があります)"
sudo cp "$TMP_SERVICE_FILE" "$SERVICE_PATH"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "systemdにYoriaiを登録しました: $SERVICE_PATH"
echo "実行ユーザー: $RUN_USER"
echo "python3: $PYTHON_BIN"
echo "ログ: $LOG_DIR/agent.log / $LOG_DIR/agent.error.log (または journalctl)"
echo ""
echo "状態確認: sudo systemctl status $SERVICE_NAME"
echo "ログ確認: sudo journalctl -u $SERVICE_NAME -f"
echo "一時停止: sudo systemctl stop $SERVICE_NAME"
echo "再開:     sudo systemctl start $SERVICE_NAME"
echo "アンインストール: $SCRIPT_DIR/uninstall.sh"
