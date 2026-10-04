// tools/localocr.swift
// 命令列文字辨識（Vision，繁中＋英文），給 tools/local_export.py 用：
//   localocr <圖片路徑>...        每張一行 JSON：{"path","text","error","ms"}
//   echo 路徑 | localocr -          從 stdin 一行一個路徑
// 和 App 內建的 OCRService 同樣的辨識設定；太高／太寬的長截圖會切成帶重疊的幾塊分別辨識再接起來
// （Vision 對極端長寬比的圖常常直接丟 CRImageReaderError）。圖片只在記憶體裡處理。
import CoreML
import Foundation
import ImageIO
import Vision

func normalize(_ img: CGImage, maxSide: CGFloat = 4096) -> CGImage? {
    let w = CGFloat(img.width), h = CGFloat(img.height)
    guard w > 0, h > 0 else { return nil }
    let scale = min(1, maxSide / max(w, h))
    let tw = max(1, Int((w * scale).rounded())), th = max(1, Int((h * scale).rounded()))
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: tw, height: th, bitsPerComponent: 8, bytesPerRow: 0,
                              space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: tw, height: th))
    ctx.interpolationQuality = .high
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: tw, height: th))
    return ctx.makeImage()
}

func recognize(_ img: CGImage, orientation: CGImagePropertyOrientation) throws -> [String] {
    var last: Error? = nil
    for attempt in 0..<2 {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = true
        req.recognitionLanguages = ["zh-Hant", "en-US"]
        let handler = VNImageRequestHandler(cgImage: img, orientation: orientation, options: [:])
        do {
            try handler.perform([req])
            return (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
        } catch { last = error }
    }
    throw last ?? NSError(domain: "ocr", code: 1)
}

/// 切塊：長邊／短邊超過 4 就沿長邊切成帶 10% 重疊的塊（每塊長寬比 ≤ 3）。
func tiles(_ img: CGImage) -> [CGImage] {
    let w = img.width, h = img.height
    let tall = h > w
    let long = tall ? h : w, short = tall ? w : h
    guard Double(long) / Double(max(short, 1)) > 4 else { return [img] }
    let step = short * 3, overlap = short / 3
    var out: [CGImage] = []
    var start = 0
    while start < long {
        let len = min(step, long - start)
        let rect = tall ? CGRect(x: 0, y: start, width: w, height: len) : CGRect(x: start, y: 0, width: len, height: h)
        if let c = img.cropping(to: rect) { out.append(c) }
        if start + len >= long { break }
        start += step - overlap
    }
    return out.isEmpty ? [img] : out
}

func run(path: String) -> (String?, String?) {
    guard let data = FileManager.default.contents(atPath: path),
          let src = CGImageSourceCreateWithData(data as CFData, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
    else { return (nil, "無法讀取圖片") }
    var orientation = CGImagePropertyOrientation.up
    if let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
       let raw = props[kCGImagePropertyOrientation] as? UInt32,
       let o = CGImagePropertyOrientation(rawValue: raw) { orientation = o }
    var lastError = "辨識失敗"
    // 原圖 → 重繪成標準 sRGB 後再試；長截圖兩種都先切塊
    for variant in 0..<2 {
        guard let base = variant == 0 ? img : normalize(img) else { continue }
        var lines: [String] = []
        var failed = false
        for t in tiles(base) {
            do { lines += try autoreleasepool { try recognize(t, orientation: variant == 0 ? orientation : .up) } }
            catch { lastError = (variant == 0 ? "" : "重繪後：") + error.localizedDescription; failed = true; break }
        }
        if !failed { return (lines.joined(separator: "\n"), nil) }
    }
    return (nil, lastError)
}

setbuf(stdout, nil)
var paths = Array(CommandLine.arguments.dropFirst())
if paths == ["-"] {
    paths = []
    while let line = readLine() { if !line.isEmpty { paths.append(line) } }
}
for p in paths {
    let t0 = Date()
    let (text, err) = autoreleasepool { run(path: p) }
    let obj: [String: Any] = ["path": p, "text": text ?? "", "error": err ?? "",
                              "ms": Int(Date().timeIntervalSince(t0) * 1000)]
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.withoutEscapingSlashes]),
       let s = String(data: d, encoding: .utf8) { print(s) }
}
