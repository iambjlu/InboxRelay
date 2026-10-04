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

    private func run(_ data: Data) -> (String?, String?) {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, [kCGImageSourceShouldCache: false] as CFDictionary)
        else { return (nil, "無法讀取圖片") }
        var orientation = CGImagePropertyOrientation.up
        if let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
           let raw = props[kCGImagePropertyOrientation] as? UInt32,
           let o = CGImagePropertyOrientation(rawValue: raw) {
            orientation = o
        }
        // 先用 Neural Engine；失敗（偶爾會暫時建不起來）就讓 Vision 自己挑 CPU／GPU 再跑一次
        var lastError = "辨識失敗"
        for useANE in [true, false] {
            let req = VNRecognizeTextRequest()
            req.recognitionLevel = .accurate
            req.usesLanguageCorrection = true
            req.recognitionLanguages = ["zh-Hant", "en-US"]
            if useANE { preferNeuralEngine(req) }
            let handler = VNImageRequestHandler(cgImage: img, orientation: orientation, options: [:])
            do {
                try handler.perform([req])
            } catch {
                lastError = (useANE ? "ANE：" : "CPU/GPU：") + error.localizedDescription
                continue
            }
            let lines = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
            return (lines.joined(separator: "\n"), nil)
        }
        return (nil, lastError)
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
