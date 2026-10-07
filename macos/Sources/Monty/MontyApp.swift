import AppKit
import MontyKit
import SwiftUI
import UserNotifications

@main
struct MontyApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @AppStorage("appearance") private var appearance = Appearance.system

    var body: some Scene {
        Window("Monty", id: "main") {
            RootView()
                .environment(delegate.app)
                .preferredColorScheme(appearance.scheme)
                .frame(minWidth: 760, minHeight: 520)
        }
        .defaultSize(width: 1180, height: 780)
        .commands { MontyCommands(app: delegate.app) }

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
        app.notify = { [weak self] notice in self?.post(notice) }
        app.threadsChanged = { [weak self] in self?.updateBadge() }
        app.firstTaskSent = { [weak self] in self?.requestNotifications() }
        let center = NotificationCenter.default
        center.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.app.isActive = true
                self?.app.refreshThreads()
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

    /// Closing the window keeps Monty in the menu bar, where it still says when a task needs you.
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
        content.userInfo = ["thread": notice.threadId]
        if notice.kind != .finished { content.interruptionLevel = .timeSensitive }
        center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let thread = response.notification.request.content.userInfo["thread"] as? String
        await MainActor.run { show(thread: thread) }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
}

struct MontyCommands: Commands {
    let app: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        SidebarCommands()
        CommandGroup(replacing: .newItem) {
            Button("New Task") { show(.chat(nil)) }
                .keyboardShortcut("n")
                .disabled(app.user == nil || app.isTakingOver)
        }
        CommandGroup(after: .appSettings) {
            SignOutCommand(app: app)
        }
        CommandMenu("Task") {
            Button("Stop Task…") { NotificationCenter.default.post(name: .montyStopTask, object: nil) }
                .keyboardShortcut(".")
                .disabled(app.chat?.canStop != true || app.isTakingOver)
            if !app.isTakingOver {
                // While taking over, the takeover screen's "Not now" has ⇧⌘T.
                Button("Take Over Browser") { Task { await app.chat?.takeOver() } }
                    .keyboardShortcut("t", modifiers: [.command, .shift])
                    .disabled(app.chat?.ask?.kind != .handoff)
            }
            Button(app.chat?.watching == true ? "Hide Monty's Browser" : "Watch Monty's Browser") {
                app.chat?.watching.toggle()
            }
            .keyboardShortcut("b", modifiers: [.command, .shift])
            .disabled(app.chat?.isActive != true || app.isTakingOver)
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
            Group {
                Button("Schedules") { show(.schedules) }.keyboardShortcut("1")
                Button("Files") { show(.files) }.keyboardShortcut("2")
                Button("Saved Sign-ins") { show(.signIns) }.keyboardShortcut("3")
                Button("Memory") { show(.memory) }.keyboardShortcut("4")
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
        let order = app.needsYou + app.threads.filter { $0.status != .waiting }
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
            alert.messageText = "Sign out of Monty?"
            alert.informativeText = "Your tasks keep running on Monty's server."
            alert.addButton(withTitle: "Sign Out")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() == .alertFirstButtonReturn { Task { await app.signOut() } }
        }
        .disabled(app.user == nil)
    }
}
