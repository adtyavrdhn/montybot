import MontyKit
import SwiftUI

/// The user's integrations, grouped as Logfire's Connections page is: the featured ones by kind (chat, code, issues…),
/// then the user's own MCP servers, then every other app. Each row says whether it is connected and what to do next.
/// Signing in happens in the user's browser; the page reads itself again when they come back to Monty.
struct IntegrationsView: View {
    @Environment(AppModel.self) private var app
    @State private var removing: Connection?
    @State private var search = ""
    @State private var addingServer = false
    /// The entry whose token form is open: a known MCP server that takes a token the user pastes (GitHub's).
    @State private var tokenFor: CatalogApp.ID?
    /// Whether "More apps" is open, once the user says; until then it opens when one of them is connected.
    @State private var moreOpen: Bool?

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
                    TextField("Search integrations", text: $search, prompt: Text("Search integrations…"))
                        .labelsHidden()
                        .accessibilityLabel("Search integrations")
                        .field(focused: false)
                        .padding(.top, 20)
                    if let note = app.serverNote, !addingServer {  // the form says it itself while open
                        NoticeBar(notice: note) { app.serverNote = nil }.padding(.top, 16)
                    }
                    groups(integrations)
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
        .onAppear { if !app.serverName.isEmpty { addingServer = true } }  // a chat's "Add an MCP server"
        .onChange(of: app.serverName) { if !app.serverName.isEmpty { addingServer = true } }
        .onChange(of: search) { moreOpen = nil }
        .confirmationDialog(
            "\(removing?.isConnected == true ? "Disconnect" : "Remove") \(removing?.name ?? "")?",
            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } })
        ) {
            Button(removing?.isConnected == true ? "Disconnect" : "Remove", role: .destructive) {
                if let removing { Task { await app.remove(removing) } }
            }
        } message: {
            Text("\(app.montyName) will no longer be able to use it. You can connect it again later.")
        }
    }

    @ViewBuilder private func groups(_ integrations: Integrations) -> some View {
        let catalog = app.apps ?? []
        let connections = integrations.connections
        let shown = catalog.filter { $0.matches(search) }
        let featured = shown.filter(\.featured)
        let more = shown.filter { !$0.featured }
        // The user's own servers, and anything else no entry covers (an app the catalog no longer lists).
        let custom = connections.filter { connection in !catalog.contains { $0.matches(connection) } }.filter(matches)
        if app.apps == nil {
            MontyMark(mood: .working, size: 18).frame(maxWidth: .infinity).padding(.vertical, 24)
        }
        ForEach(kinds(featured), id: \.self) { kind in
            let entries = featured.filter { ($0.kind ?? "") == kind }
            section(entries[0].kindLabel ?? kind.capitalized, count: entries.count) { rows(entries, connections) }
        }
        if featured.isEmpty && more.isEmpty && custom.isEmpty && !search.trimmingCharacters(in: .whitespaces).isEmpty {
            Text("No integration matches “\(search)”. If it has an MCP server, add it under Custom.")
                .font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant).padding(.top, 24)
        }
        section("Custom", count: custom.isEmpty ? nil : custom.count) {
            ForEach(custom) { connection in
                IntegrationRow(name: connection.name, logo: connection.logo, subtitle: connection.detail, connection: connection,
                               connect: {}, remove: { removing = $0 })
                Divider().overlay(Palette.outlineVariant)
            }
            addServerRow
        }
        if !more.isEmpty {
            let anyConnected = more.contains { entry in connections.contains { entry.matches($0) } }
            DisclosureGroup(isExpanded: Binding(get: { moreOpen ?? (anyConnected || !search.isEmpty) }, set: { moreOpen = $0 })) {
                VStack(spacing: 0) { rows(more, connections) }.card(padding: 0).padding(.top, 10)
            } label: {
                header("More apps", count: more.count)
            }
            .padding(.top, 24)
        }
    }

    /// The featured kinds, in the order the server lists them.
    private func kinds(_ featured: [CatalogApp]) -> [String] {
        featured.reduce(into: []) { kinds, entry in if !kinds.contains(entry.kind ?? "") { kinds.append(entry.kind ?? "") } }
    }

    private func matches(_ connection: Connection) -> Bool {
        let query = search.trimmingCharacters(in: .whitespaces)
        return query.isEmpty || [connection.name, connection.key, connection.detail].contains { $0.localizedCaseInsensitiveContains(query) }
    }

    @ViewBuilder private func rows(_ entries: [CatalogApp], _ connections: [Connection]) -> some View {
        LazyVStack(spacing: 0) {
            ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                if index > 0 { Divider().overlay(Palette.outlineVariant) }
                let connection = connections.first { entry.matches($0) }
                IntegrationRow(
                    name: entry.name, logo: entry.logo, subtitle: entry.description,
                    connection: connection,
                    connect: {
                        if entry.isApp {
                            await app.connect(app: entry.key)
                        } else if entry.needsToken {
                            tokenFor = tokenFor == entry.id ? nil : entry.id
                        } else {
                            await app.connect(preset: entry)
                        }
                    },
                    remove: { removing = $0 }
                )
                if tokenFor == entry.id, connection == nil {
                    Divider().overlay(Palette.outlineVariant)
                    TokenForm(entry: entry) { tokenFor = nil }
                }
            }
        }
    }

    private var addServerRow: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "plus").font(.system(size: 13, weight: .medium)).foregroundStyle(Palette.onSurfaceVariant)
                    .frame(width: 28, height: 28)
                    .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerHigh))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Add a custom MCP server").font(.system(size: 13, weight: .semibold))
                    Text("Any MCP server that speaks streamable HTTP.").font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
                Button(addingServer ? "Cancel" : "Add") { addingServer.toggle() }
                    .buttonStyle(.monty(.outline, small: true))
                    .accessibilityLabel(addingServer ? "Cancel adding an MCP server" : "Add a custom MCP server")
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            if addingServer {
                Divider().overlay(Palette.outlineVariant)
                AddServerForm()
            }
        }
    }

    private func section<Content: View>(_ title: String, count: Int?, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            header(title, count: count)
            VStack(spacing: 0) { content() }.card(padding: 0)
        }
        .padding(.top, 24)
    }

    private func header(_ title: String, count: Int?) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.system(size: 14, weight: .semibold))
            if let count { Text("\(count)").font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
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

/// One integration: its name, how it is (when connected at all), a line on what it is, and what the user can do now.
private struct IntegrationRow: View {
    @Environment(AppModel.self) private var app
    let name: String
    let logo: String
    let subtitle: String
    let connection: Connection?
    let connect: () async -> Void
    let remove: (Connection) -> Void

    var body: some View {
        HStack(spacing: 12) {
            AppLogo(url: logo, name: name, size: 28)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 8) {
                    Text(name).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        .foregroundStyle(connection == nil ? Palette.onSurfaceVariant : Palette.onSurface)
                    if let connection { StatusBadge(state: connection.state) }
                }
                if !subtitle.isEmpty {
                    Text(subtitle).font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(1).truncationMode(.tail)
                }
            }
            Spacer(minLength: 8)
            actions
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    @ViewBuilder private var actions: some View {
        if let connection {
            if !connection.isConnected {
                Button(connection.state == "needs_sign_in" ? "Sign in" : "Reconnect") {
                    Task { if connection.isApp { await app.connect(app: connection.key) } else { await app.signIn(server: connection.id) } }
                }
                .buttonStyle(.monty(.primary, small: true))
                .accessibilityLabel("\(connection.state == "needs_sign_in" ? "Sign in to" : "Reconnect") \(name)")
            }
            Button(connection.isConnected ? "Disconnect" : "Remove") { remove(connection) }
                .buttonStyle(.monty(.outline, small: true))
                .accessibilityLabel("\(connection.isConnected ? "Disconnect" : "Remove") \(name)")
        } else {
            Button("Connect") { Task { await connect() } }
                .buttonStyle(.monty(.outline, small: true))
                .accessibilityLabel("Connect \(name)")
        }
    }
}

/// Connected, or what is wrong with a connection, beside its name.
private struct StatusBadge: View {
    let state: String

    var body: some View {
        if state == "connected" {
            Badge(text: "Connected")
        } else {
            Text(state == "needs_sign_in" ? "Needs you to sign in" : "Not working")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(Palette.onErrorContainer)
                .padding(.horizontal, 8).padding(.vertical, 3)
                .background(Capsule().fill(Palette.errorContainer))
                .fixedSize()
        }
    }
}

/// A known MCP server that takes a token the user pastes (GitHub's personal access token), rather than a sign-in.
private struct TokenForm: View {
    @Environment(AppModel.self) private var app
    let entry: CatalogApp
    let close: () -> Void
    @State private var token = ""
    @State private var adding = false
    @FocusState private var focused: Bool

    var body: some View {
        HStack(spacing: 8) {
            SecureField("Token", text: $token, prompt: Text(entry.tokenHint ?? "Token"))
                .labelsHidden().accessibilityLabel("Token for \(entry.name)")
                .focused($focused).field(focused: focused)
                .onSubmit(connect)
            Button(action: connect) {
                HStack(spacing: 6) {
                    if adding { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                    Text("Connect")
                }
            }
            .buttonStyle(.monty(.primary, small: true))
            .disabled(adding || token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel("Connect \(entry.name) with this token")
            Button("Cancel", action: close)
                .buttonStyle(.monty(.ghost, small: true))
                .accessibilityLabel("Cancel connecting \(entry.name)")
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .onAppear { focused = true }
    }

    private func connect() {
        guard !adding, !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        Task {
            adding = true
            defer { adding = false }
            app.serverNote = nil
            if await app.connect(preset: entry, token: token) {
                token = ""  // the token is not left on screen
                close()
            }
        }
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
            Text("Leave the header empty if the server signs you in itself.")
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
        .padding(14)
    }
}
