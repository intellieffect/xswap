import AppKit

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
    let executable: String
    init(_ executable: String) { self.executable = executable }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "XS · 조회 중"
        rebuild()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 300, repeats: true) { [weak self] _ in self?.refresh() }
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
        menu.addItem(line("xswap · 주간 사용량"))
        menu.addItem(line("█ 사용  ░ 남음 · 선택됨 = 새 실행 기본 계정"))
        menu.addItem(.separator())
        if let data = snapshot {
            for account in data.accounts {
                let badge = account.selected ? "  · 선택됨" : account.exhausted ? "  · 한도 도달" : ""
                menu.addItem(line((account.selected ? "▸ " : "") + account.name + badge))
                let color: NSColor = account.tone == "red" ? .systemRed : account.tone == "yellow" ? .systemOrange : account.tone == "green" ? .systemGreen : .secondaryLabelColor
                if !account.bar.isEmpty { menu.addItem(line(account.bar, color: color, mono: true)) }
                menu.addItem(line(account.summary, color: color))
                if !account.reset.isEmpty { menu.addItem(line(account.reset)) }
                menu.addItem(.separator())
            }
            menu.addItem(line(data.policy))
            if data.sessions.isEmpty {
                menu.addItem(line("실행 중인 자동전환 세션 없음"))
            } else {
                for name in Set(data.sessions.map { $0.account }).sorted() {
                    let count = data.sessions.filter { $0.account == name }.count
                    menu.addItem(line("실행 중: \(name) · \(count)개 세션"))
                }
            }
            let formatter = DateFormatter(); formatter.dateFormat = "HH:mm:ss"
            menu.addItem(line("조회 \(formatter.string(from: Date(timeIntervalSince1970: data.updatedAt))) · 5분마다 갱신"))
        } else {
            menu.addItem(line(failed ? "조회 실패 · CLI 설치와 로그인을 확인하세요" : "사용량을 불러오는 중…"))
        }
        if failed && snapshot != nil { menu.addItem(line("갱신 실패 · 표시된 수치는 마지막 조회 결과", color: .systemOrange)) }
        menu.addItem(.separator())
        let refreshItem = NSMenuItem(title: busy ? "새로고침 중…" : "새로고침", action: #selector(refresh), keyEquivalent: "r")
        refreshItem.target = self; refreshItem.isEnabled = !busy
        menu.addItem(refreshItem)
        let quit = NSMenuItem(title: "메뉴바 종료", action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self; menu.addItem(quit)
        item.menu = menu
        if let account = snapshot?.accounts.first(where: { $0.selected }), let remaining = account.remaining {
            item.button?.title = "XS \(Int((100 - remaining).rounded()))% 사용" + (failed ? " !" : "")
            item.button?.toolTip = "\(account.name) · 주간 \(account.summary) (새 실행 기본 계정)"
        } else { item.button?.title = busy ? "XS · 조회 중" : "XS · 확인 필요" }
    }

    @objc func refresh() {
        guard !busy else { return }
        busy = true; rebuild()
        DispatchQueue.global(qos: .utility).async { [self] in
            let process = Process()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = ["dashboard"]
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
