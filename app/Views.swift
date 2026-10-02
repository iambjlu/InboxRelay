// app/Views.swift
// 選單列內容、主視窗（儀表板／設定／日誌）。
import AppKit
import SwiftUI

struct StateBadge: View {
    @ObservedObject var core: CoreController

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 9, height: 9)
            Text(text).font(.headline)
        }
    }

    private var color: Color {
        switch core.state {
        case .running: return core.status.paused ? .orange : .green
        case .starting, .restarting: return .yellow
        case .stopped: return .red
        }
    }

    private var text: String {
        switch core.state {
        case .running: return core.status.paused ? "背景 AI 已暫停" : "運行中"
        case .starting: return "啟動中…"
        case .restarting(let s): return "核心停止，\(s) 秒後重試"
        case .stopped: return "已停止"
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

struct ControlButtons: View {
    @ObservedObject var core: CoreController
    var long = false      // 主視窗用較長的文字

    var body: some View {
        Button(core.recapButtonTitle) { core.recapNow() }
            .disabled(!core.canRecapNow)
            .help(core.recapHelp)
        if core.status.paused {
            Button(long ? "繼續背景 AI" : "繼續") { core.resume() }
                .disabled(!core.canPauseResume)
        } else {
            Button(long ? "暫停背景 AI" : "暫停") { core.pause() }
                .disabled(!core.canPauseResume)
                .help("暫停 AI 分析與總結（搬移照常）")
        }
    }
}

struct TaskRows: View {
    let tasks: [CoreTask]

    var body: some View {
        if tasks.isEmpty {
            Text("本地 AI 閒置").foregroundStyle(.secondary)
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
        VStack(alignment: .leading, spacing: 10) {
            StateBadge(core: core)
            if !core.status.chat_title.isEmpty {
                Text("投遞箱：\(core.status.chat_title)｜AI：\(core.status.ai_status)")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            TaskRows(tasks: core.status.tasks)
            Text(summaryLine).font(.caption).foregroundStyle(.secondary)
            if !core.lastError.isEmpty {
                Text(core.lastError).font(.caption).foregroundStyle(.red)
            }
            Divider()
            HStack { ControlButtons(core: core) }
            HStack {
                Button("開啟主視窗") {
                    NSApp.activate(ignoringOtherApps: true)
                    openWindow(id: "main")
                }
                switch core.state {
                case .stopped:
                    Button("啟動核心") { core.start() }
                case .restarting:
                    Button("立即重試") { core.restart() }
                default:
                    Button(core.state == .starting ? "啟動中…" : "重新啟動") {
                        confirmIfBusy(core, "重新啟動") { core.restart() }
                    }
                    .disabled(!core.canRestart)
                }
            }
            Divider()
            Button("結束 InboxRelay") { confirmIfBusy(core, "結束 InboxRelay") { NSApp.terminate(nil) } }
        }
        .padding(14)
        .frame(width: 340)
    }

    private var summaryLine: String {
        var parts: [String] = []
        if core.status.backlog > 0 { parts.append("待分析 \(core.status.backlog)") }
        if core.status.probe_pending > 0 { parts.append("待確認刪除 \(core.status.probe_pending)") }
        if !core.status.summarized_until.isEmpty { parts.append("總結到 \(core.status.summarized_until)") }
        if core.status.rss_mb > 0 { parts.append("記憶體 \(core.status.rss_mb)MB") }
        if let h = core.status.next_recap_hours {
            parts.append(String(format: "下次總結 %@（%.1f 小時後）", core.status.recap_time, h))
        }
        return parts.joined(separator: "｜")
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
                GroupBox("概況") {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 6) {
                        GridRow { Text("投遞箱"); Text(core.status.chat_title) }
                        GridRow { Text("本地 AI"); Text(core.status.ai_status) }
                        GridRow { Text("待分析"); Text("\(core.status.backlog) 則") }
                        GridRow { Text("待確認刪除"); Text("\(core.status.probe_pending) 則") }
                        GridRow { Text("每日總結完成到"); Text(core.status.summarized_until.isEmpty ? "—" : core.status.summarized_until) }
                        GridRow { Text("每天總結時間"); Text(core.status.recap_time) }
                        GridRow { Text("核心記憶體"); Text("\(core.status.rss_mb) MB") }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    ControlButtons(core: core, long: true)
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
    @State private var message = ""
    @State private var revealed: Set<String> = []     // 目前顯示明碼的欄位（關掉視窗就恢復隱藏）

    var body: some View {
        Form {
            Section("設定儲存位置") {
                Picker("儲存在", selection: $settings.storage) {
                    ForEach(SettingsStore.Storage.allCases) { Text($0.rawValue).tag($0) }
                }
                if settings.storage == .folder {
                    HStack {
                        TextField("資料夾（自己選，例如 iCloud Drive 裡的資料夾）", text: $settings.folderPath)
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
                TextField("專案資料夾（每台電腦各自設定）", text: $settings.projectPath)
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
                    ForEach(SettingCatalog.fields.filter { $0.section == sec }) { f in
                        fieldRow(f)
                    }
                }
            }
            Section {
                HStack {
                    Button("儲存並重新啟動核心") {
                        confirmIfBusy(core, "儲存並重新啟動") {
                            settings.save()
                            message = "已儲存（Token 存在鑰匙圈）"
                            if core.state == .stopped { core.start() } else { core.restart() }
                        }
                    }
                    .disabled(!core.canRestart)
                    .keyboardShortcut("s", modifiers: .command)
                    Button("從 .env 補齊空白欄位") {
                        let n = settings.importFromEnv()
                        message = "從 .env 補上 \(n) 個欄位"
                    }
                }
                if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.secondary) }
            }
        }
        .formStyle(.grouped)
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
            Stepper("\(f.label)：\(value.wrappedValue) \(unit)", value: value, in: range)
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
