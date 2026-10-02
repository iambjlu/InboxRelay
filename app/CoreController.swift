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
}

@MainActor
final class CoreController: ObservableObject {
    enum State: Equatable { case stopped, starting, running, restarting(Int) }

    @Published var state: State = .stopped
    @Published var status = CoreStatus()
    @Published var logs: [String] = []
    @Published var lastError: String = ""
    /// 按下按鈕後、核心回報新狀態前（最多 6 秒）先鎖住，避免連點送出重複指令
    @Published private(set) var pendingAction: String? = nil

    private var process: Process?
    private var stdinPipe: Pipe?
    private var outBuffer = Data()
    private var errBuffer = Data()
    private var wantRunning = false
    private var failures = 0
    let settings: SettingsStore

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
        if !status.recap_enabled { return "每日總結沒有啟用（需要本地 AI）" }
        if recapBusy { return "總結正在進行，完成前不能再按" }
        return status.paused ? "背景 AI 暫停中：按了會先排隊，按「繼續」才開始" : "馬上處理所有積欠的每日／月總結"
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
        let runSh = (settings.projectPath as NSString).appendingPathComponent("run.sh")
        guard FileManager.default.fileExists(atPath: runSh) else {
            lastError = "找不到 \(runSh)，請到「設定」確認專案資料夾"
            state = .stopped
            return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        p.arguments = [runSh]
        p.currentDirectoryURL = URL(fileURLWithPath: settings.projectPath)
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
            appendLog("— 啟動核心（\(settings.projectPath)）—")
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
               let obj = try? JSONDecoder().decode(CoreStatus.self, from: d) {
                if pendingAction != "restart" { pendingAction = nil }   // 核心回報新狀態 → 解鎖
                status = obj
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
