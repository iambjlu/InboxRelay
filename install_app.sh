#!/usr/bin/env bash
# install_app.sh — 編譯 App → 結束舊的 → 換掉 /Applications/InboxRelay.app → 打開新的
# 用法：cd ~/Downloads/InboxRelay && ./install_app.sh
set -euo pipefail
cd "$(dirname "$0")"

./build_app.sh                                   # 編譯失敗會在這裡停下，舊 App 不受影響

if pgrep -x InboxRelay >/dev/null 2>&1; then
  echo "[install] 結束執行中的 InboxRelay…"
  osascript -e 'quit app "InboxRelay"' >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do                       # 最多等 30 秒（核心會先發「已離線」通知）
    pgrep -x InboxRelay >/dev/null 2>&1 || break
    sleep 1
  done
  pgrep -x InboxRelay >/dev/null 2>&1 && { echo "[install] ⚠️ InboxRelay 還沒結束，請從選單列手動結束後再跑一次"; exit 1; }
fi

rm -rf /Applications/InboxRelay.app
cp -R build/InboxRelay.app /Applications/
open /Applications/InboxRelay.app
echo "[install] ✅ 已安裝並打開 /Applications/InboxRelay.app"
