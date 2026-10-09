import SammyKit
import SwiftUI

struct MainView: View {
    @Environment(AppModel.self) private var app
    @State private var newTitle = ""
    @Environment(\.undoManager) private var undoManager

    var body: some View {
        Group {
            // Sammy's browser fills the window in place of the chat: to take over (the dogfood found a side panel too
            // small to sign in with), or to watch it full size. In place of, not over: an overlay left the chat laid
            // out larger than the window afterwards, its message box below the window's edge.
            if let chat = app.chat, let live = chat.live {
                TakeoverView(chat: chat, live: live)
                    .navigationTitle(chat.title.isEmpty ? "Sammy" : chat.title.readableTitle)
                    .transition(.opacity.combined(with: .scale(scale: 0.97)))
            } else if let chat = app.chat, chat.browserExpanded {
                ExpandedBrowserView(chat: chat)
                    .navigationTitle(chat.title.isEmpty ? "Sammy" : chat.title.readableTitle)
                    .transition(.opacity.combined(with: .scale(scale: 0.97)))
            } else {
                split.frame(minWidth: app.chat?.watching == true ? Metrics.windowWithBrowserMinWidth : nil)
            }
        }
        .overlay {
            if app.showingPalette, app.chat?.live == nil {
                CommandPalette(isPresented: Binding(get: { app.showingPalette }, set: { app.showingPalette = $0 }))
            }
        }
        .motion(.spring(response: 0.38, dampingFraction: 0.9), value: app.chat?.live == nil)
        .motion(.spring(response: 0.38, dampingFraction: 0.9), value: app.chat?.browserExpanded)
        .onChange(of: app.chat?.watching) { _, watching in
            // Sammy's browser beside the chat needs a wide window: widen it, as Xcode does for its inspector. A screen
            // too small for that shows the browser filling the window instead.
            guard watching == true, let chat = app.chat, !chat.browserExpanded else { return }
            if !widenWindow(to: Metrics.windowWithBrowserMinWidth) { chat.browserExpanded = true }
        }
        .alert("Rename chat", isPresented: Binding(get: { app.renaming != nil }, set: { if !$0 { app.renaming = nil } })) {
            TextField("Title", text: $newTitle)
            Button("Rename") {
                // The field shows the readable title: unchanged, it renames nothing.
                if let thread = app.renaming, newTitle != thread.title.readableTitle { Task { await app.rename(thread, to: newTitle) } }
            }
            Button("Cancel", role: .cancel) {}
        }
        .onChange(of: app.renaming) { _, thread in if let thread { newTitle = thread.title.readableTitle } }
        // No "are you sure": the chat goes at once, and Undo (in the sidebar, Edit menu or ⌘Z) brings it back for a moment.
        .onChange(of: app.deleting) { _, thread in
            guard let thread else { return }
            app.deleting = nil
            app.deleteWithUndo(thread)
            undoManager?.removeAllActions(withTarget: app)
            undoManager?.registerUndo(withTarget: app) { app in app.undoDelete() }
            undoManager?.setActionName("Delete Chat")
        }
        .onChange(of: app.recentlyDeleted == nil) { _, gone in
            if gone { undoManager?.removeAllActions(withTarget: app) }  // deleted for good, or put back
        }
        .alert(
            app.actionError ?? "",
            isPresented: Binding(get: { app.actionError != nil }, set: { if !$0 { app.actionError = nil } })
        ) {
            Button("OK", role: .cancel) {}
        }
    }

    private var split: some View {
        NavigationSplitView {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 220, ideal: 256, max: 340)
        } detail: {
            Group {
                switch app.route {
                case .chat:
                    if let chat = app.chat { ChatView(chat: chat).id(ObjectIdentifier(chat)) }
                case .schedules: SchedulesView()
                case .signIns: SavedSitesView()
                case .integrations: IntegrationsView()
                case .memory: MemoryView()
                case .skills: SkillsView()
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Palette.surface)
            // Without a width of its own the chat's column asked for more room than the window allows, and was
            // clipped at both sides in a narrow window, or with Sammy's browser open beside it.
            .navigationSplitViewColumnWidth(min: 340, ideal: 720)
        }
    }

    /// Makes the main window at least `width` wide, staying on its screen; false if the screen is narrower than that.
    private func widenWindow(to width: CGFloat) -> Bool {
        guard let window = NSApp.windows.first(where: { $0.identifier?.rawValue.hasPrefix("main") == true }) ?? NSApp.keyWindow,
              let screen = window.screen?.visibleFrame
        else { return true }
        guard window.frame.width < width else { return true }
        guard !window.styleMask.contains(.fullScreen), screen.width >= width else { return false }
        var frame = window.frame
        frame.origin.x -= (width - frame.width) / 2  // grows on both sides, as zooming does
        frame.size.width = width
        frame.origin.x = min(max(frame.origin.x, screen.minX), screen.maxX - width)
        window.setFrame(frame, display: true, animate: true)
        return true
    }

}

struct Sidebar: View {
    @Environment(AppModel.self) private var app
    @State private var search = ""
    /// Chats whose tasks or replies mention the search, as the server found them, with the words they answer: they
    /// count only for those words, so a new search never shows the last one's chats.
    @State private var contentMatches: (query: String, ids: Set<String>) = ("", [])

    private var selection: Binding<Route?> {
        Binding(get: { app.route }, set: { if let route = $0 { app.open(route) } })
    }

    // Laid out as Codex's sidebar: the window's buttons alone at the top, then New task as the first row (selected
    // while you are on it), then the chats, and the library pinned at the bottom, out of the chats' way.
    var body: some View {
        List(selection: selection) {
            Label("New task", systemImage: "square.and.pencil")
                .badge(Text("⌘N").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant))
                .help("Start a new task (⌘N)")
                .tag(Route.chat(nil))

            let needs = filtered(app.needsYou)
            if !needs.isEmpty {
                Section {
                    ForEach(needs) { row($0) }
                } header: {
                    Text("Needs you").accessibilityLabel("Needs you, \(needs.count) chat\(needs.count == 1 ? "" : "s")")
                }
            }

            // Pinned chats, then the rest by when they were last active, as ChatGPT's sidebar. A chat that needs the
            // user is only under "Needs you", above.
            let pinned = filtered(app.pinned.compactMap { id in app.threads.first { $0.id == id && $0.status != .waiting } })
            if !pinned.isEmpty {
                Section("Pinned") { ForEach(pinned) { row($0) } }
            }
            let rest = filtered(app.threads.filter { $0.status != .waiting && !app.pinned.contains($0.id) })
            if !app.threadsLoaded {
                Section("Chats") { ForEach([150, 110, 170], id: \.self) { SkeletonRow(width: $0) } }
            } else if rest.isEmpty, pinned.isEmpty {
                Section("Chats") {
                    Text(search.isEmpty ? (needs.isEmpty ? "Your chats with \(app.sammyName) appear here." : "No other chats.") : "No chats match “\(search)”.")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .selectionDisabled()
                }
            } else if rest.contains(where: { $0.updatedAt != nil }) {
                ForEach(ChatAge.allCases, id: \.self) { age in
                    let chats = rest.filter { ChatAge.of($0.updatedAt) == age }
                    if !chats.isEmpty { Section(age.rawValue) { ForEach(chats) { row($0) } } }
                }
            } else if !rest.isEmpty {
                Section("Chats") { ForEach(rest) { row($0) } }  // a server that doesn't say when
            }
        }
        .listStyle(.sidebar)
        .tint(Palette.link)
        .onDeleteCommand { app.deleting = app.openThread }  // ⌫ or ⌘⌫ on the selected chat
        .searchable(text: $search, placement: .sidebar, prompt: Text("Search chats"))
        .task(id: search) {
            let query = search.trimmingCharacters(in: .whitespaces)
            guard query.count >= 2 else { return }
            try? await Task.sleep(for: .milliseconds(250))  // once typing pauses
            guard !Task.isCancelled else { return }
            let found = await app.search(query)
            if !Task.isCancelled { contentMatches = (query, found) }
        }
        .modifier(FocusedSearch())
        // Nothing beside the window's buttons: the sidebar's own toggle is in the View menu (⌃⌘S).
        .toolbar(removing: .sidebarToggle)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                if let deleted = app.recentlyDeleted {
                    UndoDeleteBar(title: deleted.title.readableTitle) { app.undoDelete() }
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
                Divider().overlay(Palette.outline)
                VStack(spacing: 1) {
                    LibraryLink(title: "Schedules", icon: "calendar.badge.clock", route: .schedules)
                    LibraryLink(title: "Saved sign-ins", icon: "key", route: .signIns)
                    LibraryLink(title: "Integrations", icon: "puzzlepiece.extension", route: .integrations)
                    LibraryLink(title: "Memory", icon: "brain", route: .memory)
                    LibraryLink(title: "Skills", icon: "book", route: .skills)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Library")
                offlineBanner
            }
            .animation(.easeOut(duration: 0.2), value: app.recentlyDeleted?.id)
        }
    }

    @ViewBuilder private var offlineBanner: some View {
        if app.offline {
            HStack(spacing: 6) {
                SammyMark(mood: .failed, size: 9).frame(width: 16, height: 16)
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

    private func row(_ thread: ThreadSummary) -> some View {
        ThreadRow(
            thread: thread, unseen: app.unseen.contains(thread.id), pinned: app.pinned.contains(thread.id),
            hasDraft: app.chat?.threadId != thread.id
                && !(app.drafts[thread.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
            pin: { app.setPinned(thread, !app.pinned.contains(thread.id)) }, delete: { app.deleting = thread }
        )
            .tag(Route.chat(thread.id))
            .contextMenu {
                let isPinned = app.pinned.contains(thread.id)
                Button(isPinned ? "Unpin" : "Pin") { app.setPinned(thread, !isPinned) }
                if app.unseen.contains(thread.id) {
                    Button("Mark as Read") { app.markSeen(thread) }
                } else if thread.status == nil, app.route != .chat(thread.id) {  // open, it would be read at once
                    Button("Mark as Unread") { app.markUnread(thread) }
                }
                Button("Rename…") { app.renaming = thread }
                Divider()
                Button("Delete…", role: .destructive) { app.deleting = thread }
            }
            .swipeActions(edge: .trailing) {
                Button(role: .destructive) { app.deleting = thread } label: { Label("Delete", systemImage: "trash") }
            }
            .accessibilityAction(named: app.pinned.contains(thread.id) ? "Unpin" : "Pin") {
                app.setPinned(thread, !app.pinned.contains(thread.id))
            }
            .accessibilityAction(named: "Rename") { app.renaming = thread }
            .accessibilityAction(named: "Delete") { app.deleting = thread }
    }

    private func filtered(_ threads: [ThreadSummary]) -> [ThreadSummary] {
        let query = search.trimmingCharacters(in: .whitespaces)
        return query.isEmpty ? threads : threads.filter {
            $0.title.localizedCaseInsensitiveContains(query) || (contentMatches.query == query && contentMatches.ids.contains($0.id))
        }
    }
}

/// "Deleted “…”. Undo", for the moment a deleted chat can still come back.
struct UndoDeleteBar: View {
    let title: String
    let undo: () -> Void

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "trash").font(.system(size: 11)).accessibilityHidden(true)
            Text("Deleted “\(title)”").lineLimit(1).truncationMode(.middle)
            Spacer(minLength: 4)
            Button("Undo", action: undo)
                .buttonStyle(.link)
                .help("Bring the chat back (⌘Z)")
        }
        .font(.system(size: 12))
        .foregroundStyle(Palette.onSurfaceVariant)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Palette.containerHigh.opacity(0.6))
        .onAppear { AccessibilityNotification.Announcement("Deleted \(title). Undo is available.").post() }
    }
}

/// A page of the library, pinned under the chats: a row that looks selected while it is open.
struct LibraryLink: View {
    @Environment(AppModel.self) private var app
    let title: String
    let icon: String
    let route: Route
    @State private var hovering = false

    var body: some View {
        let selected = app.route == route
        Button { app.open(route) } label: {
            Label(title, systemImage: icon)
                .font(.system(size: 13))
                .foregroundStyle(Palette.onSurface)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 8)
                .frame(height: 26)
                .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                    .fill(selected ? Palette.containerHighest : hovering ? Palette.containerHigh.opacity(0.6) : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .disabled(app.isTakingOver)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// A chat in the sidebar: its title, and a mark for what Sammy is doing in it. On hover, a button to delete it, as
/// Codex shows Archive.
struct ThreadRow: View {
    @Environment(AppModel.self) private var app
    let thread: ThreadSummary
    /// Finished while the user looked elsewhere: bold, with a dot, until they open it, as unread mail.
    var unseen = false
    var pinned = false
    /// Holds a message the user started and hasn't sent, as T3 Code marks an unsent draft.
    var hasDraft = false
    var pin: (() -> Void)?
    var delete: (() -> Void)?
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 6) {
            Text(thread.title.readableTitle).lineLimit(1).fontWeight(unseen && thread.status == nil ? .semibold : nil)
            Spacer(minLength: 4)
            if hovering, let delete {
                if let pin {
                    Button(action: pin) { Image(systemName: pinned ? "pin.slash" : "pin").font(.system(size: 11)) }
                        .buttonStyle(IconButtonStyle(size: 20))
                        .help(pinned ? "Unpin" : "Pin to the top")
                }
                Button(action: delete) { Image(systemName: "trash").font(.system(size: 11)) }
                    .buttonStyle(IconButtonStyle(size: 20))
                    .help("Delete this chat")
            } else {
                mark
            }
        }
        .onHover { hovering = $0 }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(thread.title.readableTitle)
        .accessibilityValue(statusText)
    }

    @ViewBuilder private var mark: some View {
        switch thread.status {
        case .waiting:
            // What it needs, in a word, as T3 Code's status pills: "Approval" says more than a dot.
            HStack(spacing: 5) {
                if let kind = thread.waitingFor {
                    Text(Self.word(for: kind)).font(.system(size: 11, weight: .medium)).foregroundStyle(Palette.onSurfaceVariant)
                }
                Circle().fill(Palette.logfire).frame(width: 7, height: 7)
            }
            .accessibilityHidden(true)
        case .running, .queued:
            SammyMark(mood: .working, size: 9).frame(width: 16, height: 16)
        default:
            switch thread.outcome {
            case _ where unseen:
                Circle().fill(Palette.link).frame(width: 7, height: 7)
                    .help(thread.outcome == .failed ? "\(app.sammyName) couldn't finish this task" : "\(app.sammyName) finished: you haven't seen it yet")
            case _ where hasDraft:
                Image(systemName: "pencil.line").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                    .help("You started a message here and haven't sent it")
            case .failed:
                Image(systemName: "exclamationmark.circle").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                    .help("\(app.sammyName) couldn't finish this task")
            case .stopped:
                Image(systemName: "stop.circle").font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                    .help("You stopped this task")
            default:
                EmptyView()
            }
        }
    }

    static func word(for kind: AskKind) -> String {
        switch kind {
        case .question: "Question"
        case .approval: "Approval"
        case .handoff: "Browser"
        case .connect: "Connect"
        case .other: "Needs you"
        }
    }

    private var statusText: String {
        switch (thread.status, thread.outcome) {
        case (.waiting, _): thread.waitingFor.map { "Needs you: \(Self.word(for: $0).lowercased())" } ?? "Needs you"
        case (.running, _), (.queued, _): "Working"
        case (_, .failed) where unseen: "Couldn't finish, not seen yet"
        case _ where unseen: "New reply"
        case _ where hasDraft: "Unsent draft"
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

/// ⌘F puts the keyboard in the sidebar's search (macOS 15 can move the focus there; 14 can't, and leaves it be).
private struct FocusedSearch: ViewModifier {
    @Environment(AppModel.self) private var app
    @FocusState private var focused: Bool

    func body(content: Content) -> some View {
        if #available(macOS 15, *) {
            content
                .searchFocused($focused)
                .onChange(of: app.wantsFindChats) { _, wants in if wants { focused = true; app.wantsFindChats = false } }
                .onAppear { if app.wantsFindChats { focused = true; app.wantsFindChats = false } }  // asked while closed
        } else {
            content
        }
    }
}
