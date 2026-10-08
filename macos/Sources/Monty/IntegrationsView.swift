import MontyKit
import SwiftUI

/// The user's integrations: what is connected, the apps they can connect in one click, and their own MCP servers.
/// Signing in happens in the user's browser; the page reads itself again when they come back to Monty.
struct IntegrationsView: View {
    @Environment(AppModel.self) private var app
    @State private var removing: Connection?
    @State private var search = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text("Integrations").font(.system(size: 20, weight: .semibold)).accessibilityAddTraits(.isHeader)
                Text("Connect the apps you use, and \(app.montyName) can work in them for you. It asks before it changes anything.")
                    .font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant).padding(.top, 4)
                if let error = app.libraryError, app.integrations != nil {
                    NoticeBar(notice: .error(error)) { app.libraryError = nil }.padding(.top, 16)
                }
                if let integrations = app.integrations {
                    section("Connected") { connected(integrations.connections) }
                    if integrations.appsAvailable { section("Apps") { apps(integrations.connections) } }
                    section("Add an MCP server") { AddServerForm() }
                } else if let error = app.libraryError {
                    EmptyState(icon: "exclamationmark.triangle", title: "Couldn't load integrations", text: error) {
                        Button("Try again") { app.libraryError = nil; app.reloadPage() }.buttonStyle(.outline)
                    }
                    .card(padding: 0)
                    .padding(.top, 20)
                } else {
                    MontyMark(mood: .working, size: 22).frame(maxWidth: .infinity).padding(.vertical, 48)
                }
            }
            .frame(maxWidth: Metrics.readingWidth, alignment: .leading)
            .padding(.horizontal, Metrics.gutter)
            .padding(.vertical, 28)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("Integrations")
        .navigationSubtitle("")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { app.libraryError = nil; app.reloadPage() } label: { Label("Reload", systemImage: "arrow.clockwise") }
                    .help("Read this page again (⌘R)")
            }
        }
        .confirmationDialog("Remove \(removing?.name ?? "")?", isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })) {
            Button("Remove", role: .destructive) { if let removing { Task { await app.remove(removing) } } }
        } message: {
            Text("\(app.montyName) will no longer be able to use it. You can connect it again later.")
        }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.system(size: 14, weight: .semibold)).accessibilityAddTraits(.isHeader)
            content()
        }
        .padding(.top, 24)
    }

    @ViewBuilder private func connected(_ connections: [Connection]) -> some View {
        if connections.isEmpty {
            EmptyState(icon: "puzzlepiece.extension", title: "Nothing connected yet",
                       text: "Connect an app below, or ask \(app.montyName) about one in a chat.").card(padding: 0)
        } else {
            VStack(spacing: 0) {
                ForEach(Array(connections.enumerated()), id: \.element.id) { index, connection in
                    if index > 0 { Divider().overlay(Palette.outlineVariant) }
                    ConnectionRow(connection: connection) { removing = connection }
                        .padding(.horizontal, 14).padding(.vertical, 10)
                }
            }
            .card(padding: 0)
        }
    }

    @ViewBuilder private func apps(_ connections: [Connection]) -> some View {
        let connectedApps = Set(connections.filter { $0.isApp && $0.isConnected }.map(\.key))
        TextField("Search apps", text: $search, prompt: Text("Search apps: Linear, GitHub, Gmail…"))
            .labelsHidden()
            .accessibilityLabel("Search apps")
            .field(focused: false)
        if let catalog = app.apps {
            let shown = catalog.filter { $0.matches(search) }
            if shown.isEmpty {
                Text("No app matches “\(search)”. If it has an MCP server, add it below.")
                    .font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: 10)], spacing: 10) {
                    ForEach(shown) { listed in
                        AppTile(listed: listed, connected: connectedApps.contains(listed.slug))
                    }
                }
            }
        } else {
            MontyMark(mood: .working, size: 18).frame(maxWidth: .infinity).padding(.vertical, 24)
        }
    }
}

/// An app's logo, or its first letter while there is none (or it doesn't load).
struct AppLogo: View {
    let url: String
    let name: String
    var size: CGFloat = 32

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerHigh)
            Text(name.prefix(1).uppercased()).font(.system(size: size * 0.45, weight: .semibold)).foregroundStyle(Palette.onSurfaceVariant)
            if let url = URL(string: url), !self.url.isEmpty {
                AsyncImage(url: url) { image in
                    image.resizable().scaledToFit().padding(size * 0.15).background(Palette.containerHigh)
                } placeholder: { EmptyView() }
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
        .accessibilityHidden(true)
    }
}

private struct ConnectionRow: View {
    @Environment(AppModel.self) private var app
    let connection: Connection
    let remove: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            AppLogo(url: connection.logo, name: connection.name)
            VStack(alignment: .leading, spacing: 2) {
                Text(connection.name).font(.system(size: 13, weight: .medium))
                Text(connection.detail).font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(1)
            }
            Spacer(minLength: 8)
            if !connection.isConnected {
                Text(connection.state == "needs_sign_in" ? "Needs you to sign in" : "Not working")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(Palette.onErrorContainer)
                    .padding(.horizontal, 8).padding(.vertical, 3)
                    .background(Capsule().fill(Palette.errorContainer))
                Button(connection.isApp ? "Reconnect" : "Sign in") {
                    Task { if connection.isApp { await app.connect(app: connection.key) } else { await app.signIn(server: connection.id) } }
                }
                .buttonStyle(.monty(.primary, small: true))
            }
            Button("Remove…", action: remove)
                .buttonStyle(.monty(.outline, small: true))
                .accessibilityLabel("Remove \(connection.name)")
        }
    }
}

private struct AppTile: View {
    @Environment(AppModel.self) private var app
    let listed: CatalogApp
    let connected: Bool

    var body: some View {
        HStack(spacing: 10) {
            AppLogo(url: listed.logo, name: listed.name)
            VStack(alignment: .leading, spacing: 2) {
                Text(listed.name).font(.system(size: 13, weight: .medium)).lineLimit(1)
                Text(listed.description).font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(2)
            }
            Spacer(minLength: 4)
            if connected {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(Palette.link).help("Connected")
                    .accessibilityLabel("Connected")
            } else {
                Button("Connect") { Task { await app.connect(app: listed.slug) } }
                    .buttonStyle(.monty(.outline, small: true))
                    .accessibilityLabel("Connect \(listed.name)")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
        .card(padding: 0)
    }
}

private struct AddServerForm: View {
    @Environment(AppModel.self) private var app
    @State private var url = ""
    @State private var header = ""
    @State private var value = ""
    @State private var adding = false
    @FocusState private var focus: Field?

    enum Field { case name, url, header, value }

    var body: some View {
        @Bindable var app = app
        VStack(alignment: .leading, spacing: 10) {
            Text("Any MCP server that speaks streamable HTTP. Leave the header empty if it signs you in itself.")
                .font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
            TextField("Name", text: $app.serverName, prompt: Text("Name, such as My notes"))
                .labelsHidden().accessibilityLabel("Name")
                .focused($focus, equals: .name).field(focused: focus == .name)
            TextField("Server address", text: $url, prompt: Text("https://example.com/mcp"))
                .labelsHidden().accessibilityLabel("Server address")
                .focused($focus, equals: .url).field(focused: focus == .url)
            HStack(spacing: 8) {
                TextField("Header (optional)", text: $header, prompt: Text("Header (optional): Authorization"))
                    .labelsHidden().accessibilityLabel("Header name, optional")
                    .focused($focus, equals: .header).field(focused: focus == .header)
                SecureField("Value", text: $value, prompt: Text("Value: Bearer …"))
                    .labelsHidden().accessibilityLabel("Header value")
                    .focused($focus, equals: .value).field(focused: focus == .value)
            }
            if let note = app.serverNote {
                NoticeBar(notice: note) { app.serverNote = nil }
            }
            Button {
                Task {
                    adding = true
                    defer { adding = false }
                    if await app.addServer(name: app.serverName, url: url, header: header, value: value) {
                        url = ""; header = ""; value = ""  // the key is not left on screen
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    if adding { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                    Text("Add server")
                }
            }
            .buttonStyle(.primary)
            .disabled(adding || app.serverName.trimmingCharacters(in: .whitespaces).isEmpty || url.trimmingCharacters(in: .whitespaces).isEmpty)
        }
        .card(padding: 14)
    }
}
