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
  <key>NSHighResolutionCapable</key><true/>
  <key>InboxRelayProjectPath</key><string>$(pwd)</string>
</dict>
</plist>
PLIST

codesign --force --deep -s - "$OUT" >/dev/null 2>&1 || echo "[build] ⚠️ 簽章失敗（不影響在本機執行）"
rm -f app/.build.log
echo "[build] ✅ 完成：$OUT"
echo "安裝：先關掉 Terminal 裡的 ./run.sh，然後"
echo "  rm -rf /Applications/InboxRelay.app && cp -R $OUT /Applications/ && open /Applications/InboxRelay.app"
