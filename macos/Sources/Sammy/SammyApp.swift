import AppKit
import SammyKit
import SwiftUI
import UserNotifications

@main
struct SammyApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @AppStorage("appearance") private var appearance = Appearance.system

    var body: some Scene {
        Window("Sammy", id: "main") {
            RootView()
                .environment(delegate.app)
                .preferredColorScheme(appearance.scheme)
                .frame(minWidth: Metrics.windowMinWidth, minHeight: 560)
        }
        .defaultSize(width: 1180, height: 780)
        .commands { SammyCommands(app: delegate.app) }

        MenuBarExtra {
            MenuBarMenu().environment(delegate.app)
        } label: {
            MenuBarIcon(app: delegate.app)
        }
        .menuBarExtraStyle(.menu)

        Settings {
            SettingsView()
                .environment(delegate.app)
                .preferredColorScheme(appearance.scheme)
        }
    }
}

enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var scheme: ColorScheme? {
        switch self {
        case .system: nil
        case .light: .light
        case .dark: .dark
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    let app = Tour.directory == nil ? AppModel() : Tour.model()
    /// Opens (or brings back) the main window; set by a view that has SwiftUI's `openWindow`.
    var openMainWindow: (() -> Void)?

    /// Notification Center needs an app bundle; `swift run` has none.
    private var notifications: UNUserNotificationCenter? {
        Bundle.main.bundleIdentifier == nil ? nil : UNUserNotificationCenter.current()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        notifications?.delegate = self
        // A question can be answered from its notification, without opening the app.
        let reply = UNTextInputNotificationAction(
            identifier: "reply", title: "Reply", options: [], textInputButtonTitle: "Send", textInputPlaceholder: "Your answer"
        )
        notifications?.setNotificationCategories([UNNotificationCategory(identifier: "question", actions: [reply], intentIdentifiers: [])])
        app.notify = { [weak self] notice in self?.post(notice) }
        app.openInBrowser = { url in NSWorkspace.shared.open(url) }  // sign-ins happen in the user's own browser
        app.threadsChanged = { [weak self] in self?.updateBadge() }
        app.firstTaskSent = { [weak self] in self?.requestNotifications() }
        // Seen in the app: what Notification Center says about the chat is old news.
        app.signedOutOfNotifications = { [weak self] in self?.notifications?.removeAllDeliveredNotifications() }
        app.chatSeen = { [weak self] thread in
            self?.notifications?.removeDeliveredNotifications(withIdentifiers: [Self.identifier(thread)])
        }
        let center = NotificationCenter.default
        center.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.app.isActive = true
                self?.app.refreshThreads()
                self?.app.reloadPage()  // whatever changed while the user was away
            }
        }
        center.addObserver(forName: NSApplication.didResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.app.isActive = false }
        }
        // Whether the main window is on screen: a closed or minimised window means the user is not looking.
        let visibility: [(Notification.Name, Bool)] = [
            (NSWindow.didBecomeMainNotification, true), (NSWindow.didDeminiaturizeNotification, true),
            (NSWindow.willCloseNotification, false), (NSWindow.didMiniaturizeNotification, false),
        ]
        for (name, visible) in visibility {
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let window = note.object as? NSWindow
                MainActor.assumeIsolated {
                    if window?.identifier?.rawValue.hasPrefix("main") == true { self?.app.isWindowVisible = visible }
                }
            }
        }
        Task {
            await app.start()
            if let directory = Tour.directory { await Tour.run(app, into: directory) }
        }
    }

    /// The last few seconds' spans, which the batch has not sent yet.
    func applicationWillTerminate(_ notification: Notification) {
        app.telemetry.flushBeforeQuitting()
    }

    /// A chat deleted a moment ago is deleted before Sammy quits (waiting at most a few seconds for the server), so
    /// quitting never quietly undoes a delete.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard app.hasPendingDeletes else { return .terminateNow }
        Task {
            await withTaskGroup(of: Void.self) { group in
                group.addTask { await self.app.finishPendingDelete() }
                group.addTask { try? await Task.sleep(for: .seconds(4)) }
                await group.next()
                group.cancelAll()
            }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    /// Closing the window keeps Sammy in the menu bar, where it still says when a task needs you.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { openMainWindow?() }
        return true
    }

    func requestNotifications() {
        notifications?.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    /// The Dock badge is the number of chats waiting for the user, always.
    private func updateBadge() {
        let count = app.needsYou.count
        NSApp.dockTile.badgeLabel = count == 0 ? nil : "\(count)"
    }

    func show(thread: String?) {
        NSApp.activate(ignoringOtherApps: true)
        openMainWindow?()
        if let thread { app.open(.chat(thread)) }
    }

    private func post(_ notice: Notice) {
        guard let center = notifications else { return }
        let content = UNMutableNotificationContent()
        content.title = notice.title
        content.body = notice.body
        content.sound = notice.kind == .finished ? nil : .default
        content.threadIdentifier = notice.threadId
        content.userInfo = ["thread": notice.threadId, "ask": notice.askId ?? "", "user": app.user?.id ?? ""]
        if notice.kind == .question { content.categoryIdentifier = "question" }
        if notice.kind != .finished { content.interruptionLevel = .timeSensitive }
        // One per chat: its latest news replaces what it said before, and opening the chat clears it.
        center.add(UNNotificationRequest(identifier: Self.identifier(notice.threadId), content: content, trigger: nil))
    }

    private static func identifier(_ thread: String) -> String { "chat-\(thread)" }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        // Someone else's, from before they signed out on this Mac: it says nothing about this user's chats.
        let owner = info["user"] as? String
        guard await MainActor.run(body: { owner == app.user?.id }) else {
            await MainActor.run { show(thread: nil) }
            return
        }
        let thread = info["thread"] as? String
        if let reply = response as? UNTextInputNotificationResponse, let thread, let ask = info["ask"] as? String, !ask.isEmpty {
            let text = reply.userText
            // Answered from the notification: the app stays where it is, unless the answer couldn't go.
            if await app.answerFromNotification(ask, in: thread, text: text) { return }
        }
        await MainActor.run { show(thread: thread) }
    }

    /// The Dock icon's menu: what needs the user, and a new task.
    func applicationDockMenu(_ sender: NSApplication) -> NSMenu? {
        let menu = NSMenu()
        guard app.user != nil else { return menu }
        for thread in app.needsYou.prefix(8) {
            let item = NSMenuItem(title: thread.title.readableTitle, action: #selector(openFromDock(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = thread.id
            menu.addItem(item)
        }
        if !app.needsYou.isEmpty { menu.addItem(.separator()) }
        let new = NSMenuItem(title: "New Task", action: #selector(openFromDock(_:)), keyEquivalent: "")
        new.target = self
        menu.addItem(new)
        return menu
    }

    @objc private func openFromDock(_ item: NSMenuItem) {
        if let thread = item.representedObject as? String { show(thread: thread) } else { show(thread: nil); app.open(.chat(nil)) }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

struct SammyCommands: Commands {
    let app: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        SidebarCommands()
        CommandGroup(replacing: .newItem) {
            Button("New Task") { show(.chat(nil)) }
                .keyboardShortcut("n")
                .disabled(app.user == nil || app.isTakingOver)
            Divider()
            // No ⌘⌫ here: in the message box it deletes the line. The sidebar takes ⌫ and ⌘⌫ on the selected chat.
            Button("Rename Chat…") { app.renaming = app.openThread }
                .disabled(app.openThread == nil || app.isTakingOver)
            Button("Delete Chat…") { app.deleting = app.openThread }
                .disabled(app.openThread == nil || app.isTakingOver)
        }
        CommandGroup(after: .appSettings) {
            SignOutCommand(app: app)
        }
        CommandMenu("Task") {
            Button("Stop Task") { Task { await app.chat?.stop() } }
                .keyboardShortcut(".")
                .disabled(app.chat?.canStop != true || app.isTakingOver)
            if case .chat = app.route {
                Button("Try Again") { Task { await app.chat?.retry() } }
                    .keyboardShortcut("r")
                    .disabled(app.chat?.canRetry != true || app.isTakingOver)
            } else {
                Button("Reload") { app.libraryError = nil; app.reloadPage() }  // ⌘R, as in a browser
                    .keyboardShortcut("r")
                    .disabled(app.user == nil)
            }
            Button("Edit and Send Again") { app.chat?.editLastTask() }
                .disabled(app.chat?.canRetry != true || app.isTakingOver)
            Divider()
            // No shortcuts: going ahead with something that costs money is a deliberate choice, even from a menu.
            Button("Approve") { Task { await app.chat?.answer(.approve()) } }
                .disabled(app.chat?.ask?.kind != .approval || app.chat?.answering == true || app.isTakingOver)
            Button("Don't Approve…") { app.chat?.denying = true }
                .disabled(app.chat?.ask?.kind != .approval || app.chat?.answering == true || app.isTakingOver)
            Divider()
            if !app.isTakingOver {
                // While taking over, the takeover screen's "Not now" has ⇧⌘T.
                Button("Take Over Browser") { Task { await app.chat?.takeOver() } }
                    .keyboardShortcut("t", modifiers: [.command, .shift])
                    .disabled(app.chat?.ask?.kind != .handoff)
            }
            Button(app.chat?.watching == true ? "Hide \(app.sammyName)'s Browser" : "Watch \(app.sammyName)'s Browser") {
                app.chat?.watching.toggle()
            }
            .keyboardShortcut("b", modifiers: [.command, .shift])
            .disabled(app.chat?.run == nil || app.isTakingOver)
            Button(app.chat?.browserExpanded == true ? "Back to the Chat" : "Expand \(app.sammyName)'s Browser") {
                app.chat?.browserExpanded.toggle()
            }
            .keyboardShortcut("f", modifiers: [.command, .shift])
            .disabled(app.chat?.run == nil || app.isTakingOver)
            Divider()
            Button("Next Chat Needing You") {
                if let next = nextNeedingYou { show(.chat(next.id)) }
            }
            .keyboardShortcut("j", modifiers: [.command, .shift])
            .disabled(nextNeedingYou == nil || app.isTakingOver)
            Button("Previous Chat") { step(-1) }.keyboardShortcut("[", modifiers: [.command, .option]).disabled(app.isTakingOver)
            Button("Next Chat") { step(1) }.keyboardShortcut("]", modifiers: [.command, .option]).disabled(app.isTakingOver)
        }
        CommandGroup(after: .sidebar) {
            Divider()
            Button("Back") { app.goBack() }
                .keyboardShortcut("[")
                .disabled(!app.canGoBack || app.isTakingOver)
            Button("Forward") { app.goForward() }
                .keyboardShortcut("]")
                .disabled(!app.canGoForward || app.isTakingOver)
            Divider()
            Button("Find Chats") { openWindow(id: "main"); app.wantsFindChats = true }
                .keyboardShortcut("f")
                .disabled(app.user == nil || app.isTakingOver)
            Button("Message Box") { NotificationCenter.default.post(name: .sammyFocusMessage, object: nil) }
                .keyboardShortcut("l")
                .disabled(app.user == nil || app.isTakingOver || app.chat == nil)
            Divider()
            Button("Command Palette…") { openWindow(id: "main"); app.showingPalette.toggle() }
                .keyboardShortcut("k")
                .disabled(app.user == nil || app.isTakingOver)
            // ⌘1…⌘9: the chats in the sidebar's order, as T3 Code's threads; their titles say which is which.
            Menu("Go to Chat") {
                ForEach(Array(app.sidebarOrder.prefix(9).enumerated()), id: \.element.id) { index, thread in
                    Button(thread.title.readableTitle) { show(.chat(thread.id)) }
                        .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")))
                }
            }
            .disabled(app.user == nil || app.isTakingOver || app.threads.isEmpty)
            Group {
                Button("Schedules") { show(.schedules) }.keyboardShortcut("1", modifiers: [.command, .shift])
                Button("Saved Sign-ins") { show(.signIns) }.keyboardShortcut("2", modifiers: [.command, .shift])
                Button("Memory") { show(.memory) }.keyboardShortcut("3", modifiers: [.command, .shift])
                Button("Integrations") { show(.integrations) }.keyboardShortcut("4", modifiers: [.command, .shift])
                Button("Skills") { show(.skills) }.keyboardShortcut("5", modifiers: [.command, .shift])
            }
            .disabled(app.user == nil || app.isTakingOver)
            Divider()
        }
    }

    private var nextNeedingYou: ThreadSummary? {
        let waiting = app.needsYou
        guard case .chat(let current) = app.route, let index = waiting.firstIndex(where: { $0.id == current }) else { return waiting.first }
        return waiting.count > 1 ? waiting[(index + 1) % waiting.count] : nil
    }

    private func step(_ offset: Int) {
        let order = app.sidebarOrder
        guard !order.isEmpty else { return }
        guard case .chat(let current) = app.route, let index = order.firstIndex(where: { $0.id == current }) else {
            show(.chat(order[0].id))
            return
        }
        show(.chat(order[(index + offset + order.count) % order.count].id))
    }

    private func show(_ route: Route) {
        openWindow(id: "main")
        app.open(route)
    }
}

/// Sign Out… in the app menu, after asking.
struct SignOutCommand: View {
    let app: AppModel

    var body: some View {
        Button("Sign Out…") {
            let alert = NSAlert()
            alert.messageText = "Sign out of Sammy?"
            alert.informativeText = "Your tasks keep running on Sammy's server."
            alert.addButton(withTitle: "Sign Out")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() == .alertFirstButtonReturn { Task { await app.signOut() } }
        }
        .disabled(app.user == nil)
    }
}
