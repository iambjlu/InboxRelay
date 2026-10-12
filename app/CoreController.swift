// app/CoreController.swift
// 管理 Python 核心子行程：啟動、停止、當掉自動重啟（指數退避），收發 JSON Lines。
import Foundation

struct CoreTask: Codable, Hashable {
    let label: String
    let done: Int
    let total: Int?
    let detail: String
    let line: String
    let progress: Double?     // 0～1，含目前項目的細部進度
}

struct LaneStatus: Codable, Hashable {
    var label: String = ""
    var mode: String = ""
    var state: String = ""          // running / waiting / paused / offline / unavailable
    var describe: String = ""
    var queued: Bool = false
    var backlog: Int = 0
    var next_start: String = ""
}

/// 核心啟動中回報的進度（見 relay-core/startup.py）
struct StartupInfo: Equatable {
    var stage: String
    var detail: String
    var step: Int
    var steps: Int
    var done: Int?
    var total: Int?
}

struct CoreStatus: Codable {
    var paused: Bool = false
    var tasks: [CoreTask] = []
    var backlog: Int = 0
    var probe_pending: Int = 0
    var summarized_until: String = ""
    var next_recap_hours: Double? = nil
    var recap_time: String = ""
    var chat_title: String = ""
    var ai_status: String = ""
    var rss_mb: Int = 0
    var recap_enabled: Bool = false
    var recap_phase: String = ""      // running / queued / ""
    var lanes: [String: LaneStatus]? = nil   // llm（外部 LLM）／ane（本機 Neural Engine）
}

@MainActor
final class CoreController: ObservableObject {
    enum State: Equatable { case stopped, starting, running, restarting(Int) }

    @Published var state: State = .stopped
    @Published var status = CoreStatus()
    @Published var logs: [String] = []
    /// 啟動中的進度（核心開始送一般狀態列後就是 nil）
    @Published var startup: StartupInfo? = nil
    private var instantPending = CommandLine.arguments.contains("--instant")   // 啟動後立即執行 LLM 與 OCR 車道一次
    @Published var lastError: String = ""
    /// 設定頁「測試連線」：nil＝沒在測；結果由核心回傳
    @Published var llmTesting = false
    @Published var llmTestResult: (ok: Bool, message: String)? = nil
    private var llmTestId = 0
    /// 按下按鈕後、核心回報新狀態前（最多 6 秒）先鎖住，避免連點送出重複指令
    @Published private(set) var pendingAction: String? = nil

    private var process: Process?
    private var stdinPipe: Pipe?
    private var outBuffer = Data()
    private var errBuffer = Data()
    private var wantRunning = false
    private var failures = 0
    let settings: SettingsStore
    let ocr = OCRService()

    init(settings: SettingsStore) {
        self.settings = settings
    }

    var isPaused: Bool { status.paused }
    var isRunning: Bool { state == .running }
    var recapBusy: Bool { !status.recap_phase.isEmpty }
    /// 有東西在跑（總結或 AI 工作）時，重新啟動／結束前要先確認
    var isBusy: Bool { isRunning && (recapBusy || !status.tasks.isEmpty) }

    var canRecapNow: Bool { isRunning && status.recap_enabled && !recapBusy && pendingAction == nil }
    var canPauseResume: Bool { isRunning && pendingAction == nil }
    var canRestart: Bool { state != .starting && pendingAction != "restart" }

    var recapButtonTitle: String {
        switch status.recap_phase {
        case "running": return "總結中…"
        case "queued": return status.paused ? "已排隊（暫停中）" : "總結即將開始…"
        default: return pendingAction == "recap" ? "送出中…" : "立即總結"
        }
    }

    var recapHelp: String {
        if !isRunning { return "核心沒在運行" }
        if !status.recap_enabled { return "每日總結沒有啟用（需要外部 LLM）" }
        if recapBusy { return "總結正在進行，完成前不能再按" }
        return status.paused ? "背景 AI 暫停中：按了會先排隊，按「繼續」才開始"
            : "馬上執行積欠的 AI 分析與每日／月總結（不用等每日時段）"
    }

    private func lock(_ action: String) {
        pendingAction = action
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            if self.pendingAction == action { self.pendingAction = nil }
        }
    }

    func start() {
        wantRunning = true
        launch()
    }

    func stop() {
        wantRunning = false
        send(["type": "stop"])
        if let p = process { terminateLater(p) }
    }

    /// 先請核心自己結束（會發「已離線」通知），15 秒後還在才強制結束。
    private func terminateLater(_ p: Process) {
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 15_000_000_000)
            if p.isRunning { p.terminate() }
        }
    }

    func restart() {
        guard canRestart else { return }
        lock("restart")
        appendLog("— 重新啟動核心 —")
        wantRunning = true
        failures = 0
        if let p = process, p.isRunning {
            send(["type": "stop"])
            terminateLater(p)
        } else {
            launch()
        }
    }

    func send(_ obj: [String: String]) {
        guard let pipe = stdinPipe,
              let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        try? pipe.fileHandleForWriting.write(contentsOf: data + Data([0x0A]))
    }

    func sendAny(_ obj: [String: Any]) {
        guard let pipe = stdinPipe,
              let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        try? pipe.fileHandleForWriting.write(contentsOf: data + Data([0x0A]))
    }

    // MARK: - 兩條車道（外部 LLM／本機 ANE）
    func lane(_ name: String) -> LaneStatus? { status.lanes?[name] }

    func canRun(_ name: String) -> Bool {
        guard isRunning, pendingAction == nil, let l = lane(name), !l.queued else { return false }
        if name == "llm" { return status.recap_enabled ? !recapBusy : l.state != "unavailable" }
        // 本機 ANE：已經在執行、或沒有待辨識的圖片時，「立即執行」沒有意義 → 反灰
        return (l.state == "waiting" || l.state == "paused") && l.backlog > 0
    }

    func canToggle(_ name: String) -> Bool {
        guard isRunning, pendingAction == nil, let l = lane(name), l.state != "unavailable" else { return false }
        // 待命（正在執行時段裡但沒有工作）時沒東西可暫停 → 反灰；已暫停的一定可以按「繼續」
        if l.state == "running" && l.backlog == 0 && !(name == "llm" && recapBusy) { return false }
        return true
    }

    func toggleHelp(_ name: String) -> String {
        guard let l = lane(name) else { return "" }
        if l.state == "paused" { return "繼續" }
        if l.state == "running" && l.backlog == 0 && !(name == "llm" && recapBusy) { return "目前沒有工作可以暫停" }
        return l.state == "waiting" ? "暫停（到了排程時間也不會開始）" : "暫停"
    }

    /// 「立即執行」按鈕的說明（滑鼠移上去顯示，也說明為什麼反灰）
    func runHelp(_ name: String) -> String {
        if name == "llm" { return recapHelp }
        guard isRunning, let l = lane(name) else { return "核心沒在運行" }
        let (doing, what) = name == "shot" ? ("截圖", "連結") : ("辨識", "圖片")
        switch l.state {
        case "running": return l.backlog > 0 ? "正在\(doing)中" : "沒有待\(doing)的\(what)"
        case "unavailable": return "等待 App 與核心連線"
        default: return l.backlog > 0 ? "馬上\(doing) \(l.backlog) 則還沒處理的\(what)（不用等排程）" : "沒有待\(doing)的\(what)"
        }
    }

    func runTitle(_ name: String) -> String {
        if name == "llm" && status.recap_enabled { return recapButtonTitle }
        if pendingAction == "run_\(name)" { return "送出中…" }
        return lane(name)?.queued == true ? "已排隊…" : "立即執行"
    }

    func laneRun(_ name: String) {
        guard canRun(name) else { return }
        lock(name == "llm" ? "recap" : "run_\(name)")
        sendAny(["type": "lane", "lane": name, "action": "run_now"])
    }

    func laneToggle(_ name: String) {
        guard canToggle(name), let l = lane(name) else { return }
        lock("toggle_\(name)")
        sendAny(["type": "lane", "lane": name, "action": l.state == "paused" ? "resume" : "pause"])
    }

    /// 用設定頁目前填的值測試外部 LLM（不必先儲存、重啟）。核心沒在跑就直接說明。
    func testLLM() {
        guard !llmTesting else { return }
        guard isRunning else {
            llmTestResult = (false, "核心沒有在運行，請先啟動核心再測試")
            return
        }
        llmTestId += 1
        let id = llmTestId
        llmTesting = true
        llmTestResult = nil
        sendAny(["type": "llm_test", "id": id,
                 "server": settings.values["LOCAL_AI_SERVER"] ?? "",
                 "model": settings.values["LOCAL_AI_MODEL"] ?? "",
                 "key": settings.values["LOCAL_AI_API_KEY"] ?? ""] as [String: Any])
        Task { @MainActor [weak self] in          // 核心 25 秒內沒回就當逾時
            try? await Task.sleep(nanoseconds: 25_000_000_000)
            guard let self = self, self.llmTesting, self.llmTestId == id else { return }
            self.llmTesting = false
            self.llmTestResult = (false, "測試逾時，核心沒有回應")
        }
    }

    private func handleStartup(_ json: [String: Any]) {
        if (json["finished"] as? Bool) == true { startup = nil; return }
        let info = StartupInfo(stage: (json["stage"] as? String) ?? "", detail: (json["detail"] as? String) ?? "",
                               step: (json["step"] as? Int) ?? 0, steps: (json["steps"] as? Int) ?? 0,
                               done: json["done"] as? Int, total: json["total"] as? Int)
        startup = info
        // 記住上次 #關鍵字庫 一共有幾筆：下次啟動讀取時就能算出大約完成幾 %
        if info.stage.hasPrefix("載入 #關鍵字庫"), let t = info.total, t > 0 {
            UserDefaults.standard.set(t, forKey: "lastKeywordTotal")
        }
    }

    /// 啟動進度 0～1：每個步驟各佔一份；讀 #關鍵字庫 時用「已讀數 ÷ 上次的總數」細分
    var startupFraction: Double? {
        guard let s = startup, s.steps > 0, s.step > 0 else { return nil }
        var within = 0.0
        if let done = s.done {
            let last = UserDefaults.standard.integer(forKey: "lastKeywordTotal")
            let total = s.total ?? (last > 0 ? last : 0)
            if total > 0 { within = min(Double(done) / Double(total), 0.97) }
        }
        return min((Double(s.step - 1) + within) / Double(s.steps), 0.99)
    }

    /// 啟動中這一步的說明（例如「步驟 6/8：載入 #關鍵字庫｜已讀 4,000 則（約 42%）」）
    var startupText: String? {
        guard let s = startup, s.step > 0 else { return nil }
        var t = "步驟 \(s.step)/\(s.steps)：\(s.stage)"
        if !s.detail.isEmpty { t += "｜\(s.detail)" }
        if let done = s.done, s.total == nil {
            let last = UserDefaults.standard.integer(forKey: "lastKeywordTotal")
            if last > 0 { t += "（約 \(min(Int(Double(done) / Double(last) * 100), 99))%）" }
        }
        return t
    }

    private func handleOCR(_ obj: [String: Any]) {
        guard let id = obj["id"] as? Int, let url = obj["url"] as? String else { return }
        ocr.recognize(urlString: url) { [weak self] text, error, ms, retry, wait in
            Task { @MainActor in
                self?.sendAny(["type": "ocr_result", "id": id, "text": text ?? "", "error": error ?? "", "ms": ms,
                               "retry": retry, "retry_after": wait] as [String: Any])
            }
        }
    }

    /// 清空 #總結 並依日期重建目錄（佔位），之後照外部 LLM 排程重新總結
    func recapReset() {
        guard isRunning, status.recap_enabled, pendingAction == nil else { return }
        lock("recap_reset")
        sendAny(["type": "recap_reset"] as [String: Any])
    }

    func recapNow() {
        guard canRecapNow else { return }
        lock("recap")
        send(["type": "recap_now"])
    }
    func pause() {
        guard canPauseResume, !status.paused else { return }
        lock("pause")
        send(["type": "pause"])
    }
    func resume() {
        guard canPauseResume, status.paused else { return }
        lock("resume")
        send(["type": "resume"])
    }

    // MARK: - 子行程

    private func launch() {
        guard process == nil else { return }
        // 優先用 App 內建的核心（Contents/Resources/core）；沒有才退回專案資料夾（開發用）
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("core/run.sh").path ?? ""
        let fm = FileManager.default
        let runSh = fm.fileExists(atPath: bundled) ? bundled
            : (settings.projectPath as NSString).appendingPathComponent("run.sh")
        guard fm.fileExists(atPath: runSh) else {
            lastError = "找不到核心程式（App 內建的 core 不見了），請重新安裝 InboxRelay"
            state = .stopped
            return
        }
        let workDir = (runSh as NSString).deletingLastPathComponent
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [runSh]
        p.currentDirectoryURL = URL(fileURLWithPath: workDir)
        p.environment = settings.environment()
        let inPipe = Pipe(), outPipe = Pipe(), errPipe = Pipe()
        p.standardInput = inPipe
        p.standardOutput = outPipe
        p.standardError = errPipe
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            Task { @MainActor in self?.consume(d, isErr: false) }
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] h in
            let d = h.availableData
            Task { @MainActor in self?.consume(d, isErr: true) }
        }
        p.terminationHandler = { [weak self] proc in
            let code = proc.terminationStatus
            Task { @MainActor in self?.terminated(code: code) }
        }
        do {
            try p.run()
            process = p
            stdinPipe = inPipe
            state = .starting
            lastError = ""
            // 告訴核心：這個 App 會做本機文字辨識（核心讀到後「本機 ANE」車道才會開始）
            sendAny(["type": "hello", "ocr": true, "device": "Vision／Apple Neural Engine"] as [String: Any])
            appendLog("— 啟動核心（\(runSh == bundled ? "App 內建" : workDir)）—")
        } catch {
            lastError = "無法啟動：\(error.localizedDescription)"
            state = .stopped
        }
    }

    private func terminated(code: Int32) {
        process = nil
        stdinPipe = nil
        if pendingAction == "restart" { pendingAction = nil }
        status.recap_phase = ""
        status.tasks = []
        startup = nil
        appendLog("— 核心結束（代碼 \(code)）—")
        guard wantRunning else {
            state = .stopped
            return
        }
        if code == 4 {                // 另一個核心已在執行（單一執行鎖）
            lastError = "已經有另一個 InboxRelay 在跑（可能是 Terminal 裡的 ./run.sh），請先關掉它"
            state = .stopped
            wantRunning = false
            return
        }
        if code == 5 {                // 另一台電腦正在運行 → 每 5 分鐘再試，對方停了就接手
            lastError = "另一台電腦正在運行 InboxRelay，這台待命中（每 5 分鐘檢查一次，對方停掉就自動接手）"
            state = .restarting(300)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 300_000_000_000)
                guard let self = self, self.wantRunning, self.process == nil else { return }
                self.launch()
            }
            return
        }
        failures += 1
        let delay = min(60, Int(pow(2.0, Double(min(failures, 6)))))
        state = .restarting(delay)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay) * 1_000_000_000)
            guard let self = self, self.wantRunning, self.process == nil else { return }
            self.launch()
        }
    }

    private func consume(_ data: Data, isErr: Bool) {
        if data.isEmpty { return }
        if isErr { errBuffer.append(data) } else { outBuffer.append(data) }
        var buf = isErr ? errBuffer : outBuffer
        while let nl = buf.firstIndex(of: 0x0A) {
            let lineData = buf[buf.startIndex..<nl]
            buf.removeSubrange(buf.startIndex...nl)
            let line = String(decoding: lineData, as: UTF8.self)
            if !isErr, line.hasPrefix("{"), let d = line.data(using: .utf8),
               let json = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               (json["type"] as? String) == "ocr_request" {
                handleOCR(json)
            } else if !isErr, line.hasPrefix("{"), let d = line.data(using: .utf8),
               let json = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               (json["type"] as? String) == "startup" {
                handleStartup(json)
            } else if !isErr, line.hasPrefix("{"), let d = line.data(using: .utf8),
               let json = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               (json["type"] as? String) == "llm_test_result" {
                if (json["id"] as? Int) == llmTestId {
                    llmTesting = false
                    llmTestResult = ((json["ok"] as? Bool) ?? false, (json["message"] as? String) ?? "")
                }
            } else if !isErr, line.hasPrefix("{"), let d = line.data(using: .utf8),
               let obj = try? JSONDecoder().decode(CoreStatus.self, from: d) {
                if pendingAction != "restart" { pendingAction = nil }   // 核心回報新狀態 → 解鎖
                startup = nil                                              // 開始送一般狀態列＝啟動完成
                status = obj
                if instantPending, let lanes = obj.lanes, lanes["llm"] != nil,
                   let ane = lanes["ane"], ane.state != "unavailable" {
                    instantPending = false
                    for name in ["llm", "ane", "shot"] where lanes[name] != nil {
                        sendAny(["type": "lane", "lane": name, "action": "run_now"])
                    }
                }
                if state != .running {
                    state = .running
                    failures = 0
                }
            } else if !line.trimmingCharacters(in: .whitespaces).isEmpty {
                appendLog(line)
            }
        }
        if isErr { errBuffer = buf } else { outBuffer = buf }
    }

    private func appendLog(_ line: String) {
        logs.append(line)
        if logs.count > 500 { logs.removeFirst(logs.count - 500) }   // 只留在記憶體的環狀緩衝（500 行）
    }
}
