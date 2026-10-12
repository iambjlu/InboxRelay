// app/InboxRelayApp.swift
// InboxRelay 選單列 App：在背景管理 Python 核心，選單列顯示狀態與進度。
import AppKit
import ServiceManagement
import SwiftUI

@main
struct InboxRelayApp: App {
    @StateObject private var settings: SettingsStore
    @StateObject private var core: CoreController

    init() {
        let s = SettingsStore()
        _settings = StateObject(wrappedValue: s)
        let c = CoreController(settings: s)
        _core = StateObject(wrappedValue: c)
        signal(SIGPIPE, SIG_IGN)                    // 核心結束後再寫 stdin 不要讓 App 當掉
        Task { @MainActor in c.start() }            // App 一開就啟動核心
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification,
                                               object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { c.stop() }
            Thread.sleep(forTimeInterval: 2)        // 給核心一點時間發「已離線」
        }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContent(core: core)
        } label: {
            MenuIcon(name: menuIcon)
        }
        .menuBarExtraStyle(.window)

        Window("InboxRelay", id: "main") {
            MainWindow(core: core, settings: settings)
                .frame(minWidth: 720, minHeight: 520)
                // 選單列 App 平常是 accessory（沒有 Dock 圖示、沒有左上角選單）；
                // 主視窗打開時暫時變成一般 App，關掉後再藏回去
                .onAppear {
                    NSApp.setActivationPolicy(.regular)
                    NSApp.activate(ignoringOtherApps: true)
                }
                .onDisappear {
                    NSApp.setActivationPolicy(.accessory)
                }
        }
        .defaultSize(width: 860, height: 640)
    }

    private var menuIcon: String {
        switch core.state {
        case .stopped: return "exclamationmark.triangle"
        case .starting, .restarting: return "arrow.triangle.2.circlepath"
        case .running:
            if core.status.paused { return "pause.circle" }
            return core.status.tasks.isEmpty ? "tray.full" : "tray.and.arrow.down"
        }
    }
}

/// 選單列圖示。App 一啟動它就會被建立，所以順便在這裡依「App 開啟時顯示主視窗」設定（預設開）打開主視窗，只做一次。
struct MenuIcon: View {
    let name: String
    @Environment(\.openWindow) private var openWindow
    @AppStorage(AppPrefs.showMainWindowKey) private var showMainWindow = true
    @State private var handled = false

    var body: some View {
        Image(systemName: name)
            .task {
                guard !handled else { return }
                handled = true
                if showMainWindow {
                    openWindow(id: "main")
                    NSApp.activate(ignoringOtherApps: true)
                }
            }
    }
}

/// 每台電腦各自的 App 介面偏好（存在 UserDefaults，不隨設定資料夾同步）。
enum AppPrefs {
    static let showMainWindowKey = "showMainWindowOnLaunch"
    static let diagnosticKey = "diagnosticLogging"          // 診斷模式：預設關閉＝日誌只顯示、不寫檔
}

/// 診斷日誌檔（只有「診斷模式」打開時才寫；平常日誌只留在記憶體的 500 行環狀緩衝）。
/// ~/Library/Logs/InboxRelay/diagnostic.log，超過 5 MB 就換成 diagnostic.1.log（只留一份舊的），所以最多約 10 MB。
enum DiagnosticLog {
    static let maxBytes = 5 * 1024 * 1024
    static var directory: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Logs/InboxRelay")
    }
    static var file: URL { directory.appendingPathComponent("diagnostic.log") }
    private static let queue = DispatchQueue(label: "tw.iambjlu.InboxRelay.diaglog", qos: .utility)
    private static var handle: FileHandle?

    static var enabled: Bool { UserDefaults.standard.bool(forKey: AppPrefs.diagnosticKey) }

    static func write(_ line: String) {
        guard enabled else { return }
        let stamp = ISO8601DateFormatter().string(from: Date())
        queue.async {
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                if handle == nil {
                    if !FileManager.default.fileExists(atPath: file.path) {
                        FileManager.default.createFile(atPath: file.path, contents: nil)
                    }
                    handle = try FileHandle(forWritingTo: file)
                }
                if let size = try handle?.seekToEnd(), size > UInt64(maxBytes) {
                    try handle?.close()
                    handle = nil
                    let old = directory.appendingPathComponent("diagnostic.1.log")
                    try? FileManager.default.removeItem(at: old)
                    try FileManager.default.moveItem(at: file, to: old)
                    FileManager.default.createFile(atPath: file.path, contents: nil)
                    handle = try FileHandle(forWritingTo: file)
                }
                try handle?.write(contentsOf: Data("\(stamp) \(line)\n".utf8))
            } catch {
                handle = nil                        // 寫不進去就算了（不影響 App）；下次再試
            }
        }
    }

    /// 關掉診斷模式時把檔案關起來
    static func close() {
        queue.async { try? handle?.close(); handle = nil }
    }
}

enum LoginItem {
    static var enabled: Bool { SMAppService.mainApp.status == .enabled }

    static func set(_ on: Bool) -> String? {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            return nil
        } catch {
            return "設定開機啟動失敗：\(error.localizedDescription)（App 需放在「應用程式」資料夾）"
        }
    }
}
