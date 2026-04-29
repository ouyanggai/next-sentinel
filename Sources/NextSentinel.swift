import AppKit
import Foundation

enum HomePaths {
    static let home = FileManager.default.homeDirectoryForCurrentUser.path
    static let hooksDirectory = "\(home)/.codex/hooks"
    static let ctlPath = "\(hooksDirectory)/next_ctl.py"
    static let routerLogPath = "\(hooksDirectory)/next_router.log"
    static let sentinelLogPath = "\(hooksDirectory)/next_sentinel.log"
}

final class CommandRunner {
    private let ctlPath: String
    private let timeout: DispatchTimeInterval = .seconds(12)

    init() {
        ctlPath = ProcessInfo.processInfo.environment["NEXT_CTL_PATH"] ?? HomePaths.ctlPath
    }

    func run(_ arguments: [String]) -> String {
        let process = Process()
        let pipe = Pipe()
        let completion = DispatchSemaphore(value: 0)
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [ctlPath] + arguments
        process.standardOutput = pipe
        process.standardError = pipe
        process.terminationHandler = { _ in
            completion.signal()
        }

        do {
            try process.run()
        } catch {
            return "执行失败: \(error.localizedDescription)"
        }

        if completion.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            _ = completion.wait(timeout: .now() + .seconds(2))
            return "执行超时: next_ctl.py \(arguments.joined(separator: " "))"
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (String(data: data, encoding: .utf8) ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem?
    private let menu = NSMenu()
    private let runner = CommandRunner()
    private let codexBundleIdentifier = "com.openai.codex"
    private let triggerDelay: TimeInterval = 60
    private let idleStatusLength: CGFloat = 72
    private let countdownStatusLength: CGFloat = 104

    private var pendingWorkItem: DispatchWorkItem?
    private var statusItemTitle = NSMenuItem(title: "状态读取中", action: nil, keyEquivalent: "")
    private var lastActionItem = NSMenuItem(title: "最近动作：暂无", action: nil, keyEquivalent: "")
    private var countdownTimer: Timer?
    private var scheduledFireDate: Date?
    private var statusIcon: NSImage?
    private var lastTargetStatusCode = "UNKNOWN"

    func applicationWillFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        appLog("applicationWillFinishLaunching")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        appLog("applicationDidFinishLaunching")
        configureStatusItem()
        configureMenu()
        observeCodexLifecycle()
        refreshStatus { [weak self] in
            guard let self else { return }
            if self.isCodexRunning() {
                self.scheduleFallbackForExistingCodex()
            }
        }
    }

    private func configureStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem = item
        item.length = idleStatusLength
        guard let button = item.button else {
            appLog("status item button unavailable")
            return
        }
        statusIcon = loadStatusIcon()
        button.image = statusIcon
        button.title = "NEXT"
        button.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .semibold)
        button.imagePosition = .imageLeft
        button.toolTip = "NEXT 兜底监督"
        appLog("status item configured")
    }

    private func loadStatusIcon() -> NSImage? {
        guard let url = Bundle.main.url(forResource: "StatusIcon", withExtension: "png"),
              let image = NSImage(contentsOf: url) else {
            return nil
        }
        image.size = NSSize(width: 18, height: 18)
        image.isTemplate = true
        return image
    }

    private func configureMenu() {
        menu.autoenablesItems = false

        let header = NSMenuItem(title: "NEXT 兜底监督", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        menu.addItem(statusItemTitle)
        menu.addItem(lastActionItem)
        menu.addItem(.separator())

        menu.addItem(makeItem("刷新状态", #selector(refreshStatusAction), "r"))
        menu.addItem(makeItem("启动 NEXT", #selector(startAction), "s"))
        menu.addItem(makeItem("停止 NEXT", #selector(stopAction), "p"))
        menu.addItem(makeItem("立即触发兜底", #selector(triggerAction), "t"))
        menu.addItem(.separator())
        menu.addItem(makeItem("打开日志", #selector(openLogAction), "l"))
        menu.addItem(makeItem("打开配置目录", #selector(openHooksDirectoryAction), ""))
        menu.addItem(.separator())
        menu.addItem(makeItem("退出", #selector(quitAction), "q"))

        statusItem?.menu = menu
    }

    private func makeItem(_ title: String, _ action: Selector, _ keyEquivalent: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: keyEquivalent)
        item.target = self
        return item
    }

    private func observeCodexLifecycle() {
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(applicationLaunched(_:)),
            name: NSWorkspace.didLaunchApplicationNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(applicationTerminated(_:)),
            name: NSWorkspace.didTerminateApplicationNotification,
            object: nil
        )
    }

    @objc private func applicationLaunched(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else {
            return
        }
        guard app.bundleIdentifier == codexBundleIdentifier else { return }
        scheduleFallback(reason: "检测到 Codex 启动")
    }

    @objc private func applicationTerminated(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else {
            return
        }
        guard app.bundleIdentifier == codexBundleIdentifier else { return }
        pendingWorkItem?.cancel()
        pendingWorkItem = nil
        scheduledFireDate = nil
        stopCountdown()
        setIdleStatusIcon()
        updateLastAction("Codex 已退出，已取消待触发兜底")
    }

    private func isCodexRunning() -> Bool {
        NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == codexBundleIdentifier }
    }

    private func scheduleFallback(reason: String) {
        pendingWorkItem?.cancel()

        let fireDate = Date().addingTimeInterval(triggerDelay)
        scheduledFireDate = fireDate
        updateLastAction("\(reason)，60 秒后触发兜底")
        startCountdown()

        let workItem = DispatchWorkItem { [weak self] in
            DispatchQueue.main.async {
                self?.triggerFallback(source: "Codex 启动后自动触发")
            }
        }
        pendingWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + triggerDelay, execute: workItem)
    }

    private func scheduleFallbackForExistingCodex() {
        if lastTargetStatusCode == "RUNNING" {
            updateLastAction("Codex 已在运行，目标运行中，不触发兜底")
            return
        }
        if lastTargetStatusCode == "COMPLETE" {
            updateLastAction("Codex 已在运行，目标已完成，不触发兜底")
            return
        }
        scheduleFallback(reason: "Codex 已在运行")
    }

    private func startCountdown() {
        stopCountdown()
        countdownTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.updateCountdown()
        }
        updateCountdown()
    }

    private func stopCountdown() {
        countdownTimer?.invalidate()
        countdownTimer = nil
    }

    private func updateCountdown() {
        guard let fireDate = scheduledFireDate else {
            stopCountdown()
            return
        }
        let remaining = max(0, Int(ceil(fireDate.timeIntervalSinceNow)))
        if remaining == 0 {
            stopCountdown()
            setIdleStatusIcon()
            return
        }
        guard let button = statusItem?.button else { return }
        statusItem?.length = countdownStatusLength
        button.image = statusIcon
        button.title = "NEXT \(remaining)s"
        button.imagePosition = .imageLeft
    }

    private func setIdleStatusIcon() {
        guard let button = statusItem?.button else { return }
        statusItem?.length = idleStatusLength
        button.image = statusIcon
        button.title = "NEXT"
        button.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .semibold)
        button.imagePosition = .imageLeft
    }

    private func triggerFallback(source: String) {
        pendingWorkItem = nil
        scheduledFireDate = nil
        stopCountdown()
        setIdleStatusIcon()

        updateLastAction("\(source)：执行中")
        runCommand(["trigger"]) { [weak self] output in
            guard let self else { return }
            self.updateLastAction("\(source)：\(self.compact(output))")
            self.refreshStatus()
        }
    }

    private func updateLastAction(_ text: String) {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "HH:mm:ss"
        lastActionItem.title = "最近动作：\(formatter.string(from: Date()))  \(text)"
    }

    private func compact(_ output: String) -> String {
        let firstLine = output.split(separator: "\n", omittingEmptySubsequences: true).first
        return firstLine.map(String.init) ?? "无输出"
    }

    private func appLog(_ message: String) {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let line = "\(formatter.string(from: Date())) \(message)\n"
        let url = URL(fileURLWithPath: HomePaths.sentinelLogPath)
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: url.path),
               let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: url, options: .atomic)
            }
        }
        NSLog("[Next Sentinel] \(message)")
    }

    private func runCommand(_ arguments: [String], completion: @escaping (String) -> Void) {
        DispatchQueue.global(qos: .utility).async { [runner] in
            let output = runner.run(arguments)
            DispatchQueue.main.async {
                completion(output)
            }
        }
    }

    private func refreshStatus(completion: (() -> Void)? = nil) {
        statusItemTitle.title = "状态：读取中"
        runCommand(["status"]) { [weak self] output in
            self?.applyStatus(output)
            completion?()
        }
    }

    private func applyStatus(_ output: String) {
        let lines = output.split(separator: "\n").map(String.init)
        let hooks = lines.first(where: { $0.hasPrefix("NEXT hooks:") })?.replacingOccurrences(of: "NEXT hooks: ", with: "") ?? "UNKNOWN"
        let automation = statusValue(lines, suffix: " db:")
        let nextRun = statusValue(lines, suffix: " next_run_at:")
        let target = targetStatusValue(lines)
        lastTargetStatusCode = targetStatusCode(lines)

        statusItemTitle.title = "状态：NEXT \(hooks) / 兜底 \(automation) / 目标 \(target) / next \(nextRun)"
    }

    private func targetStatusCode(_ lines: [String]) -> String {
        guard let line = lines.first(where: { $0.hasPrefix("target_status:") }) else {
            return "UNKNOWN"
        }
        if line.contains("QUOTA_BLOCKED") { return "QUOTA_BLOCKED" }
        if line.contains("RUNNING") { return "RUNNING" }
        if line.contains("COMPLETE") { return "COMPLETE" }
        return "UNKNOWN"
    }

    private func targetStatusValue(_ lines: [String]) -> String {
        guard let line = lines.first(where: { $0.hasPrefix("target_status:") }) else {
            return "UNKNOWN"
        }
        if line.contains("QUOTA_BLOCKED") {
            if let range = line.range(of: "retry_after=") {
                let hint = String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
                return hint.isEmpty ? "额度阻塞" : "额度阻塞 \(hint)"
            }
            return "额度阻塞"
        }
        if line.contains("RUNNING") {
            return "运行中"
        }
        if line.contains("COMPLETE") {
            return "完成"
        }
        return "UNKNOWN"
    }

    private func statusValue(_ lines: [String], suffix: String) -> String {
        guard let line = lines.first(where: { $0.contains(suffix) }),
              let range = line.range(of: suffix) else {
            return "UNKNOWN"
        }
        return String(line[range.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    @objc private func refreshStatusAction() {
        updateLastAction("状态刷新中")
        refreshStatus { [weak self] in
            self?.updateLastAction("状态已刷新")
        }
    }

    @objc private func startAction() {
        updateLastAction("启动 NEXT 中")
        runCommand(["start"]) { [weak self] output in
            guard let self else { return }
            self.updateLastAction(self.compact(output))
            self.refreshStatus()
        }
    }

    @objc private func stopAction() {
        pendingWorkItem?.cancel()
        pendingWorkItem = nil
        scheduledFireDate = nil
        stopCountdown()
        setIdleStatusIcon()
        updateLastAction("停止 NEXT 中")
        runCommand(["stop"]) { [weak self] output in
            guard let self else { return }
            self.updateLastAction(self.compact(output))
            self.refreshStatus()
        }
    }

    @objc private func triggerAction() {
        triggerFallback(source: "手动触发")
    }

    @objc private func openLogAction() {
        NSWorkspace.shared.open(URL(fileURLWithPath: HomePaths.routerLogPath))
    }

    @objc private func openHooksDirectoryAction() {
        NSWorkspace.shared.open(URL(fileURLWithPath: HomePaths.hooksDirectory))
    }

    @objc private func quitAction() {
        NSApp.terminate(nil)
    }
}

@main
enum NextSentinelMain {
    private static var delegate: AppDelegate?

    static func main() {
        let app = NSApplication.shared
        let appDelegate = AppDelegate()
        delegate = appDelegate
        app.delegate = appDelegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}
