import MontyKit
import SwiftUI

struct MainView: View {
    @Environment(AppModel.self) private var app

    var body: some View {
        NavigationSplitView {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 220, ideal: 256, max: 340)
        } detail: {
            Group {
                switch app.route {
                case .chat:
                    if let chat = app.chat { ChatView(chat: chat).id(ObjectIdentifier(chat)) }
                case .schedules: SchedulesView()
                case .files: FilesView()
                case .signIns: SavedSitesView()
                case .memory: MemoryView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Palette.surface)
        }
        .toolbar(app.chat?.live == nil ? .automatic : .hidden, for: .windowToolbar)
        .accessibilityHidden(app.chat?.live != nil)
        .allowsHitTesting(app.chat?.live == nil)
        .overlay {
            // Taking over the browser fills the window: the dogfood found a side panel too small to sign in with.
            if let chat = app.chat, let live = chat.live {
                TakeoverView(chat: chat, live: live)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: app.chat?.live == nil)
    }
}

struct Sidebar: View {
    @Environment(AppModel.self) private var app
    @State private var search = ""
    @State private var renaming: ThreadSummary?
    @State private var newTitle = ""
    @State private var deleting: ThreadSummary?

    private var selection: Binding<Route?> {
        Binding(get: { app.route }, set: { if let route = $0 { app.open(route) } })
    }

    var body: some View {
        List(selection: selection) {
            let needs = filtered(app.needsYou)
            if !needs.isEmpty {
                Section {
                    ForEach(needs) { row($0) }
                } header: {
                    Text("Needs you").accessibilityLabel("Needs you, \(needs.count) chat\(needs.count == 1 ? "" : "s")")
                }
            }

            Section("Library") {
                Label("Schedules", systemImage: "calendar.badge.clock").tag(Route.schedules)
                Label("Files", systemImage: "doc.on.doc").tag(Route.files)
                Label("Saved sign-ins", systemImage: "key").tag(Route.signIns)
                Label("Memory", systemImage: "brain").tag(Route.memory)
            }

            Section("Chats") {
                let rest = filtered(app.threads.filter { $0.status != .waiting })
                if !app.threadsLoaded {
                    ForEach([150, 110, 170], id: \.self) { SkeletonRow(width: $0) }
                } else if rest.isEmpty {
                    Text(search.isEmpty ? (needs.isEmpty ? "Your chats with Monty appear here." : "No other chats.") : "No chats match “\(search)”.")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .selectionDisabled()
                }
                ForEach(rest) { row($0) }
            }
        }
        .listStyle(.sidebar)
        .tint(Palette.link)
        .onDeleteCommand {  // ⌘⌫ on the selected chat
            if case .chat(let id?) = app.route { deleting = app.threads.first { $0.id == id } }
        }
        .alert("Rename chat", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $newTitle)
            Button("Rename") { if let renaming { Task { await app.rename(renaming, to: newTitle) } } }
            Button("Cancel", role: .cancel) {}
        }
        .confirmationDialog(
            "Delete “\(deleting?.title.readableTitle ?? "")”?",
            isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })
        ) {
            Button("Delete chat", role: .destructive) { if let deleting { Task { await app.delete(deleting) } } }
        } message: {
            Text(deleteMessage)
        }
        .searchable(text: $search, placement: .sidebar, prompt: Text("Search chats"))
        .toolbar {
            ToolbarItem {
                Button { app.open(.chat(nil)) } label: { Label("New task", systemImage: "square.and.pencil") }
                    .help("New task (⌘N)")
                    .disabled(app.isTakingOver)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if app.offline {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini).accessibilityHidden(true)
                    Text("Connection lost. Reconnecting…")
                }
                .font(.system(size: 12))
                .foregroundStyle(Palette.onErrorContainer)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.errorContainer)
            }
        }
    }

    private func row(_ thread: ThreadSummary) -> some View {
        ThreadRow(thread: thread)
            .tag(Route.chat(thread.id))
            .contextMenu {
                Button("Rename…") { newTitle = thread.title.readableTitle; renaming = thread }
                Divider()
                Button("Delete…", role: .destructive) { deleting = thread }
            }
            .accessibilityAction(named: "Rename") { newTitle = thread.title.readableTitle; renaming = thread }
            .accessibilityAction(named: "Delete") { deleting = thread }
    }

    private var deleteMessage: String {
        guard let deleting else { return "" }
        let stops = deleting.status != nil ? " Monty stops the task it is doing there." : ""
        return "The chat and everything in it are deleted, along with a schedule that reports there.\(stops) This can't be undone."
    }

    private func filtered(_ threads: [ThreadSummary]) -> [ThreadSummary] {
        let query = search.trimmingCharacters(in: .whitespaces)
        return query.isEmpty ? threads : threads.filter { $0.title.localizedCaseInsensitiveContains(query) }
    }
}

/// A chat in the sidebar: its title, and a mark for what Monty is doing in it.
struct ThreadRow: View {
    let thread: ThreadSummary

    var body: some View {
        HStack(spacing: 6) {
            Text(thread.title.readableTitle).lineLimit(1)
            Spacer(minLength: 4)
            switch thread.status {
            case .waiting:
                Circle().fill(Palette.logfire).frame(width: 7, height: 7).accessibilityHidden(true)
            case .running, .queued:
                ProgressView().controlSize(.mini).accessibilityHidden(true)
            default:
                switch thread.outcome {
                case .failed:
                    Image(systemName: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                        .help("Monty couldn't finish this task")
                case .stopped:
                    Image(systemName: "stop.circle").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                        .help("You stopped this task")
                default:
                    EmptyView()
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(thread.title.readableTitle)
        .accessibilityValue(statusText)
    }

    private var statusText: String {
        switch (thread.status, thread.outcome) {
        case (.waiting, _): "Needs you"
        case (.running, _), (.queued, _): "Working"
        case (_, .failed): "Couldn't finish"
        case (_, .stopped): "Stopped"
        default: ""
        }
    }
}

struct SkeletonRow: View {
    var width: CGFloat = 140
    var body: some View {
        RoundedRectangle(cornerRadius: 3)
            .fill(Palette.containerHigh)
            .frame(width: width, height: 9)
            .selectionDisabled()
            .accessibilityHidden(true)
    }
}
