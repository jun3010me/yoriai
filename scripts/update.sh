#!/bin/bash
# Yoriaiのコードを最新化し、依存関係を更新し、常駐化している場合は
# サービス(launchd/systemd)を再起動するスクリプト。
#
# 使い方: ./scripts/update.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_DIR"

echo "=== 1/3: 最新のコードを取得します ==="
# 仮の判断: ローカルに未コミットの変更や、originから分岐したコミットがある状態での
# 上書きは事故のもとなので、fast-forwardできる場合だけ更新する(force的な上書きはしない)。
git fetch origin
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git merge --ff-only "origin/$CURRENT_BRANCH"

echo ""
echo "=== 2/3: 依存関係を更新します ==="
if [[ -x "$REPO_DIR/.venv/bin/pip" ]]; then
    "$REPO_DIR/.venv/bin/pip" install -q -r requirements.txt
else
    pip install -q -r requirements.txt
fi

echo ""
echo "=== 3/3: 常駐化サービスを再起動します(登録されている場合のみ) ==="
case "$(uname -s)" in
    Darwin)
        PLIST_PATH="$HOME/Library/LaunchAgents/com.yoriai.agent.plist"
        if [[ -f "$PLIST_PATH" ]]; then
            launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
            launchctl load "$PLIST_PATH"
            echo "launchdサービスを再起動しました。"
        else
            echo "launchdサービスは登録されていません(常駐化していない場合はこのままでOKです)。"
            echo "フォアグラウンドで実行中の場合は、手動でCtrl+Cしてから再度起動してください。"
        fi
        ;;
    Linux)
        if command -v systemctl >/dev/null 2>&1 && [[ -f /etc/systemd/system/yoriai.service ]]; then
            sudo systemctl restart yoriai.service
            echo "systemdサービスを再起動しました。"
        else
            echo "systemdサービスは登録されていません(常駐化していない場合はこのままでOKです)。"
            echo "フォアグラウンドで実行中の場合は、手動でCtrl+Cしてから再度起動してください。"
        fi
        ;;
    *)
        echo "未対応のOSのため、サービスの再起動はスキップしました。"
        ;;
esac

echo ""
echo "更新が完了しました。"
