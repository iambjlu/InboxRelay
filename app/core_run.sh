#!/usr/bin/env bash
# app/core_run.sh → 打包成 InboxRelay.app/Contents/Resources/core/run.sh
# App 內建核心的啟動腳本。App 本身是唯讀的；Python 環境與所有會寫的檔案放在
# ~/Library/Application Support/InboxRelay（第一次啟動自動建立，之後沿用）。
# 注意：stdout 保留給 App 的 JSON 狀態，這裡的訊息一律寫到 stderr。
set -euo pipefail
CORE="$(cd "$(dirname "$0")" && pwd)"
DATA="${INBOXRELAY_DATA:-$HOME/Library/Application Support/InboxRelay}"
mkdir -p "$DATA"
export INBOXRELAY_DATA="$DATA"
export INBOXRELAY_ENV="${INBOXRELAY_ENV:-$DATA/.env}"     # 設定由 App 傳入；這個檔案不存在也沒關係
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

PY="${PYTHON:-$(command -v python3 || echo /usr/bin/python3)}"
VENV="$DATA/venv"
REQ="$CORE/relay-core/requirements.txt"
STAMP="$VENV/.requirements.sha"

if [ ! -x "$VENV/bin/python" ]; then
  echo "[core] 第一次啟動：建立 Python 環境（約 1～3 分鐘，之後不用）" >&2
  "$PY" -m venv "$VENV" >&2
fi
SUM="$(shasum "$REQ" | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$SUM" ]; then
  echo "[core] 安裝／更新 Python 套件…" >&2
  "$VENV/bin/python" -m pip install -q --upgrade pip >&2
  "$VENV/bin/python" -m pip install -q -r "$REQ" >&2
  echo "$SUM" > "$STAMP"
fi
exec "$VENV/bin/python" "$CORE/relay-core/main.py" "$@"
