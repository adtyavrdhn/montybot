import AppKit
import SammyKit
import SwiftUI

/// ⌘K, as T3 Code's command palette: chats and actions in one list, found by typing. Empty, it offers a new task, the
/// recent chats, and what can be done to the open one; ↑↓ choose, ↩ does it, esc closes.
struct CommandPalette: View {
    @Environment(AppModel.self) private var app
    @Binding var isPresented: Bool
    @State private var query = ""
    @State private var selected = 0
    @FocusState private var focused: Bool

    struct Item: Identifiable {
        let id: String
        let title: String
        var detail: String? = nil
        let icon: String
        var shortcut: String? = nil
        let action: () -> Void
    }

    var body: some View {
        let items = results
        ZStack(alignment: .top) {
            Color.black.opacity(0.18)
                .ignoresSafeArea()
                .onTapGesture { isPresented = false }
                .accessibilityHidden(true)
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(Palette.onSurfaceVariant).accessibilityHidden(true)
                    TextField("Search chats and actions", text: $query)
                        .textFieldStyle(.plain)
                        .font(.system(size: 15))
                        .focused($focused)
                        .onSubmit { run(items) }
                        .onKeyPress(.downArrow) { move(1, in: items); return .handled }
                        .onKeyPress(.upArrow) { move(-1, in: items); return .handled }
                        .onExitCommand { isPresented = false }
                        .accessibilityLabel("Search chats and actions")
                }
                .padding(.horizontal, 14)
                .frame(height: 46)
                Divider().overlay(Palette.outline)
                if items.isEmpty {
                    Text("Nothing matches “\(query)”.")
                        .font(.system(size: 13))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 22)
                } else {
                    ScrollViewReader { scroller in
                        ScrollView {
                            VStack(spacing: 2) {
                                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                    row(item, selected: index == selected)
                                        .id(item.id)
                                        .onTapGesture { selected = index; run(items) }
                                        .onHover { if $0 { selected = index } }
                                }
                            }
                            .padding(6)
                        }
                        .frame(maxHeight: 360)
                        .onChange(of: selected) { _, index in
                            if items.indices.contains(index) { scroller.scrollTo(items[index].id) }
                        }
                    }
                }
            }
            .frame(width: 560)
            .fixedSize(horizontal: false, vertical: true)
            .background(RoundedRectangle(cornerRadius: 12).fill(Palette.container))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Palette.outline))
            .shadow(color: .black.opacity(0.18), radius: 24, y: 10)
            .padding(.top, 90)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Command palette")
        }
        .onAppear { focused = true }
        .onChange(of: query) { selected = 0 }
        // The list can change while open (a task finishes, a chat arrives): the choice stays on a row.
        .onChange(of: items.map(\.id)) { _, ids in selected = min(selected, max(ids.count - 1, 0)) }
    }

    private func row(_ item: Item, selected: Bool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: item.icon)
                .font(.system(size: 12))
                .frame(width: 18)
                .foregroundStyle(Palette.onSurfaceVariant)
                .accessibilityHidden(true)
            Text(item.title).lineLimit(1).foregroundStyle(Palette.onSurface)
            if let detail = item.detail {
                Text(detail).lineLimit(1).foregroundStyle(Palette.onSurfaceVariant)
            }
            Spacer(minLength: 8)
            if let shortcut = item.shortcut {
                Text(shortcut).font(.mono(11)).foregroundStyle(Palette.onSurfaceVariant)
            }
        }
        .font(.system(size: 13))
        .padding(.horizontal, 10)
        .frame(height: 32)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(selected ? Palette.containerHighest : .clear))
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([item.title, item.detail].compactMap { $0 }.joined(separator: ", "))
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
    }

    private func move(_ step: Int, in items: [Item]) {
        guard !items.isEmpty else { return }
        selected = (selected + step + items.count) % items.count
    }

    private func run(_ items: [Item]) {
        guard items.indices.contains(selected) else { return }
        isPresented = false
        items[selected].action()
    }

    // MARK: what it offers

    private var results: [Item] {
        let query = query.trimmingCharacters(in: .whitespaces)
        let chats = app.sidebarOrder.map { thread in
            Item(
                id: "chat-\(thread.id)", title: thread.title.readableTitle,
                detail: thread.status == .waiting ? "needs you" : thread.status?.isWorking == true ? "working" : nil,
                icon: thread.status == .waiting ? "exclamationmark.bubble" : "bubble.left", action: { app.open(.chat(thread.id)) }
            )
        }
        guard !query.isEmpty else { return [actions[0]] + chats.prefix(8) + actions.dropFirst() }
        let matches = { (item: Item) in item.title.localizedStandardContains(query) }
        // Actions first when their name matches, then chats by title.
        return actions.filter(matches) + chats.filter(matches)
    }

    private var actions: [Item] {
        var items = [Item(id: "new", title: "New task", icon: "square.and.pencil", shortcut: "⌘N") { app.open(.chat(nil)) }]
        if let chat = app.chat {
            if chat.canStop {
                items.append(Item(id: "stop", title: "Stop task", icon: "stop.fill", shortcut: "⌘.") { Task { await chat.stop() } })
            }
            if chat.canRetry {
                items.append(Item(id: "retry", title: "Try again", icon: "arrow.clockwise", shortcut: "⌘R") { Task { await chat.retry() } })
                items.append(Item(id: "edit", title: "Edit and send again", icon: "pencil") { chat.editLastTask() })
            }
            if chat.run != nil {
                items.append(Item(id: "watch", title: chat.watching ? "Hide \(app.sammyName)'s browser" : "Watch \(app.sammyName)'s browser", icon: "macwindow", shortcut: "⇧⌘B") {
                    chat.watching.toggle()
                })
                items.append(Item(
                    id: "expand", title: chat.browserExpanded ? "Back to the chat" : "Expand \(app.sammyName)'s browser",
                    icon: chat.browserExpanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right",
                    shortcut: "⇧⌘F"
                ) { chat.browserExpanded.toggle() })
            }
        }
        if let thread = app.openThread {
            let pinned = app.pinned.contains(thread.id)
            items.append(Item(id: "pin", title: pinned ? "Unpin chat" : "Pin chat", icon: pinned ? "pin.slash" : "pin") {
                app.setPinned(thread, !pinned)
            })
            items.append(Item(id: "rename", title: "Rename chat…", icon: "character.cursor.ibeam") { app.renaming = thread })
            items.append(Item(id: "delete", title: "Delete chat", icon: "trash") { app.deleting = thread })
        }
        items += [
            Item(id: "schedules", title: "Schedules", icon: "calendar.badge.clock", shortcut: "⇧⌘1") { app.open(.schedules) },
            Item(id: "signins", title: "Saved sign-ins", icon: "key", shortcut: "⇧⌘2") { app.open(.signIns) },
            Item(id: "memory", title: "Memory", icon: "brain", shortcut: "⇧⌘3") { app.open(.memory) },
            Item(id: "integrations", title: "Integrations", icon: "puzzlepiece.extension", shortcut: "⇧⌘4") { app.open(.integrations) },
            Item(id: "settings", title: "Settings…", icon: "gearshape", shortcut: "⌘,") {
                NSApp.sendAction(Selector(("showSettingsWindow:")), to: nil, from: nil)
            },
        ]
        return items
    }
}
