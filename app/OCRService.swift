// app/OCRService.swift
// 本機圖片文字辨識：Vision（VNRecognizeTextRequest，繁中＋英文），優先指定在 Apple Neural Engine 上跑。
// 核心送 {"type":"ocr_request","id","url","name"} → 這裡下載圖片、辨識 → 回 {"type":"ocr_result","id","text","ms"}。
// 一次只處理一張（序列佇列），圖片只在記憶體裡，辨識完就釋放。
import CoreML
import Foundation
import ImageIO
import Vision

final class OCRService: @unchecked Sendable {
    private let queue = DispatchQueue(label: "tw.iambjlu.InboxRelay.ocr", qos: .utility)
    private let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral          // 不留快取、不留 Cookie
        c.timeoutIntervalForRequest = 60
        c.urlCache = nil
        return URLSession(configuration: c)
    }()
    private(set) var deviceName = "Vision"

    /// 回傳 (文字, 錯誤, 毫秒, 是否暫時性錯誤＝稍後重試, 建議等待秒數)
    func recognize(urlString: String,
                   completion: @escaping (String?, String?, Int, Bool, Double) -> Void) {
        guard let url = URL(string: urlString) else {
            completion(nil, "網址錯誤", 0, false, 0)
            return
        }
        session.dataTask(with: url) { data, resp, err in
            if let err = err {                                    // 網路錯誤：稍後重試
                completion(nil, "下載失敗：\(err.localizedDescription)", 0, true, 0)
                return
            }
            let http = resp as? HTTPURLResponse
            let code = http?.statusCode ?? 200
            guard (200..<300).contains(code), let data = data, !data.isEmpty else {
                // 429（太多請求）、5xx、403 等：不是圖片壞掉，是暫時拿不到 → 稍後重試
                let wait = Double(http?.value(forHTTPHeaderField: "Retry-After") ?? "") ?? 0
                completion(nil, "下載失敗：HTTP \(code)", 0, true, wait)
                return
            }
            self.queue.async {
                let t0 = Date()
                let (text, error) = autoreleasepool { self.run(data) }
                completion(text, error, Int(Date().timeIntervalSince(t0) * 1000), false, 0)
            }
        }.resume()
    }

    /// 注意：一定要「立即完整解碼」（ShouldCacheImmediately）。Telegram 的照片是 progressive JPEG，延遲解碼的 CGImage 交給 Vision
    /// 會一律丟 CRImageReaderError（連重繪成 sRGB 都救不了），曾造成兩千多張圖被標成「無法取得圖片」。
    private func run(_ data: Data) -> (String?, String?) {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
        else { return (nil, "無法讀取圖片") }
        var orientation = CGImagePropertyOrientation.up
        if let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
           let raw = props[kCGImagePropertyOrientation] as? UInt32,
           let o = CGImagePropertyOrientation(rawValue: raw) {
            orientation = o
        }
        // 先用 Neural Engine；失敗（偶爾會暫時建不起來）就讓 Vision 自己挑 CPU／GPU 再跑一次；
        // 還是失敗（CRImageReaderError：色彩空間／位元深度特殊、尺寸過大）→ 重繪成標準 sRGB 8-bit 再試
        var lastError = "辨識失敗"
        for attempt in 0..<3 {
            let useANE = attempt == 0
            var target = img
            if attempt == 2 {
                guard let n = normalize(img) else { break }
                target = n
            }
            let req = VNRecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = true
            req.recognitionLanguages = ["zh-Hant", "en-US"]
            if useANE { preferNeuralEngine(req) }
            let handler = VNImageRequestHandler(cgImage: target, orientation: orientation, options: [:])
            do {
                try handler.perform([req])
            } catch {
                lastError = (useANE ? "ANE：" : attempt == 1 ? "CPU/GPU：" : "重繪後：") + error.localizedDescription
                continue
            }
            let lines = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            return (lines.joined(separator: "\n"), nil)
        }
        return (nil, lastError)
    }

    /// 重繪成標準 sRGB／8-bit／RGBA 點陣圖（長邊最多 4096），避開 Vision 讀不了的特殊格式。
    private func normalize(_ img: CGImage) -> CGImage? {
        let maxSide: CGFloat = 4096
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

    /// 每個運算階段如果支援 Neural Engine 就指定它（macOS 14+）；不支援就讓 Vision 自己選。
    private func preferNeuralEngine(_ req: VNRequest) {
        guard let stages = try? req.supportedComputeStageDevices else { return }
        var used = false
        for (stage, devices) in stages {
            if let ane = devices.first(where: { if case .neuralEngine = $0 { return true } else { return false } }) {
                req.setComputeDevice(ane, for: stage)
                used = true
            }
        }
        deviceName = used ? "Apple Neural Engine" : "Vision（CPU／GPU）"
    }
}
