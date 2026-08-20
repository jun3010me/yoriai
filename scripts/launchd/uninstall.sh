#!/bin/bash
# launchdからYoriaiの登録を解除するスクリプト。
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "このスクリプトはmacOS専用です(launchd)。Linuxでは scripts/systemd/uninstall.sh を使ってください。" >&2
    exit 1
fi

PLIST_LABEL="com.yoriai.agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

if [[ -f "$PLIST_PATH" ]]; then
    launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
    rm -f "$PLIST_PATH"
    echo "launchdの登録を解除しました: $PLIST_PATH"
else
    echo "登録されたplistが見つかりません: $PLIST_PATH"
fi
