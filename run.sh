#!/usr/bin/env bash
# run.sh — 建立/沿用 .venv、安裝依賴、啟動 InboxRelay 核心
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
export PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1   # 安裝套件時不留下載快取
REQ="relay-core/requirements.txt"
STAMP=".venv/.requirements.installed"

if [ ! -x .venv/bin/python ]; then
  echo "[run] 建立 .venv（$($PY --version)）"
  "$PY" -m venv .venv
fi

if [ ! -f "$STAMP" ] || [ "$REQ" -nt "$STAMP" ]; then
  echo "[run] 安裝依賴到 .venv"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q -r "$REQ"
  touch "$STAMP"
fi

# 連結截圖小工具（macOS WebKit）：有 Xcode 命令列工具就自動編譯；原始碼更新時重新編譯
if [ "$(uname)" = "Darwin" ] && command -v swiftc >/dev/null 2>&1; then
  if [ ! -x tools/linkshot ] || [ tools/linkshot.swift -nt tools/linkshot ] || [ tools/linkshot-Info.plist -nt tools/linkshot ]; then
    echo "[run] 編譯連結截圖小工具 tools/linkshot"
    # 內嵌 Info.plist：允許 http:// 網址（ATS 例外），否則 http 連結會被系統擋掉
    if ! swiftc -O -swift-version 5 tools/linkshot.swift -o tools/linkshot \
        -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker tools/linkshot-Info.plist \
        2> tools/.linkshot-build.log; then
      echo "[run] ⚠️ linkshot 編譯失敗（連結截圖會停用），錯誤訊息在 tools/.linkshot-build.log"
    else
      rm -f tools/.linkshot-build.log
    fi
  fi
fi

export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
exec .venv/bin/python relay-core/main.py "$@"
