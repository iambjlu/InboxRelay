#!/usr/bin/env bash
# build_app.sh — 用 Xcode 命令列工具（swiftc）打包 InboxRelay.app，不需要開 Xcode
set -euo pipefail
cd "$(dirname "$0")"
OUT="build/InboxRelay.app"
ARCH="$(uname -m)"

command -v swiftc >/dev/null 2>&1 || { echo "找不到 swiftc，請先執行：xcode-select --install"; exit 1; }

echo "[build] 編譯 InboxRelay.app（$ARCH, macOS 14+）"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
if ! swiftc -O -swift-version 5 -parse-as-library -target "$ARCH-apple-macos14.0" \
      app/*.swift -o "$OUT/Contents/MacOS/InboxRelay" 2> app/.build.log; then
  echo "[build] ❌ 編譯失敗，完整錯誤在 app/.build.log："
  head -40 app/.build.log
  exit 1
fi

# 內建核心：relay-core（不含測試）＋啟動腳本＋預先編譯好的截圖小工具 → Contents/Resources/core
# 裝好之後 App 自己就能跑，不需要專案資料夾（專案資料夾只用來改程式、重新 build）
CORE="$OUT/Contents/Resources/core"
mkdir -p "$CORE/tools"
rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude 'tests' relay-core "$CORE/"
cp app/core_run.sh "$CORE/run.sh"
chmod +x "$CORE/run.sh"
if ! swiftc -O -swift-version 5 tools/linkshot.swift -o "$CORE/tools/linkshot" \
     -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker tools/linkshot-Info.plist \
     2> tools/.linkshot-build.log; then
  echo "[build] ⚠️ 截圖小工具編譯失敗（連結截圖會停用），錯誤在 tools/.linkshot-build.log"
else
  rm -f tools/.linkshot-build.log
fi

# App 圖示（Dock 用）：第一次 build 時產生 build/AppIcon.icns，之後沿用；失敗也不影響 App
ICON="build/AppIcon.icns"
if [ ! -f "$ICON" ] || [ tools/make_icon.swift -nt "$ICON" ]; then
  TMP="$(mktemp -d)"
  if swift tools/make_icon.swift "$TMP/icon.png" >/dev/null 2>&1; then
    mkdir -p "$TMP/AppIcon.iconset"
    for s in 16 32 128 256 512; do
      sips -z $s $s "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
      sips -z $((s * 2)) $((s * 2)) "$TMP/icon.png" --out "$TMP/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
    done
    iconutil -c icns "$TMP/AppIcon.iconset" -o "$ICON" || echo "[build] ⚠️ 圖示產生失敗（Dock 會顯示預設圖示）"
  else
    echo "[build] ⚠️ 圖示產生失敗（Dock 會顯示預設圖示）"
  fi
  rm -rf "$TMP"
fi
[ -f "$ICON" ] && cp "$ICON" "$OUT/Contents/Resources/AppIcon.icns"

cat > "$OUT/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>tw.iambjlu.InboxRelay</string>
  <key>CFBundleName</key><string>InboxRelay</string>
  <key>CFBundleDisplayName</key><string>InboxRelay</string>
  <key>CFBundleExecutable</key><string>InboxRelay</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.4</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>InboxRelayProjectPath</key><string>$(pwd)</string>
</dict>
</plist>
PLIST

codesign --force --deep -s - "$OUT" >/dev/null 2>&1 || echo "[build] ⚠️ 簽章失敗（不影響在本機執行）"
rm -f app/.build.log
echo "[build] ✅ 完成：$OUT"
echo "安裝：./install_app.sh（會自動結束舊版、換掉 /Applications/InboxRelay.app 並打開）"
