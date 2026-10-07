import AppKit
import MontyKit
import SwiftUI

/// `scripts/tour.sh` runs `Monty --tour <dir> [--server URL]`: it walks the real app through every screen against a dev server
/// (`macos/scripts/dev_server.py`), as a new user, and saves each step as a PNG of the window and a text dump of what
/// is on screen (the accessibility tree: every text, button and field, with its frame). For reviewing the design
/// without clicking through it by hand.
@MainActor
enum Tour {
    static var directory: URL? {
        guard let index = CommandLine.arguments.firstIndex(of: "--tour"), index + 1 < CommandLine.arguments.count else { return nil }
        return URL(filePath: CommandLine.arguments[index + 1])
    }

    static var server: URL? {
        guard let index = CommandLine.arguments.firstIndex(of: "--server"), index + 1 < CommandLine.arguments.count else { return nil }
        return URL(string: CommandLine.arguments[index + 1])
    }

    /// A model of its own: a new account, and nothing of the user's real session or settings.
    static func model() -> AppModel {
        let id = "monty-tour-\(UUID().uuidString)"
        return AppModel(
            serverURL: server ?? URL(string: "http://127.0.0.1:8000")!,
            cookies: HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: id),
            defaults: UserDefaults(suiteName: id)!
        )
    }

    static func run(_ app: AppModel, into directory: URL) async {
        current = app
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var step = 0
        func snap(_ name: String, settle: Int = 900) async {
            try? await Task.sleep(for: .milliseconds(settle))  // let animations settle
            step += 1
            await capture(String(format: "%02d-%@", step, name), into: directory)
        }
        func wait(_ seconds: Double = 30, _ condition: () -> Bool) async {
            let deadline = Date().addingTimeInterval(seconds)
            while !condition(), Date() < deadline { try? await Task.sleep(for: .milliseconds(150)) }
        }
        func sites() -> [String: String] {
            let file = URL(filePath: #filePath).deletingLastPathComponent().appending(path: "../../../data/mac-dev-sites.json").standardized
            return (try? JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: String]) ?? [:]
        }
        func say(_ text: String) async -> ChatModel? {
            guard let chat = app.chat else { return nil }
            chat.draft = text
            await chat.send()
            return chat
        }
        guard let window = mainWindow else { return }
        window.setContentSize(NSSize(width: 1180, height: 780))
        window.center()
        let site = sites()
        NSApp.appearance = NSAppearance(named: .aqua)  // light first, whatever this Mac uses; dark comes later

        await wait(5) { app.phase == .signedOut }
        await snap("sign-in")
        let email = "tour-\(UUID().uuidString.prefix(6).lowercased())@example.test"
        try? await app.signUp(email: email, password: "correct horse")
        await wait { app.threadsLoaded }
        await snap("new-task-empty")
        app.chat?.draft = "Find the three cheapest flights to Lisbon next Friday."
        await snap("new-task-suggestion-filled")

        if let flights = site["flights"], let chat = await say("Find the three cheapest flights to Lisbon next Friday at \(flights)") {
            chat.watching = true
            await snap("working", settle: 60)
            await wait { chat.run?.status.isActive == false }
            await snap("reply-markdown")
        }

        app.open(.chat(nil))
        if let chat = await say("Ask me my favourite colour and remember it.") {
            await wait { chat.ask != nil }
            await snap("ask-question")
            await chat.answer(.text("green"))
            await wait { chat.run?.status.isActive == false }
        }

        app.open(.chat(nil))
        if let shop = site["shop"], let chat = await say("Order eggs from \(shop)") {
            await wait { chat.ask?.kind == .handoff }
            await snap("ask-handoff")
            chat.browserExpanded = true
            await snap("browser-expanded")
            chat.browserExpanded = false
            await chat.takeOver()
            if let live = chat.live {
                await wait(15) { live.frame != nil && live.activeTab?.url.hasPrefix("http") == true }
                await snap("takeover")
                let start = live.activeTab?.url
                live.type("alice")
                live.input(.press(key: "Tab", modifiers: []))
                live.type("hunter2")
                live.input(.press(key: "Enter", modifiers: []))
                await wait(10) { live.activeTab?.url != start }
                await snap("takeover-signed-in")
                live.giveBack()
                await wait(10) { live.state.isOver }
                await snap("takeover-given-back")
                await wait(10) { chat.live == nil }
            }
            await wait { chat.ask?.kind == .approval }
            await snap("ask-approval")
            await chat.answer(.approve())
            await wait { chat.run?.status.isActive == false }
            await snap("order-done")
        }

        // Several chats waiting at once: the sidebar's "Needs you".
        for _ in 0..<2 {
            app.open(.chat(nil))
            if let chat = await say("Ask me my favourite colour and remember it.") { await wait { chat.ask != nil } }
        }
        app.open(.chat(nil))
        await app.loadThreads()
        await snap("sidebar-needs-you")
        NotificationCenter.default.post(name: .montyCommandPalette, object: nil)
        await snap("command-palette")
        NotificationCenter.default.post(name: .montyCommandPalette, object: nil)
        if let waiting = app.needsYou.first {
            app.open(.chat(waiting.id))
            await wait { app.chat?.ask != nil }
            await app.chat?.stop()
            await snap("stopped")
        }

        app.open(.chat(nil))
        if let chat = await say("Fail please") {
            await wait { chat.run?.status.isActive == false }
            await snap("failed")
        }

        app.open(.chat(nil))
        if let slots = site["slots"], let chat = await say("Tell me when a delivery slot opens at \(slots)") {
            await wait { chat.ask?.kind == .approval }
            await chat.answer(.approve())
            await wait { chat.run?.status.isActive == false }
        }
        app.open(.chat(nil))
        if let invoices = site["invoices"], let chat = await say("Download my last three invoices from \(invoices)") {
            await wait(60) { chat.run?.status.isActive == false }
            await snap("invoices-reply")
        }
        for (route, name) in [(Route.schedules, "schedules"), (.files, "files"), (.signIns, "saved-sign-ins"), (.memory, "memory")] {
            app.open(route)
            await snap(name)
        }

        NSApp.appearance = NSAppearance(named: .darkAqua)
        app.open(.chat(nil))
        await snap("dark-new-task")
        if let first = app.threads.last {
            app.open(.chat(first.id))
            await wait { app.chat?.loading == false && app.chat?.messages.isEmpty == false }
            await snap("dark-reply")
        }
        app.open(.chat(nil))
        if let chat = await say("Ask me my favourite colour and remember it.") {
            await wait { chat.ask != nil }
            await snap("dark-ask-question")
            await chat.stop()
        }
        app.open(.schedules)
        await snap("dark-schedules")

        NSApp.appearance = NSAppearance(named: .aqua)
        window.setContentSize(NSSize(width: 760, height: 520))  // smaller than allowed: the window keeps to its minimum
        app.open(.chat(nil))
        await snap("narrow-new-task")
        if let first = app.threads.last {
            app.open(.chat(first.id))
            await snap("narrow-reply")
        }
        if let waiting = app.needsYou.first {  // Monty's browser opened beside a chat in a narrow window
            app.open(.chat(waiting.id))
            await wait { app.chat?.ask != nil }
            app.chat?.watching = true
            await snap("narrow-watching")
        }
        print("tour: \(step) screens in \(directory.path)")
        NSApp.terminate(nil)
    }

    static var mainWindow: NSWindow? { NSApp.windows.first { $0.isVisible && $0.canBecomeMain } }

    /// The window as a PNG, and what is on screen as text.
    static var current: AppModel?

    static func capture(_ name: String, into directory: URL) async {
        guard let window = mainWindow,
              let view = window.contentView?.superview ?? window.contentView,
              let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        try? bitmap.representation(using: .png, properties: [:])?.write(to: directory.appending(path: "\(name).png"))

        var lines = ["# \(name): window \(Int(window.frame.width))×\(Int(window.frame.height)), \(NSApp.effectiveAppearance.name.rawValue)"]
        if let app = current {
            lines.append("# model: " + app.threads.map { "\($0.title.prefix(20))=\($0.status?.rawValue ?? "-")" }.joined(separator: ", "))
        }
        // What VoiceOver sees, read from outside: scripts/tour.sh answers each request (`<name>.want`) with the
        // accessibility tree (`<name>.ax`) from scripts/ax-dump.swift. AX answers this process only with itself, and
        // macOS trusts a reader started from the shell, not one started by this app.
        let want = directory.appending(path: "\(name).want")
        let answer = directory.appending(path: "\(name).ax")
        try? "\(getpid())".write(to: want, atomically: true, encoding: .utf8)
        let deadline = Date().addingTimeInterval(15)
        while !FileManager.default.fileExists(atPath: answer.path), Date() < deadline {
            try? await Task.sleep(for: .milliseconds(100))  // the main thread stays free to answer the reader
        }
        lines.append((try? String(contentsOf: answer, encoding: .utf8))
            ?? "(no accessibility dump: run the tour with scripts/tour.sh, on an unlocked Mac)")
        try? FileManager.default.removeItem(at: want)
        try? FileManager.default.removeItem(at: answer)
        try? lines.joined(separator: "\n").write(to: directory.appending(path: "\(name).txt"), atomically: true, encoding: .utf8)
    }
}
