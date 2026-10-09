import AppKit
import SammyKit
import ServiceManagement
import SwiftUI
import UniformTypeIdentifiers
import UserNotifications

/// The menu bar icon: the logomark, with a dot when a task needs the user. It is always there, so it also gives the
/// app a way to open the main window when it was closed.
struct MenuBarIcon: View {
    let app: AppModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Image(nsImage: Self.image(attention: !app.needsYou.isEmpty))
            .accessibilityLabel(app.needsYou.isEmpty ? "Sammy" : "Sammy: \(app.needsYou.count) chat\(app.needsYou.count == 1 ? " needs" : "s need") you")
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

/// The menu bar menu: what needs the user, what Sammy is working on, and the way back to the app.
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
        Button("Open Sammy") { show(nil) }
        Divider()
        SettingsLink { Text("Settings…") }
        Button("Quit Sammy") { NSApp.terminate(nil) }
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
                Text("Sammy tells you when a task needs you or finishes while you're elsewhere. It can only while it's open: with Open at login it waits in the menu bar from the moment you sign in to your Mac.")
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
                ? "Another of your Macs is browsing for \(app.sammyName) now. Turn this off and on again to take it back."
                : "While \(app.sammyName) is open, the browser of each task you start visits sites from this Mac, so they see your own internet connection, not a server's. It reaches only public websites, never your network. Scheduled tasks always browse from the server.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
        }
    }

    private func setOpensAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
            loginError = nil
        } catch {
            loginError = "Couldn't change this: \(error.localizedDescription) You can also add Sammy in System Settings › General › Login Items."
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
    @State private var exporting = false
    @State private var exportNote: String?
    @State private var deleting = false

    enum Confirm: Identifiable { case signOut, server; var id: Self { self } }

    var body: some View {
        Form {
            LabeledContent("Signed in as", value: app.user?.email ?? "Not signed in")
            if app.user != nil {
                Button("Sign Out…") { confirming = .signOut }
                Section {
                    Button("Export My Data…") { Task { await export() } }.disabled(exporting)
                    if let exportNote { Text(exportNote).font(.system(size: 12)).foregroundStyle(.secondary) }
                    Button("Delete Account…", role: .destructive) { deleting = true }
                } footer: {
                    Text("Your export has your chats, files, memories, schedules and the names of your integrations, never your sign-ins or tokens.")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                }
            }
            Section {
                TextField("Server", text: $server, prompt: Text("https://sammy.example.com"))
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
        .sheet(isPresented: $deleting) { DeleteAccountSheet() }
        .confirmationDialog(confirming == .server ? "Use another server?" : "Sign out of Sammy?",
                            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })) {
            if confirming == .server {
                Button("Use this server", action: save)
            } else {
                Button("Sign Out") { Task { await app.signOut() } }
            }
        } message: {
            Text(confirming == .server ? "You'll be signed out, and sign in again on the new server." : "Your tasks keep running on Sammy's server.")
        }
    }

    private func export() async {
        exporting = true
        defer { exporting = false }
        exportNote = "Gathering your data…"
        do {
            switch try await app.exportData() {
            case .emailed(let email):
                exportNote = "There is a lot of it, so Sammy is zipping it up. A link to download it will arrive at \(email) soon, and works for a day."
            case .zip(let data):
                exportNote = nil
                let panel = NSSavePanel()
                panel.nameFieldStringValue = "sammy-export-\(Date.now.formatted(.iso8601.year().month().day())).zip"
                panel.allowedContentTypes = [.zip]
                guard panel.runModal() == .OK, let url = panel.url else { return }
                try data.write(to: url)
                exportNote = "Saved as \(url.lastPathComponent)."
            }
        } catch {
            exportNote = error.localizedDescription
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

/// Deleting the account: everything of it goes from the server, once the user types their password.
struct DeleteAccountSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var password = ""
    @State private var problem: String?
    @State private var working = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Delete your Sammy account?").font(.headline)
            Text("Sammy stops your tasks, deletes your schedules, disconnects your integrations, closes its browser, and deletes your chats, files, memories and saved sign-ins. This cannot be undone.")
                .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            SecureField("Your password, to confirm", text: $password).onSubmit(delete)
            if let problem { Text(problem).font(.system(size: 12)).foregroundStyle(Palette.onErrorContainer) }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Delete Account", role: .destructive, action: delete).disabled(password.isEmpty || working)
            }
        }
        .padding(20)
        .frame(width: 400)
    }

    private func delete() {
        guard !password.isEmpty, !working else { return }
        working = true
        Task {
            defer { working = false }
            do {
                try await app.deleteAccount(password: password)
                dismiss()
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}
