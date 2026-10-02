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
            Image(systemName: menuIcon)
        }
        .menuBarExtraStyle(.window)

        Window("InboxRelay", id: "main") {
            MainWindow(core: core, settings: settings)
                .frame(minWidth: 720, minHeight: 520)
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
