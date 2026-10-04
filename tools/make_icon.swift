// tools/make_icon.swift
// 產生 App 圖示（1024px PNG）：圓角漸層底＋白色 SF Symbol。用法：swift tools/make_icon.swift 輸出.png
import AppKit

let out = CommandLine.arguments.dropFirst().first ?? "icon.png"
let px = 1024
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                 samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                 colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { exit(1) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let size = CGFloat(px)
let card = NSRect(x: 0, y: 0, width: size, height: size).insetBy(dx: 100, dy: 100)   // macOS 圖示留邊
let path = NSBezierPath(roundedRect: card, xRadius: 185, yRadius: 185)
NSGradient(colors: [NSColor(calibratedRed: 0.20, green: 0.45, blue: 0.95, alpha: 1),
                    NSColor(calibratedRed: 0.35, green: 0.80, blue: 0.90, alpha: 1)])?.draw(in: path, angle: 90)
let cfg = NSImage.SymbolConfiguration(pointSize: 400, weight: .semibold)
    .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
if let sym = NSImage(systemSymbolName: "tray.and.arrow.down.fill", accessibilityDescription: nil)?
    .withSymbolConfiguration(cfg) {
    let s = sym.size
    sym.draw(in: NSRect(x: (size - s.width) / 2, y: (size - s.height) / 2, width: s.width, height: s.height))
}
NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try png.write(to: URL(fileURLWithPath: out))
