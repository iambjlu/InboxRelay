#!/usr/bin/env bash
# install_app.sh — 編譯 App → 結束舊的 → 換掉 /Applications/InboxRelay.app → 打開新的
# 用法：cd ~/Downloads/InboxRelay && ./install_app.sh [--instant]
#   --instant：打開後立即執行一次「外部 LLM」與「本機 ANE（OCR）」車道
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

# App 結束後核心可能還在收尾（或卡住）：等它結束，逾時就強制結束，避免新核心因「已有一個在跑」而啟動失敗
core_pids() { pgrep -f "/Applications/InboxRelay.app/Contents/Resources/core" 2>/dev/null || true; }
for _ in $(seq 1 40); do
  [ -z "$(core_pids)" ] && break
  sleep 1
done
if [ -n "$(core_pids)" ]; then
  echo "[install] 舊的核心行程還沒結束，強制結束：$(core_pids | tr '\n' ' ')"
  core_pids | xargs kill 2>/dev/null || true
  sleep 3
  core_pids | xargs kill -9 2>/dev/null || true
fi

rm -rf /Applications/InboxRelay.app
cp -R build/InboxRelay.app /Applications/
if [ "${1:-}" = "--instant" ]; then
  open /Applications/InboxRelay.app --args --instant
else
  open /Applications/InboxRelay.app
fi
echo "[install] 已安裝並打開 /Applications/InboxRelay.app，確認核心有沒有啟動…"
for _ in $(seq 1 45); do
  [ -n "$(core_pids)" ] && pgrep -x InboxRelay >/dev/null 2>&1 && { sleep 5; break; }
  sleep 1
done
if pgrep -x InboxRelay >/dev/null 2>&1 && [ -n "$(core_pids)" ]; then
  echo "[install] ✅ App 與核心都在運行"
else
  echo "[install] ⚠️ 核心沒有在運行（App：$(pgrep -x InboxRelay >/dev/null 2>&1 && echo 在 || echo 不在)）；請看 App 主視窗日誌"
  exit 1
fi
