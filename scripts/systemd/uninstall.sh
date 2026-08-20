#!/bin/bash
# systemdからYoriaiの登録を解除するスクリプト。
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "このスクリプトはLinux専用です(systemd)。macOSでは scripts/launchd/uninstall.sh を使ってください。" >&2
    exit 1
fi

SERVICE_NAME="yoriai.service"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ -f "$SERVICE_PATH" ]]; then
    sudo systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    sudo systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    sudo rm -f "$SERVICE_PATH"
    sudo systemctl daemon-reload
    echo "systemdの登録を解除しました: $SERVICE_PATH"
else
    echo "サービスファイルが見つかりません: $SERVICE_PATH"
fi
