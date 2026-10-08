import AppKit
import MontyKit
import ServiceManagement
import SwiftUI
import UserNotifications

/// The menu bar icon: the logomark, with a dot when a task needs the user. It is always there, so it also gives the
/// app a way to open the main window when it was closed.
struct MenuBarIcon: View {
    let app: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(nsImage: Self.image(attention: !app.needsYou.isEmpty))
            .accessibilityLabel(app.needsYou.isEmpty ? "Monty" : "Monty: \(app.needsYou.count) chat\(app.needsYou.count == 1 ? " needs" : "s need") you")
            .onAppear {
                (NSApp.delegate as? AppDelegate)?.openMainWindow = { openWindow(id: "main") }
            }
    }

    static func image(attention: Bool) -> NSImage {
        let size = NSSize(width: 18, height: 16)
        let image = NSImage(size: size, flipped: true) { rect in
            let mark = PydanticMark().path(in: CGRect(x: 0, y: 1, width: 16, height: 14))
            let path = NSBezierPath(cgPath: mark.cgPath)
            path.windingRule = .evenOdd
            NSColor.black.setFill()
            path.fill()
            if attention {
                NSBezierPath(ovalIn: CGRect(x: rect.maxX - 6, y: 0, width: 6, height: 6)).fill()
            }
            return true
        }
        image.isTemplate = true
        return image
    }
}

/// The menu bar menu: what needs the user, what Monty is working on, and the way back to the app.
struct MenuBarMenu: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        if app.user == nil {
            Text("Not signed in")
        } else {
            if !app.needsYou.isEmpty {
                Section("Needs you") {
                    ForEach(app.needsYou) { thread in
                        Button(thread.title.readableTitle) { show(.chat(thread.id)) }
                    }
                }
            }
            if !app.working.isEmpty {
                Section("Working") {
                    ForEach(app.working) { thread in
                        Button(thread.title.readableTitle) { show(.chat(thread.id)) }
                    }
                }
            }
            if app.needsYou.isEmpty && app.working.isEmpty {
                Text("Nothing needs you")
            }
            Divider()
            Button("New Task") { show(.chat(nil)) }
        }
        Button("Open Monty") { show(nil) }
        Divider()
        SettingsLink { Text("Settings…") }
        Button("Quit Monty") { NSApp.terminate(nil) }
    }

    private func show(_ route: Route?) {
        (NSApp.delegate as? AppDelegate)?.show(thread: nil)
        if let route { app.open(route) }
    }
}

struct SettingsView: View {
    var body: some View {
        TabView {
            GeneralSettings().tabItem { Label("General", systemImage: "gearshape") }
            AccountSettings().tabItem { Label("Account", systemImage: "person.crop.circle") }
        }
        .frame(width: 460)
    }
}

struct GeneralSettings: View {
    @Environment(AppModel.self) private var app
    @AppStorage("appearance") private var appearance = Appearance.system
    @State private var status: UNAuthorizationStatus = .notDetermined
    @State private var opensAtLogin = SMAppService.mainApp.status == .enabled
    @State private var loginError: String?

    var body: some View {
        Form {
            Picker("Appearance", selection: $appearance) {
                Text("Match my Mac").tag(Appearance.system)
                Text("Light").tag(Appearance.light)
                Text("Dark").tag(Appearance.dark)
            }
            Section {
                LabeledContent("Notifications") {
                    switch status {
                    case .authorized, .provisional:
                        Text("On").foregroundStyle(.secondary)
                    case .notDetermined:
                        Button("Turn on") {
                            Task {
                                _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
                                await refresh()
                            }
                        }
                    default:
                        Button("Open System Settings…") {
                            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension")!)
                        }
                    }
                }
                Toggle("Open at login", isOn: Binding(get: { opensAtLogin }, set: { setOpensAtLogin($0) }))
                if let loginError {
                    Text(loginError).font(.system(size: 12)).foregroundStyle(Palette.onErrorContainer)
                }
            } footer: {
                Text("Monty tells you when a task needs you or finishes while you're elsewhere. It can only while it's open: with Open at login it waits in the menu bar from the moment you sign in to your Mac.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
            }
            browsing
        }
        .formStyle(.grouped)
        .task { await refresh() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            Task { await refresh() }
        }
    }

    private var browsing: some View {
        @Bindable var app = app
        return Section {
            Toggle("Browse from this Mac", isOn: $app.browseFromMac)
        } footer: {
            Text(app.tunnelStatus.replaced
                ? "Another of your Macs is browsing for \(app.montyName) now. Turn this off and on again to take it back."
                : "While \(app.montyName) is open, the browser of each task you start visits sites from this Mac, so they see your own internet connection, not a server's. It reaches only public websites, never your network. Scheduled tasks always browse from the server.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
        }
    }

    private func setOpensAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginError = nil
        } catch {
            loginError = "Couldn't change this: \(error.localizedDescription) You can also add Monty in System Settings › General › Login Items."
        }
        opensAtLogin = SMAppService.mainApp.status == .enabled
    }

    private func refresh() async {
        opensAtLogin = SMAppService.mainApp.status == .enabled
        guard Bundle.main.bundleIdentifier != nil else { return }
        status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }
}

struct AccountSettings: View {
    @Environment(AppModel.self) private var app
    @State private var server = ""
    @State private var serverError: String?
    @State private var confirming: Confirm?

    enum Confirm: Identifiable { case signOut, server; var id: Self { self } }

    var body: some View {
        Form {
            LabeledContent("Signed in as", value: app.user?.email ?? "Not signed in")
            if app.user != nil {
                Button("Sign Out…") { confirming = .signOut }
            }
            Section {
                TextField("Server", text: $server, prompt: Text("https://monty.example.com"))
                    .onSubmit { confirming = .server }
                if let serverError { Text(serverError).font(.system(size: 12)).foregroundStyle(Palette.onErrorContainer) }
                HStack {
                    Spacer()
                    Button("Use this server") { confirming = .server }
                        .disabled(server.trimmingCharacters(in: .whitespaces) == app.serverURL.absoluteString)
                }
            } footer: {
                Text("Changing the server signs you out of this one.").font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
        .onAppear { server = app.serverURL.absoluteString }
        .confirmationDialog(confirming == .server ? "Use another server?" : "Sign out of Monty?",
                            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })) {
            if confirming == .server {
                Button("Use this server", action: save)
            } else {
                Button("Sign Out") { Task { await app.signOut() } }
            }
        } message: {
            Text(confirming == .server ? "You'll be signed out, and sign in again on the new server." : "Your tasks keep running on Monty's server.")
        }
    }

    private func save() {
        var text = server.trimmingCharacters(in: .whitespaces)
        if !text.contains("://") { text = "https://" + text }
        guard let url = URL(string: text), url.host() != nil else { serverError = "That isn't a server address."; return }
        serverError = nil
        app.useServer(url)
    }
}
