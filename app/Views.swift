// app/Views.swift
// 選單列內容、主視窗（儀表板／設定／日誌）。
import AppKit
import SwiftUI

extension CoreController {
    var stateColor: Color {
        switch state {
        case .running: return status.paused ? .orange : .green
        case .starting, .restarting: return .yellow
        case .stopped: return .red
        }
    }

    var stateLabel: String {
        switch state {
        case .running: return status.paused ? "已暫停" : "運行中"
        case .starting:
            if let st = startup, st.step > 0 { return "啟動中 \(st.step)/\(st.steps)：\(st.stage)" }
            return "啟動中…"
        case .restarting(let s): return "\(s) 秒後重試"
        case .stopped: return "已停止"
        }
    }
}

/// 小膠囊標籤（狀態用）
@MainActor
func confirmRecapReset(_ core: CoreController) {
    let alert = NSAlert()
    alert.messageText = "清空 #總結 並重新總結？"
    alert.informativeText = "會刪掉 #總結 裡所有每日／月／年總結。之後等外部 LLM 把分析全部做完，"
        + "再依日期順序一次重新發完。你在通知頻道給過的總結修正要求會保留。"
    alert.alertStyle = .critical
    alert.addButton(withTitle: "取消")                       // 預設按鈕＝取消（按 Enter 不會誤清）
    let go = alert.addButton(withTitle: "清空並重新總結")
    go.hasDestructiveAction = true
    go.keyEquivalent = ""
    NSApp.activate(ignoringOtherApps: true)
    if alert.runModal() == .alertSecondButtonReturn { core.recapReset() }
}

struct StatusPill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .foregroundStyle(color)
            .background(color.opacity(0.16), in: Capsule())
    }
}

struct StateBadge: View {
    @ObservedObject var core: CoreController

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Circle().fill(core.stateColor).frame(width: 9, height: 9)
                Text(core.stateLabel).font(.headline)
            }
            if core.state == .starting, let text = core.startupText {
                // 啟動要花幾分鐘（尤其是讀 #關鍵字庫）：直接顯示做到哪，不用看日誌
                if let f = core.startupFraction {
                    ProgressView(value: f).progressViewStyle(.linear)
                } else {
                    ProgressView().progressViewStyle(.linear)
                }
                Text(text).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}

/// 有總結或 AI 工作在跑時，重新啟動／結束前先問一下（會中斷目前的工作，下次啟動會從頭補做）
@MainActor
func confirmIfBusy(_ core: CoreController, _ what: String, then action: () -> Void) {
    guard core.isBusy else { action(); return }
    let alert = NSAlert()
    alert.messageText = "確定要\(what)嗎？"
    let doing = core.recapBusy ? "每日總結正在進行" : "背景 AI 正在工作"
    alert.informativeText = "\(doing)，\(what)會中斷它；沒做完的部分下次啟動會再補做。"
    alert.addButton(withTitle: what)
    alert.addButton(withTitle: "取消")
    NSApp.activate(ignoringOtherApps: true)
    if alert.runModal() == .alertFirstButtonReturn { action() }
}

/// 一條背景工作（外部 LLM／本機 ANE）的卡片：圖示、狀態、待處理、立即執行、暫停／繼續
struct LaneRow: View {
    @ObservedObject var core: CoreController
    let name: String          // llm / ane / shot
    let title: String

    private var icon: String { name == "llm" ? "brain" : name == "shot" ? "camera.viewfinder" : "text.viewfinder" }

    var body: some View {
        let l = core.lane(name)
        let tint = color(l)
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 30, height: 30)
                .background(tint.opacity(0.15), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(title).font(.callout.weight(.semibold)).lineLimit(1)
                    StatusPill(text: pill(l), color: tint)
                }
                Text(detail(l)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 6)
            HStack(spacing: 2) {
                Button { core.laneRun(name) } label: {
                    Image(systemName: l?.queued == true ? "hourglass" : "bolt.fill").frame(width: 22, height: 22)
                }
                .help(core.runTitle(name) + "｜" + core.runHelp(name))
                .disabled(!core.canRun(name))
                Button { core.laneToggle(name) } label: {
                    Image(systemName: l?.state == "paused" ? "play.fill" : "pause.fill").frame(width: 22, height: 22)
                }
                .help(core.toggleHelp(name))
                .disabled(!core.canToggle(name))
            }
            .buttonStyle(.borderless)
            .font(.system(size: 13))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
    }

    private func color(_ l: LaneStatus?) -> Color {
        switch l?.state {
        case "running": return (l?.backlog ?? 0) > 0 ? .green : .teal
        case "waiting": return .blue
        case "offline": return .red
        case "paused": return .orange
        default: return .gray
        }
    }

    private func pill(_ l: LaneStatus?) -> String {
        switch l?.state {
        case "running": return (l?.backlog ?? 0) > 0 ? "執行中" : "待命"
        case "waiting": return "等待"
        case "offline": return "LLM 離線"
        case "paused": return "已暫停"
        case "unavailable": return "連線中"
        default: return "—"
        }
    }

    private func detail(_ l: LaneStatus?) -> String {
        guard let l = l else { return "核心啟動後顯示" }
        let left = "待處理 \(l.backlog.formatted())"
        switch l.state {
        case "waiting":
            return (l.next_start.isEmpty ? l.describe : "\(l.next_start) 開始") + " · " + left
        case "offline":
            return (l.next_start.isEmpty ? "連不上，下次開始時間再試" : "連不上，\(l.next_start) 再試") + " · " + left
        case "paused":
            return left + " · 按 ▶︎ 繼續"
        default:
            return l.backlog > 0 ? left + " · " + l.describe : l.describe
        }
    }
}

struct TaskRows: View {
    let tasks: [CoreTask]

    var body: some View {
        if tasks.isEmpty {
            Text("目前沒有背景工作").foregroundStyle(.secondary)
        } else {
            ForEach(tasks, id: \.self) { t in
                VStack(alignment: .leading, spacing: 2) {
                    Text(t.line).font(.callout).lineLimit(2)
                    if let p = t.progress {
                        ProgressView(value: min(max(p, 0), 1))
                    } else {
                        ProgressView().controlSize(.small)
                    }
                }
            }
        }
    }
}

struct MenuContent: View {
    @ObservedObject var core: CoreController
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // 標題列
            HStack(spacing: 10) {
                Image(systemName: "tray.and.arrow.down.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 32, height: 32)
                    .background(LinearGradient(colors: [.blue, .cyan], startPoint: .bottom, endPoint: .top),
                                in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                VStack(alignment: .leading, spacing: 1) {
                    Text("InboxRelay").font(.headline)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                StatusPill(text: core.stateLabel, color: core.stateColor)
            }

            // 目前工作（有才顯示）
            if !core.status.tasks.isEmpty {
                VStack(alignment: .leading, spacing: 8) { TaskRows(tasks: core.status.tasks) }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            }

            // 背景工作
            VStack(alignment: .leading, spacing: 6) {
                Text("背景工作").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                LaneRow(core: core, name: "llm", title: "外部 LLM")
                LaneRow(core: core, name: "ane", title: "本機 ANE")
                if core.lane("shot") != nil { LaneRow(core: core, name: "shot", title: "連結截圖") }
            }

            if !summaryLine.isEmpty {
                Text(summaryLine).font(.caption).foregroundStyle(.secondary)
            }
            if !core.lastError.isEmpty {
                Label(core.lastError, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption).foregroundStyle(.red)
            }

            Divider()

            // 底部動作
            HStack(spacing: 14) {
                Button {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                } label: { Label("主視窗", systemImage: "macwindow") }
                switch core.state {
                case .stopped:
                    Button { core.start() } label: { Label("啟動核心", systemImage: "play.circle") }
                case .restarting:
                    Button { core.restart() } label: { Label("立即重試", systemImage: "arrow.clockwise") }
                default:
                    Button { confirmIfBusy(core, "重新啟動") { core.restart() } } label: {
                        Label("重新啟動", systemImage: "arrow.clockwise")
                    }
                    .disabled(!core.canRestart)
                }
                Spacer()
                Button { confirmIfBusy(core, "結束 InboxRelay") { NSApp.terminate(nil) } } label: {
                    Label("結束", systemImage: "power")
                }
            }
            .buttonStyle(.borderless)
            .font(.callout)
        }
        .padding(14)
        .frame(width: 360)
    }

    private var subtitle: String {
        if core.status.chat_title.isEmpty { return core.state == .running ? "已連線" : "正在啟動核心…" }
        return "\(core.status.chat_title) · AI \(core.status.ai_status)"
    }

    private var summaryLine: String {
        var parts: [String] = []
        if !core.status.summarized_until.isEmpty { parts.append("總結到 \(core.status.summarized_until)") }
        if core.status.probe_pending > 0 { parts.append("待確認刪除 \(core.status.probe_pending)") }
        if core.status.rss_mb > 0 { parts.append("記憶體 \(core.status.rss_mb) MB") }
        return parts.joined(separator: " · ")
    }
}

struct MainWindow: View {
    @ObservedObject var core: CoreController
    @ObservedObject var settings: SettingsStore

    var body: some View {
        TabView {
            DashboardView(core: core)
                .tabItem { Label("儀表板", systemImage: "gauge") }
            SettingsView(core: core, settings: settings)
                .tabItem { Label("設定", systemImage: "gearshape") }
            LogView(core: core)
                .tabItem { Label("日誌", systemImage: "text.alignleft") }
        }
        .padding()
    }
}

struct DashboardView: View {
    @ObservedObject var core: CoreController

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                StateBadge(core: core)
                GroupBox("目前工作") {
                    VStack(alignment: .leading, spacing: 8) { TaskRows(tasks: core.status.tasks) }
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("背景工作") {
                    VStack(alignment: .leading, spacing: 10) {
                        LaneRow(core: core, name: "llm", title: "外部 LLM（分析、總結、記憶）")
                        Divider()
                        LaneRow(core: core, name: "ane", title: "本機 ANE（圖片文字辨識）")
                        if core.lane("shot") != nil {
                            Divider()
                            LaneRow(core: core, name: "shot", title: "連結截圖")
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                GroupBox("概況") {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                        GridRow { Text("投遞箱"); Text(core.status.chat_title) }
                        GridRow { Text("外部 LLM"); Text(core.status.ai_status) }
                        GridRow { Text("待分析"); Text("\(core.status.backlog) 則") }
                        GridRow { Text("待確認刪除"); Text("\(core.status.probe_pending) 則") }
                        GridRow { Text("每日總結完成到"); Text(core.status.summarized_until.isEmpty ? "—" : core.status.summarized_until) }
                        GridRow { Text("每日總結時間"); Text(core.status.recap_time) }
                        GridRow { Text("核心記憶體"); Text("\(core.status.rss_mb) MB") }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    Button(core.state == .stopped ? "啟動核心" : "重新啟動核心") {
                        if core.state == .stopped { core.start() } else { confirmIfBusy(core, "重新啟動") { core.restart() } }
                    }
                    .disabled(!core.canRestart)
                }
                if !core.lastError.isEmpty {
                    Text(core.lastError).foregroundStyle(.red)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct SettingsView: View {
    @ObservedObject var core: CoreController
    @ObservedObject var settings: SettingsStore
    @State private var loginItem = LoginItem.enabled
    @AppStorage(AppPrefs.showMainWindowKey) private var showMainWindow = true
    @State private var message = ""
    @State private var revealed: Set<String> = []     // 目前顯示明碼的欄位（關掉視窗就恢復隱藏）

    var body: some View {
        Form {
            Section("設定儲存位置") {
                Picker("儲存在", selection: $settings.storage) {
                    ForEach(SettingsStore.Storage.allCases) { Text($0.label).tag($0) }
                }
                if settings.storage == .folder {
                    HStack {
                        TextField("資料夾", text: $settings.folderPath)
                        Button("選擇…") { chooseFolder() }
                    }
                    Toggle("Token 也存到這個資料夾", isOn: $settings.tokensInFolder)
                    if settings.tokensInFolder {
                        Text("⚠️ Token 會以明碼寫在 settings.json，並由 iCloud 同步到你所有登入同一個 Apple ID 的電腦。")
                            .font(.caption).foregroundStyle(.orange)
                    } else {
                        Text("Token 只存在這台 Mac 的鑰匙圈；換電腦時要在那台貼一次。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Button("重新讀取資料夾裡的設定") { settings.load() }
                }
                if !settings.lastSyncNote.isEmpty {
                    Text(settings.lastSyncNote).font(.caption).foregroundStyle(.secondary)
                }
                Text("多台電腦共用設定時，同一時間只會有一台在運行；那台停掉約 3 分鐘後，其他台會自動接手。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("一般") {
                Toggle("App 開啟時顯示主視窗", isOn: $showMainWindow)
                    .help("關閉的話，App 開啟後只會出現在選單列，要從選單列的「主視窗」才打開")
                Toggle("登入時自動啟動 InboxRelay", isOn: $loginItem)
                    .onChange(of: loginItem) { _, on in
                        if let err = LoginItem.set(on) {
                            message = err
                            loginItem = LoginItem.enabled
                        }
                    }
            }
            ForEach(SettingCatalog.sections, id: \.self) { sec in
                Section(sec) {
                    ForEach(SettingCatalog.fields.filter {
                        $0.section == sec && SettingCatalog.isVisible($0, values: settings.values)
                    }) { f in
                        fieldRow(f)
                    }
                    if sec == "外部 LLM 設定" { llmTestRow }
                }
            }
            Section {
                HStack {
                    Button("儲存並重新啟動核心") {
                        confirmIfBusy(core, "儲存並重新啟動") {
                            settings.save()
                            message = settings.storage == .folder && settings.tokensInFolder
                                ? "已儲存（Token 也寫進設定資料夾）" : "已儲存（Token 存在鑰匙圈）"
                            if core.state == .stopped { core.start() } else { core.restart() }
                        }
                    }
                    .disabled(!core.canRestart)
                    .keyboardShortcut("s", modifiers: .command)
                    Spacer()
                    Button(role: .destructive) { confirmRecapReset(core) } label: {
                        Label("清空並重新總結", systemImage: "arrow.counterclockwise")
                    }
                    .disabled(!(core.isRunning && core.status.recap_enabled))
                    .help("刪掉 #總結 裡所有總結，等分析全部做完後依日期重新總結（會先跳確認視窗）")
                }
                if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .formStyle(.grouped)
    }

    /// 測試外部 LLM 連線：用上面目前填的值（不必先儲存），結果顯示在按鈕旁
    private var llmTestRow: some View {
        HStack(spacing: 8) {
            Button { core.testLLM() } label: {
                Label(core.llmTesting ? "測試中…" : "測試連線", systemImage: "bolt.horizontal")
            }
            .disabled(core.llmTesting)
            .help("用目前填的端點、模型、API Key 連線一次並請模型回一句話，不需要先儲存")
            if let r = core.llmTestResult {
                Image(systemName: r.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(r.ok ? .green : .red)
                Text(r.message).font(.caption).foregroundStyle(r.ok ? Color.secondary : Color.red)
                    .textSelection(.enabled)
            }
            Spacer()
        }
    }

    @ViewBuilder
    private func fieldRow(_ f: SettingField) -> some View {
        switch f.kind {
        case .secret:
            HStack {
                if revealed.contains(f.key) {
                    TextField(f.label, text: binding(f.key))
                } else {
                    SecureField(f.label, text: binding(f.key))
                }
                Button {
                    if revealed.contains(f.key) { revealed.remove(f.key) } else { revealed.insert(f.key) }
                } label: {
                    Image(systemName: revealed.contains(f.key) ? "eye.slash" : "eye")
                }
                .buttonStyle(.borderless)
                .help(revealed.contains(f.key) ? "隱藏" : "顯示")
            }
        case .text:
            TextField(f.label, text: binding(f.key))
        case .toggle(let def):
            Toggle(f.label, isOn: Binding(
                get: { boolValue(settings.values[f.key], def) },
                set: { settings.values[f.key] = $0 ? "true" : "false" }))
        case .time(let def):
            DatePicker(f.label, selection: Binding(
                get: { timeDate(settings.values[f.key], def) },
                set: { settings.values[f.key] = timeString($0) }),
                displayedComponents: .hourAndMinute)
        case .choice(let options, let def):
            Picker(f.label, selection: Binding(
                get: { (settings.values[f.key] ?? "").isEmpty ? def : settings.values[f.key]! },
                set: { settings.values[f.key] = $0 })) {
                ForEach(options, id: \.self) { Text($0.label).tag($0.value) }
            }
        case .number(let range, let def, let unit):
            let value = Binding<Int>(
                get: { Int(settings.values[f.key] ?? "").map { min(max($0, range.lowerBound), range.upperBound) } ?? def },
                set: { settings.values[f.key] = String($0) })
            LabeledContent(f.label) {
                HStack(spacing: 6) {
                    Text("\(value.wrappedValue) \(unit)").monospacedDigit()
                    Stepper("", value: value, in: range).labelsHidden()
                }
            }
        }
    }

    private func boolValue(_ raw: String?, _ def: Bool) -> Bool {
        guard let r = raw?.lowercased(), !r.isEmpty else { return def }
        return ["1", "true", "yes", "on"].contains(r)
    }

    private func timeDate(_ raw: String?, _ def: String) -> Date {
        let text = (raw ?? "").isEmpty ? def : raw!
        let parts = text.split(separator: ":").compactMap { Int($0) }
        var c = Calendar.current.dateComponents([.year, .month, .day], from: Date())
        c.hour = parts.count > 0 ? parts[0] : 3
        c.minute = parts.count > 1 ? parts[1] : 30
        return Calendar.current.date(from: c) ?? Date()
    }

    private func timeString(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = URL(fileURLWithPath: settings.folderPath.isEmpty
                                 ? NSHomeDirectory() + "/Library/Mobile Documents/com~apple~CloudDocs"
                                 : settings.folderPath)
        panel.message = "選擇存放 InboxRelay 設定的資料夾（放在 iCloud Drive 裡就能在多台電腦同步）"
        if panel.runModal() == .OK, let url = panel.url {
            settings.folderPath = url.path
        }
    }

    private func binding(_ key: String) -> Binding<String> {
        Binding(get: { settings.values[key] ?? "" }, set: { settings.values[key] = $0 })
    }
}

struct LogView: View {
    @ObservedObject var core: CoreController

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 2) {
                    ForEach(Array(core.logs.enumerated()), id: \.offset) { i, line in
                        Text(line).font(.system(.caption, design: .monospaced)).textSelection(.enabled).id(i)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: core.logs.count) { _, n in
                if n > 0 { proxy.scrollTo(n - 1, anchor: .bottom) }
            }
        }
    }
}
