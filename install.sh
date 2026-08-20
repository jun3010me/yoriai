#!/bin/bash
# Yoriaiのワンライナーインストーラー。
#
# 使い方:
#   curl -fsSL https://raw.githubusercontent.com/jun3010me/yoriai/main/install.sh | bash
#
# インストール先を変えたい場合は環境変数 YORIAI_DIR で指定できます:
#   YORIAI_DIR=/opt/yoriai curl -fsSL .../install.sh | bash
set -euo pipefail

REPO_URL="https://github.com/jun3010me/yoriai.git"
INSTALL_DIR="${YORIAI_DIR:-$HOME/yoriai}"

echo "=== Yoriaiをインストールします ==="
echo "インストール先: $INSTALL_DIR"
echo ""

if ! command -v git >/dev/null 2>&1; then
    echo "エラー: gitが見つかりません。先にgitをインストールしてください。" >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "エラー: python3が見つかりません。先にPython 3.9以降をインストールしてください。" >&2
    exit 1
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
    echo "$INSTALL_DIR は既にYoriaiのインストール済みディレクトリのようです。"
    echo "更新したい場合は次を実行してください:"
    echo "  cd $INSTALL_DIR && ./scripts/update.sh"
    exit 0
elif [[ -e "$INSTALL_DIR" ]]; then
    echo "エラー: $INSTALL_DIR は既に存在しますが、Yoriaiのリポジトリではないようです。" >&2
    echo "YORIAI_DIR環境変数で別のインストール先を指定してください。" >&2
    exit 1
fi

echo "リポジトリをcloneします..."
git clone --quiet "$REPO_URL" "$INSTALL_DIR"

cd "$INSTALL_DIR"
exec ./scripts/setup.sh
