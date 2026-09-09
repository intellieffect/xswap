import AppKit

// MARK: - Localization
//
// The menu bar app is launched from Finder/Dock/a login item, whose
// environment usually carries no XSWAP_LANG/LANG at all (only PATH is
// patched below for the subprocess call). It must not rely on the `xswap`
// subprocess's own locale guess — it resolves language itself, the same
// way `resolve_lang()` in xswap_display.py does, and passes `--lang`
// explicitly to the subprocess so the JSON text always matches the chrome.
//
// Every Hangul-bearing string in this file lives in the STRINGS table
// below; nothing outside this block may contain Hangul.
// L10N_TABLE_BEGIN
let STRINGS: [String: [String: String]] = [
    "en": [
        "header": "xswap · weekly usage",
        "legend": "█ used  ░ left · selected = default for new runs",
        "selected_badge": "  · selected",
        "exhausted_badge": "  · limit reached",
        "no_sessions": "no auto-switch sessions running",
        "running_session": "running: %@ · %d sessions",
        "checked_refreshes": "checked %@ · refreshes every 5 min",
        "loading": "loading usage…",
        "check_failed": "check failed · verify the CLI is installed and signed in",
        "refresh_stale": "refresh failed · showing the last successful check",
        "refreshing": "refreshing…",
        "refresh": "Refresh",
        "quit": "Quit menu bar",
        "status_used": "XS %d%% used",
        "status_checking": "XS · checking",
        "status_needs_check": "XS · needs check",
        "tooltip": "%@ · weekly %@ (default for new runs)",
    ],
    "ko": [
        "header": "xswap · 주간 사용량",
        "legend": "█ 사용  ░ 남음 · 선택됨 = 새 실행 기본 계정",
        "selected_badge": "  · 선택됨",
        "exhausted_badge": "  · 한도 도달",
        "no_sessions": "실행 중인 자동전환 세션 없음",
        "running_session": "실행 중: %@ · %d개 세션",
        "checked_refreshes": "조회 %@ · 5분마다 갱신",
        "loading": "사용량을 불러오는 중…",
        "check_failed": "조회 실패 · CLI 설치와 로그인을 확인하세요",
        "refresh_stale": "갱신 실패 · 표시된 수치는 마지막 조회 결과",
        "refreshing": "새로고침 중…",
        "refresh": "새로고침",
        "quit": "메뉴바 종료",
        "status_used": "XS %d%% 사용",
        "status_checking": "XS · 조회 중",
        "status_needs_check": "XS · 확인 필요",
        "tooltip": "%@ · 주간 %@ (새 실행 기본 계정)",
    ],
]
// L10N_TABLE_END

/// "en" unless XSWAP_LANG starts with "ko", or (XSWAP_LANG unset) LC_ALL/LANG
/// starts with "ko". Mirrors `resolve_lang()` in xswap_display.py exactly,
/// since this process's own environment (not the subprocess's) is what a
/// Finder/Dock/login-item launch actually carries.
func resolveMenuLang(_ environment: [String: String]) -> String {
    if let xswapLang = environment["XSWAP_LANG"], !xswapLang.isEmpty {
        return xswapLang.lowercased().hasPrefix("ko") ? "ko" : "en"
    }
    for key in ["LC_ALL", "LANG"] {
        if let value = environment[key], !value.isEmpty {
            return value.lowercased().hasPrefix("ko") ? "ko" : "en"
        }
    }
    return "en"
}

let menuLang = resolveMenuLang(ProcessInfo.processInfo.environment)

func t(_ key: String) -> String {
    STRINGS[menuLang]?[key] ?? STRINGS["en"]![key]!
}

struct Account: Decodable {
    let name: String
    let selected: Bool
    let remaining: Double?
    let bar: String
    let summary: String
    let reset: String
    let tone: String
    let exhausted: Bool
}
struct Session: Decodable { let account: String; let surface: String }
struct Snapshot: Decodable {
    let accounts: [Account]
    let policy: String
    let sessions: [Session]
    let updatedAt: Double
}

final class MenuApp: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!
    var snapshot: Snapshot?
    var busy = false
    var failed = false
    var timer: Timer?
    var watcher: DispatchSourceFileSystemObject?
    var stateSignature = ""
    let executable: String
    init(_ executable: String) { self.executable = executable }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = t("status_checking")
        rebuild()
        stateSignature = currentStateSignature()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refresh() }
        watchState()
    }

    // MARK: - Immediate refresh on xswap state changes
    //
    // `xswap login`, `use`, and `auto-*` rewrite files in the xswap root (usage
    // cache, registry, auto settings). Watching that directory lets the menu
    // follow a re-login within a second instead of waiting for the 5-minute
    // timer. Only these files count; the watch compares their modification
    // times so unrelated writes in the directory do nothing. Writes made by
    // this app's own `dashboard` call happen while `busy` is set and are
    // absorbed when the refresh completes, so no refresh loop can start.
    var stateRoot: String {
        let env = ProcessInfo.processInfo.environment
        if let home = env["CODEX_SWAP_HOME"], !home.isEmpty { return (home as NSString).expandingTildeInPath }
        return NSHomeDirectory() + "/.local/share/codex-swap"
    }

    func currentStateSignature() -> String {
        ["usage-cache.json", "accounts.json", "auto.json"].map { name -> String in
            let path = stateRoot + "/" + name
            let date = (try? FileManager.default.attributesOfItem(atPath: path)[.modificationDate]) as? Date
            return name + "=" + String(date?.timeIntervalSince1970 ?? 0)
        }.joined(separator: ";")
    }

    func watchState() {
        let fd = open(stateRoot, O_EVTONLY)
        guard fd >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: [.write], queue: .main)
        source.setEventHandler { [weak self] in self?.stateChanged() }
        source.setCancelHandler { close(fd) }
        source.resume()
        watcher = source
    }

    func stateChanged() {
        guard !busy else { return }
        let signature = currentStateSignature()
        guard signature != stateSignature else { return }
        stateSignature = signature
        refresh()
    }

    func line(_ title: String, color: NSColor? = nil, mono: Bool = false) -> NSMenuItem {
        let entry = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        if let color = color {
            entry.attributedTitle = NSAttributedString(string: title, attributes: [
                .foregroundColor: color,
                .font: mono ? NSFont.monospacedSystemFont(ofSize: 12, weight: .medium) : NSFont.systemFont(ofSize: 13)
            ])
        }
        return entry
    }

    func rebuild() {
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.addItem(line(t("header")))
        menu.addItem(line(t("legend")))
        menu.addItem(.separator())
        if let data = snapshot {
            for account in data.accounts {
                let badge = account.selected ? t("selected_badge") : account.exhausted ? t("exhausted_badge") : ""
                menu.addItem(line((account.selected ? "▸ " : "") + account.name + badge))
                let color: NSColor = account.tone == "red" ? .systemRed : account.tone == "yellow" ? .systemOrange : account.tone == "green" ? .systemGreen : .secondaryLabelColor
                if !account.bar.isEmpty { menu.addItem(line(account.bar, color: color, mono: true)) }
                menu.addItem(line(account.summary, color: color))
                if !account.reset.isEmpty { menu.addItem(line(account.reset)) }
                menu.addItem(.separator())
            }
            menu.addItem(line(data.policy))
            if data.sessions.isEmpty {
                menu.addItem(line(t("no_sessions")))
            } else {
                for name in Set(data.sessions.map { $0.account }).sorted() {
                    let count = data.sessions.filter { $0.account == name }.count
                    menu.addItem(line(String(format: t("running_session"), name, count)))
                }
            }
            let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
            menu.addItem(line(String(format: t("checked_refreshes"), formatter.string(from: Date(timeIntervalSince1970: data.updatedAt)))))
        } else {
            menu.addItem(line(failed ? t("check_failed") : t("loading")))
        }
        if failed && snapshot != nil { menu.addItem(line(t("refresh_stale"), color: .systemOrange)) }
        menu.addItem(.separator())
        let refreshItem = NSMenuItem(title: busy ? t("refreshing") : t("refresh"), action: #selector(refresh), keyEquivalent: "r")
        refreshItem.target = self; refreshItem.isEnabled = !busy
        menu.addItem(refreshItem)
        let quit = NSMenuItem(title: t("quit"), action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self; menu.addItem(quit)
        item.menu = menu
        if let account = snapshot?.accounts.first(where: { $0.selected }), let remaining = account.remaining {
            item.button?.title = String(format: t("status_used"), Int((100 - remaining).rounded())) + (failed ? " !" : "")
            item.button?.toolTip = String(format: t("tooltip"), account.name, account.summary)
        } else { item.button?.title = busy ? t("status_checking") : t("status_needs_check") }
    }

    @objc func refresh() {
        guard !busy else { return }
        busy = true; rebuild()
        DispatchQueue.global(qos: .utility).async { [self] in
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = ["dashboard", "--lang", menuLang]
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            // Persisted app launches may have a smaller PATH than the invoking shell.
            var env = ProcessInfo.processInfo.environment
            env["PATH"] = URL(fileURLWithPath: executable).deletingLastPathComponent().path + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + (env["PATH"] ?? "")
            process.environment = env
            var result: Snapshot?
            do {
                try process.run()
                let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
                DispatchQueue.global().asyncAfter(deadline: .now() + 60, execute: timeout)
                let bytes = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit(); timeout.cancel()
                if process.terminationStatus == 0 { result = try? JSONDecoder().decode(Snapshot.self, from: bytes) }
            } catch { }
            let received = result
            DispatchQueue.main.async { [self] in
                busy = false; failed = received == nil
                if let received = received { snapshot = received }
                stateSignature = currentStateSignature()
                rebuild()
            }
        }
    }
    @objc func quitApp() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let supplied = CommandLine.arguments.dropFirst().first
let saved = UserDefaults.standard.string(forKey: "xswapExecutable")
let executable = supplied ?? saved ?? NSHomeDirectory() + "/.local/bin/xswap"
if let supplied = supplied { UserDefaults.standard.set(supplied, forKey: "xswapExecutable") }
let delegate = MenuApp(executable)
app.delegate = delegate
app.run()
