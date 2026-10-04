// app/SettingsStore.swift
// 設定保存：Token 存「鑰匙圈」，其他（ID、網址、開關）存 UserDefaults。第一次開啟會從專案的 .env 匯入。
import Foundation
import Security

struct Choice: Hashable {
    let value: String
    let label: String
}

enum FieldKind: Hashable {
    case text
    case secret
    case toggle(Bool)                       // 預設值
    case time(String)                       // 預設 "HH:MM"
    case choice([Choice], String)           // 選項、預設值
    case number(ClosedRange<Int>, Int, String)   // 範圍、預設值、單位
}

struct SettingField: Identifiable, Hashable {
    let key: String        // 對應核心的環境變數名稱
    let label: String
    let kind: FieldKind
    let section: String
    var id: String { key }
    var secret: Bool { kind == .secret }
}

enum SettingCatalog {
    static let fields: [SettingField] = [
        SettingField(key: "TG_BOT_TOKEN", label: "Telegram Bot Token", kind: .secret, section: "連線"),
        SettingField(key: "TG_INBOX_CHAT_ID", label: "投遞箱群組 ID", kind: .text, section: "連線"),
        SettingField(key: "TG_PROBE_CHAT_ID", label: "探測頻道 ID", kind: .text, section: "連線"),
        SettingField(key: "DC_BOT_TOKEN", label: "Discord Bot Token", kind: .secret, section: "連線"),
        SettingField(key: "DC_GUILD_ID", label: "伺服器 ID", kind: .text, section: "連線"),
        SettingField(key: "DC_CH_INBOX_MIRROR", label: "#投遞箱鏡像", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_RECAP", label: "#總結", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_SEARCH", label: "#搜尋", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_KEYWORDS", label: "#關鍵字庫", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_DB", label: "#資料庫", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_NOTIFY", label: "通知與互動", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_AI_TEST", label: "AI 測試（實驗用）", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_SEARCH_TEST", label: "搜尋測試", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_SCREENSHOT", label: "連結預覽與進度", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_EXTRA", label: "補充資料（發票、筆記…）", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_CHAT", label: "AI 對話", kind: .text, section: "頻道"),
        SettingField(key: "DC_CH_CHAT_PROMPT", label: "AI 對話提示詞與記憶", kind: .text, section: "頻道"),
        SettingField(key: "LOCAL_AI_SERVER", label: "OpenAI 相容端點", kind: .text, section: "外部 LLM 設定"),
        SettingField(key: "LOCAL_AI_MODEL", label: "模型名稱", kind: .text, section: "外部 LLM 設定"),
        SettingField(key: "LOCAL_AI_API_KEY", label: "API Key", kind: .secret, section: "外部 LLM 設定"),
        SettingField(key: "LOCAL_AI_IMAGE_SUPPORT", label: "模型支援圖片", kind: .toggle(true), section: "外部 LLM 設定"),
        SettingField(key: "LLM_CONTEXT", label: "Context window", kind: .choice([
            Choice(value: "8192", label: "8K（8,192）"), Choice(value: "16384", label: "16K（16,384）"),
            Choice(value: "32768", label: "32K（32,768）"), Choice(value: "65536", label: "64K（65,536）"),
            Choice(value: "131072", label: "128K（131,072）"), Choice(value: "262144", label: "256K（262,144）"),
            Choice(value: "524288", label: "512K（524,288）"), Choice(value: "1048576", label: "1M（1,048,576）")],
            "131072"), section: "外部 LLM 設定"),
        SettingField(key: "CHAT_COMPACT_PCT", label: "AI 對話自動壓縮門檻", kind: .number(30...98, 90, "%"),
                     section: "外部 LLM 設定"),
        SettingField(key: "LLM_SCHEDULE", label: "外部 LLM 工作時間", kind: .choice([Choice(value: "always", label: "永遠立即執行"),
                                                Choice(value: "daily", label: "每天自動開始到做完為止"),
                                                Choice(value: "window", label: "每天自動開始與暫停"),
                                                Choice(value: "manual", label: "手動執行")], "daily"), section: "排程"),
        SettingField(key: "RECAP_TIME", label: "外部 LLM 開始時間", kind: .time("03:30"), section: "排程"),
        SettingField(key: "LLM_END", label: "外部 LLM 暫停時間", kind: .time("08:00"), section: "排程"),
        SettingField(key: "ANE_SCHEDULE", label: "本機 Apple Neural Engine 工作時間", kind: .choice([Choice(value: "always", label: "永遠立即執行"),
                                                Choice(value: "daily", label: "每天自動開始到做完為止"),
                                                Choice(value: "window", label: "每天自動開始與暫停"),
                                                Choice(value: "manual", label: "手動執行")], "always"), section: "排程"),
        SettingField(key: "ANE_START", label: "本機 ANE 開始時間", kind: .time("03:30"), section: "排程"),
        SettingField(key: "ANE_END", label: "本機 ANE 暫停時間", kind: .time("08:00"), section: "排程"),
        SettingField(key: "LINK_SCREENSHOT", label: "連結截圖", kind: .toggle(false), section: "功能"),
        SettingField(key: "PRIVATE_URLS", label: "內網網址也截圖與摘要", kind: .toggle(true), section: "功能"),
        SettingField(key: "IGNORE_CERT_ERRORS", label: "憑證無效也強制開啟（截圖／摘要）", kind: .toggle(false), section: "功能"),
        SettingField(key: "REVERSE_SYNC", label: "Discord 鏡像頻道發文 → 同步到 Telegram", kind: .toggle(true), section: "功能"),
        SettingField(key: "EDIT_SYNC_HOURS", label: "編輯同步時限", kind: .number(1...168, 24, "小時"), section: "功能"),
        SettingField(key: "SEARCH_LIVE", label: "啟用 #搜尋 頻道", kind: .toggle(true), section: "功能"),
        SettingField(key: "SEARCH_THINKING", label: "搜尋時先思考（較準、較慢）", kind: .toggle(false), section: "功能"),
        SettingField(key: "HEADER_STYLE", label: "日期標頭樣式", kind: .choice([Choice(value: "large", label: "大標題"),
                                                                          Choice(value: "small", label: "小字")], "large"),
                     section: "功能"),
    ]
    static let sections = ["連線", "頻道", "外部 LLM 設定", "排程", "功能"]

    /// 依排程模式顯示時間欄位：開始時間只在「每天自動開始…」兩種模式；結束時間只在「每天自動開始與暫停」
    static func isVisible(_ f: SettingField, values: [String: String]) -> Bool {
        func mode(_ key: String, _ def: String) -> String {
            let v = values[key] ?? ""
            return v.isEmpty ? def : v
        }
        switch f.key {
        case "RECAP_TIME": return ["daily", "window"].contains(mode("LLM_SCHEDULE", "daily"))
        case "LLM_END": return mode("LLM_SCHEDULE", "daily") == "window"
        case "ANE_START": return ["daily", "window"].contains(mode("ANE_SCHEDULE", "always"))
        case "ANE_END": return mode("ANE_SCHEDULE", "always") == "window"
        default: return true
        }
    }
}

enum Keychain {
    static let service = "tw.iambjlu.InboxRelay"

    static func get(_ key: String) -> String? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: key,
                                kSecReturnData as String: true,
                                kSecMatchLimit as String: kSecMatchLimitOne]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    static func set(_ key: String, _ value: String) {
        let base: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrService as String: service,
                                   kSecAttrAccount as String: key]
        SecItemDelete(base as CFDictionary)
        if value.isEmpty { return }
        var add = base
        add[kSecValueData as String] = Data(value.utf8)
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }
}

/// 設定檔（同步資料夾模式）：settings.json
struct SettingsFile: Codable {
    var version: Int = 1
    var updated: String = ""
    var updatedBy: String = ""
    var values: [String: String] = [:]
    var secrets: [String: String]? = nil      // 只有「Token 也存到資料夾」打開時才有
}

final class SettingsStore: ObservableObject {
    enum Storage: String, CaseIterable, Identifiable {
        case local
        case folder
        var id: String { rawValue }
        var label: String { self == .local ? "本機" : "指定資料夾" }

        /// 也認得舊版存的中文值
        static func from(_ raw: String?) -> Storage {
            guard let r = raw else { return .local }
            if r == "folder" || r.hasPrefix("指定資料夾") { return .folder }
            return .local
        }
    }

    @Published var values: [String: String] = [:]
    @Published var projectPath: String            // 每台電腦各自設定，不同步
    @Published var storage: Storage
    @Published var folderPath: String
    @Published var tokensInFolder: Bool
    @Published var lastSyncNote: String = ""

    private let defaults = UserDefaults.standard

    init() {
        let embedded = Bundle.main.object(forInfoDictionaryKey: "InboxRelayProjectPath") as? String
        projectPath = defaults.string(forKey: "projectPath")
            ?? embedded
            ?? (NSHomeDirectory() + "/Downloads/InboxRelay")
        storage = Storage.from(defaults.string(forKey: "storage"))
        folderPath = defaults.string(forKey: "folderPath") ?? ""      // 由使用者自己選，不預設
        // Token 預設也寫進設定資料夾（使用者指定）；可在設定頁關掉
        tokensInFolder = defaults.object(forKey: "tokensInFolder") as? Bool ?? true
        load()
        if !defaults.bool(forKey: "importedFromEnv") {
            importFromEnv()
        }
    }

    var settingsFileURL: URL {
        URL(fileURLWithPath: folderPath).appendingPathComponent("settings.json")
    }

    private func host() -> String {
        Host.current().localizedName ?? ProcessInfo.processInfo.hostName
    }

    // MARK: - 讀取

    func load() {
        var v: [String: String] = [:]
        var fromFile: SettingsFile? = nil
        if storage == .folder, !folderPath.isEmpty {
            fromFile = readFile()
        }
        for f in SettingCatalog.fields {
            if f.secret {
                if storage == .folder, tokensInFolder, let s = fromFile?.secrets?[f.key], !s.isEmpty {
                    v[f.key] = s
                } else {
                    v[f.key] = Keychain.get(f.key) ?? ""
                }
            } else if storage == .folder, let file = fromFile {
                v[f.key] = file.values[f.key] ?? ""
            } else {
                v[f.key] = defaults.string(forKey: "env." + f.key) ?? ""
            }
        }
        values = v
        if storage == .folder && folderPath.isEmpty {
            lastSyncNote = "請先選擇要存放設定的資料夾"
        } else if storage == .folder {
            lastSyncNote = fromFile.map { "已讀取 \(settingsFileURL.path)（\($0.updated)，來自 \($0.updatedBy)）" }
                ?? "資料夾裡還沒有 settings.json，按「儲存」就會建立"
        }
    }

    private func readFile() -> SettingsFile? {
        guard let data = try? Data(contentsOf: settingsFileURL) else { return nil }
        return try? JSONDecoder().decode(SettingsFile.self, from: data)
    }

    // MARK: - 儲存

    func save() {
        defaults.set(projectPath, forKey: "projectPath")
        defaults.set(storage.rawValue, forKey: "storage")
        defaults.set(folderPath, forKey: "folderPath")
        defaults.set(tokensInFolder, forKey: "tokensInFolder")

        var plain: [String: String] = [:]
        var secrets: [String: String] = [:]
        for f in SettingCatalog.fields {
            let val = (values[f.key] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if f.secret {
                Keychain.set(f.key, val)                 // 本機鑰匙圈一律保留一份
                secrets[f.key] = val
            } else {
                plain[f.key] = val
                defaults.set(val, forKey: "env." + f.key)  // 本機也留一份，切回本機模式不會空掉
            }
        }
        guard storage == .folder else { return }
        guard !folderPath.isEmpty else {
            lastSyncNote = "還沒選擇資料夾，設定先存在本機"
            return
        }
        do {
            try FileManager.default.createDirectory(atPath: folderPath, withIntermediateDirectories: true)
            let fmt = ISO8601DateFormatter()
            let file = SettingsFile(updated: fmt.string(from: Date()), updatedBy: host(), values: plain,
                                    secrets: tokensInFolder ? secrets : nil)
            let enc = JSONEncoder()
            enc.outputFormatting = [.prettyPrinted, .sortedKeys]
            try enc.encode(file).write(to: settingsFileURL, options: .atomic)
            lastSyncNote = "已儲存到 \(settingsFileURL.path)"
        } catch {
            lastSyncNote = "寫入設定檔失敗：\(error.localizedDescription)"
        }
    }

    /// 讀專案的 .env，把還沒設定的欄位補上。
    @discardableResult
    func importFromEnv() -> Int {
        let path = (projectPath as NSString).appendingPathComponent(".env")
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return 0 }
        var count = 0
        for raw in text.components(separatedBy: .newlines) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            var val = String(line[line.index(after: eq)...]).trimmingCharacters(in: .whitespaces)
            val = val.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            guard SettingCatalog.fields.contains(where: { $0.key == key }), !val.isEmpty else { continue }
            if (values[key] ?? "").isEmpty {
                values[key] = val
                count += 1
            }
        }
        save()
        defaults.set(true, forKey: "importedFromEnv")
        return count
    }

    /// 給核心的環境變數（App 設定優先；沒設定的由核心自己讀 .env）。
    func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        env["LANG"] = "zh_TW.UTF-8"
        env["PYTHONIOENCODING"] = "utf-8"
        env["INBOXRELAY_IPC"] = "1"
        env["STATUS_BAR"] = "false"
        for (k, v) in values where !v.isEmpty {
            env[k] = v
        }
        return env
    }
}
