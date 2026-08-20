#!/bin/bash
# Yoriaiをmacの launchd(ユーザーエージェント)に登録し、ログイン時に自動起動・
# クラッシュ時は自動再起動されるようにするインストールスクリプト。
#
# 前提: あらかじめ `python3 yoriai.py --init` または `--join=<トークン>` で
# トークンを保存しておくこと。トークン未設定のまま登録すると、
# yoriai.pyが起動のたびにすぐ終了し、launchdが再起動を繰り返してしまう。
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "このスクリプトはmacOS専用です(launchd)。Linuxでは scripts/systemd/install.sh を使ってください。" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PLIST_LABEL="com.yoriai.agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
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
mkdir -p "$HOME/Library/LaunchAgents"

sed \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    -e "s|__YORIAI_SCRIPT__|$REPO_DIR/yoriai.py|g" \
    -e "s|__WORKING_DIR__|$REPO_DIR|g" \
    -e "s|__LOG_DIR__|$LOG_DIR|g" \
    "$SCRIPT_DIR/com.yoriai.agent.plist.template" > "$PLIST_PATH"

# 既に登録済みの場合に備えて、一度アンロードしてから読み込み直す
launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl load "$PLIST_PATH"

echo "launchdにYoriaiを登録しました: $PLIST_PATH"
echo "python3: $PYTHON_BIN"
echo "ログ: $LOG_DIR/agent.log / $LOG_DIR/agent.error.log"
echo ""
echo "状態確認: launchctl list | grep $PLIST_LABEL"
echo "一時停止: launchctl unload $PLIST_PATH"
echo "再開:     launchctl load $PLIST_PATH"
echo "アンインストール: $SCRIPT_DIR/uninstall.sh"
