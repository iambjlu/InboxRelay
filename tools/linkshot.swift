// tools/linkshot.swift
// 連結截圖小工具：用 macOS 內建 WebKit 在背景開網頁，截第一個畫面，PNG 輸出到 stdout。
//
// 用法：linkshot <網址> [--width 1280] [--height 800] [--max-height 4000] [--timeout 25] [--settle 5] [--insecure]
// - 整頁截圖：依網頁實際高度拉長視窗再拍，最多 max-height；輸出 JPEG（比 PNG 小很多）。
// - --insecure：憑證無效也照樣開（只讀取畫面，不送出任何資料）；有用到時 stderr 會印 CERT_BYPASSED。
// - 網頁載入完成（含轉址）後再等 settle 秒才拍；等待中又跳轉就重新計時。
// - 不顯示視窗、不出現在 Dock。
// - 使用非持久化資料儲存：不寫 Cookie、快取到硬碟。
// - 只允許 http/https；下載、外部 App 連結一律擋掉。
// 編譯：swiftc -O tools/linkshot.swift -o tools/linkshot（run.sh 會自動做）
import AppKit
import WebKit

func note(_ msg: String) {
    FileHandle.standardError.write("linkshot: \(msg)\n".data(using: .utf8)!)
}

func fail(_ msg: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write("linkshot: \(msg)\n".data(using: .utf8)!)
    exit(code)
}

final class Shooter: NSObject, WKNavigationDelegate {
    let url: URL
    let size: CGSize
    let settle: Double
    let maxHeight: Double
    let insecure: Bool
    var window: NSWindow?
    var web: WKWebView?
    var finished = false
    var snapping = false
    var generation = 0          // 每次開始新的主頁面載入就 +1，舊的倒數作廢
    var loadedOnce = false

    init(url: URL, size: CGSize, settle: Double, maxHeight: Double, insecure: Bool) {
        self.url = url
        self.size = size
        self.settle = settle
        self.maxHeight = max(maxHeight, Double(size.height))
        self.insecure = insecure
    }

    // 憑證無效時：只有加了 --insecure 才接受
    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           let trust = challenge.protectionSpace.serverTrust {
            if SecTrustEvaluateWithError(trust, nil) {
                completionHandler(.performDefaultHandling, nil)
            } else if insecure {
                note("CERT_BYPASSED \(challenge.protectionSpace.host)")
                completionHandler(.useCredential, URLCredential(trust: trust))
            } else {
                completionHandler(.performDefaultHandling, nil)
            }
            return
        }
        completionHandler(.performDefaultHandling, nil)
    }

    func start() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        cfg.mediaTypesRequiringUserActionForPlayback = .all
        let rect = NSRect(origin: .zero, size: size)
        let web = WKWebView(frame: rect, configuration: cfg)
        web.navigationDelegate = self
        // 視窗在畫面外會被當成「被遮住」，WebKit 會暫停繪製導致截不到圖；關掉遮蔽偵測
        let occ = NSSelectorFromString("_setWindowOcclusionDetectionEnabled:")
        if web.responds(to: occ) { web.setValue(false, forKey: "windowOcclusionDetectionEnabled") }  // KVC 會找 _set…: 並轉成 BOOL
        web.customUserAgent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
            + "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        // 放在畫面外、完全透明的視窗裡，WebKit 才會正常排版與繪製
        let win = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height),
                           styleMask: [.borderless], backing: .buffered, defer: false)
        win.isReleasedWhenClosed = false
        win.alphaValue = 0
        win.ignoresMouseEvents = true
        win.contentView = web
        win.orderBack(nil)
        self.window = win
        self.web = web
        web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 20))
    }

    // 只允許 http/https，擋下載
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let scheme = navigationAction.request.url?.scheme?.lowercased() ?? ""
        if !["http", "https", "about", "data", "blob"].contains(scheme) {
            decisionHandler(.cancel)
            return
        }
        if #available(macOS 11.3, *), navigationAction.shouldPerformDownload {
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    // 只顯示網頁、圖片、純文字等，而且主頁面不能太大；下載連結（.ipsw、.zip…）只看標頭就取消，不會下載
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let resp = navigationResponse.response
        let mime = (resp.mimeType ?? "").lowercased()
        let size = resp.expectedContentLength          // 不知道時是 -1
        let displayable = mime.hasPrefix("text/") || mime.hasPrefix("image/") || mime.contains("html")
            || mime.contains("xml") || mime == "application/pdf"
        if navigationResponse.isForMainFrame
            && (!navigationResponse.canShowMIMEType || !displayable || size > 30_000_000) {
            decisionHandler(.cancel)
            fail("不是可截圖的網頁（\(mime.isEmpty ? "未知類型" : mime)，\(size > 0 ? "\(size / 1_000_000) MB" : "大小不明")）", code: 4)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        generation += 1
        note("開始載入 \(webView.url?.absoluteString ?? "")")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadedOnce = true
        let g = generation
        note("載入完成，\(settle) 秒後拍攝：\(webView.url?.absoluteString ?? "")")
        DispatchQueue.main.asyncAfter(deadline: .now() + settle) {
            if g == self.generation { self.snap() }    // 等待中又跳轉了就等下一次 didFinish
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        if (error as NSError).code == NSURLErrorCancelled { return }   // 被新的跳轉取代，不算失敗
        if !snapping { fail("載入失敗：\(error.localizedDescription)") }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        let ns = error as NSError
        if ns.code == NSURLErrorCancelled || ns.code == 102 { return }  // 被取代的跳轉／WebKit「frame load interrupted」
        if !snapping && !loadedOnce { fail("無法開啟：\(error.localizedDescription)（\(ns.domain) \(ns.code)）") }
    }

    func snap() {
        guard !snapping, let web = web else { return }
        snapping = true
        // 先量網頁高度，把視窗拉長到整頁（最多 maxHeight），等 1.5 秒讓新露出的圖片載入再拍
        let js = "Math.max(document.documentElement ? document.documentElement.scrollHeight : 0, "
            + "document.body ? document.body.scrollHeight : 0)"
        web.evaluateJavaScript(js) { value, _ in
            let pageH = (value as? NSNumber)?.doubleValue ?? 0
            let h = min(max(pageH, Double(self.size.height)), self.maxHeight)
            if h > Double(self.size.height) + 1 {
                let full = NSSize(width: self.size.width, height: CGFloat(h))
                self.window?.setContentSize(full)
                web.frame = NSRect(origin: .zero, size: full)
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.shoot(height: h) }
            } else {
                self.shoot(height: Double(self.size.height))
            }
        }
    }

    func shoot(height: Double) {
        guard let web = web else { return }
        let cfg = WKSnapshotConfiguration()
        cfg.rect = CGRect(x: 0, y: 0, width: size.width, height: CGFloat(height))
        cfg.snapshotWidth = NSNumber(value: Double(size.width))   // 固定 1 倍寬度，檔案比較小
        web.takeSnapshot(with: cfg) { image, error in
            guard let image = image,
                  let tiff = image.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let jpg = rep.representation(using: .jpeg, properties: [.compressionFactor: 0.8]) else {
                fail("截圖失敗：\(error?.localizedDescription ?? "未知錯誤")", code: 3)
            }
            FileHandle.standardOutput.write(jpg)
            self.finished = true
            exit(0)
        }
    }
}

// ---- 參數 ----
var args = Array(CommandLine.arguments.dropFirst())
func option(_ name: String, _ def: Double) -> Double {
    if let i = args.firstIndex(of: name), i + 1 < args.count, let v = Double(args[i + 1]) {
        args.removeSubrange(i...(i + 1))
        return v
    }
    return def
}
let width = option("--width", 1280)
let height = option("--height", 800)
let timeout = option("--timeout", 25)
let settle = option("--settle", 5)
let maxHeight = option("--max-height", 4000)
let insecure: Bool = {
    if let i = args.firstIndex(of: "--insecure") { args.remove(at: i); return true }
    return false
}()
guard let raw = args.first, let url = URL(string: raw),
      let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else {
    fail("用法：linkshot <http(s) 網址> [--width 1280] [--height 800] [--timeout 20]", code: 64)
}

let app = NSApplication.shared
app.setActivationPolicy(.prohibited)   // 不出現在 Dock、不搶焦點
let shooter = Shooter(url: url, size: CGSize(width: width, height: height), settle: settle,
                      maxHeight: maxHeight, insecure: insecure)
shooter.start()

// 時間到還沒載完：有畫面就先截；再給 5 秒還不行就放棄
DispatchQueue.main.asyncAfter(deadline: .now() + timeout + settle) {
    if !shooter.finished {
        note("等太久，直接拍目前畫面")
        shooter.snap()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { fail("逾時", code: 2) }
    }
}
app.run()
