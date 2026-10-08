import AppKit
import SammyKit
import SwiftUI

/// Back, forward, and reload (Stop while the page loads), as a browser's toolbar has them. All off for a browser
/// without them (`LiveSession.controls`); back and forward also when the tab has no history that way.
struct NavigationButtons: View {
    let live: LiveSession

    var body: some View {
        let tab = live.activeTab
        HStack(spacing: 2) {
            button("chevron.left", "Back", shortcut: "⌘[", .back, enabled: tab?.canGoBack != false)
            button("chevron.right", "Forward", shortcut: "⌘]", .forward, enabled: tab?.canGoForward != false)
            if tab?.loading == true {
                button("xmark", "Stop", shortcut: nil, .stop, enabled: true)
            } else {
                button("arrow.clockwise", "Reload", shortcut: "⌘R", .reload, enabled: true)
            }
        }
    }

    private func button(_ icon: String, _ name: String, shortcut: String?, _ command: PageCommand, enabled: Bool) -> some View {
        let on = enabled && live.controls && live.canDrive && live.activeTab != nil
        return Button { live.command(command) } label: { Image(systemName: icon) }
            .buttonStyle(IconButtonStyle())
            .opacity(on ? 1 : 0.35)
            .disabled(!on)
            .help(shortcut.map { "\(name) (\($0))" } ?? name)
            .accessibilityLabel(name)
    }
}

/// The address bar of Sammy's browser while the user drives it. It looks and behaves like a browser's: a field that
/// lights up on hover, shows the page's address (the host stands out, the rest is quiet), and edits it on a click or
/// ⌘L with everything selected. Return opens what was typed in the active tab: an address, a bare host such as
/// walmart.com, or a web search. Escape puts the page's address back.
struct AddressField: View {
    let live: LiveSession
    /// Bumped to start editing: ⌘L, or a new tab.
    let focus: Int
    /// Called once an address is opened or editing is cancelled, so the page gets the keyboard back.
    let done: () -> Void
    @State private var draft = ""
    @State private var hovering = false
    @FocusState private var editing: Bool

    private var url: String { live.activeTab?.url ?? "" }
    /// What editing starts from: the address, or nothing in a blank tab.
    private var editable: String { LiveTab.isBlank(url) ? "" : url }

    var body: some View {
        ZStack {
            if editing {
                TextField("Search or enter address", text: $draft)
                    .textFieldStyle(.plain)
                    .font(.mono(12))
                    .focused($editing)
                    .onSubmit {
                        live.go(to: draft)
                        editing = false
                        done()
                    }
                    .onExitCommand {
                        editing = false
                        done()
                    }
                    .padding(.horizontal, 10)
            } else {
                Button { start() } label: { AddressPill(url: url, blank: LiveTab.isBlank(url)) }
                    .buttonStyle(.plain)
                    .accessibilityHint("Type an address or a search")
            }
        }
        .frame(minWidth: 180, idealWidth: 420, maxWidth: 560)
        .frame(height: Metrics.controlSmall)
        .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(editing ? Palette.container : hovering ? Palette.containerHigh : Palette.containerLow))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radius)
                .strokeBorder(editing ? Palette.link : hovering ? Palette.outlineHover : Palette.outline, lineWidth: editing ? 2 : 1)
        )
        .animation(.easeOut(duration: 0.12), value: hovering)
        .onHover { inside in
            guard inside != hovering else { return }
            hovering = inside
            if inside { NSCursor.iBeam.push() } else { NSCursor.pop() }
        }
        .onDisappear { if hovering { NSCursor.pop() } }
        .onChange(of: focus) { start() }
        .onChange(of: editing) { _, now in if !now { draft = editable } }
        // A new tab arrives just after ⌘T: show it empty, unless the user already typed.
        .onChange(of: url) { old, _ in if editing, draft == (LiveTab.isBlank(old) ? "" : old) { draft = editable } }
        .disabled(!live.canDrive)
    }

    private func start() {
        draft = editable
        editing = true
        DispatchQueue.main.async { NSApp.sendAction(#selector(NSResponder.selectAll(_:)), to: nil, from: nil) }
    }
}

/// The page's address, as a browser shows it: the host stands out, the rest is quiet. A blank tab shows the
/// placeholder, so the field reads as one to type into.
struct AddressPill: View {
    let url: String
    let blank: Bool

    var body: some View {
        let parts = URLComponents(string: url)
        let secure = parts?.scheme == "https"
        HStack(spacing: 5) {
            if blank {
                Image(systemName: "magnifyingglass").font(.system(size: 11)).accessibilityHidden(true)
                Text("Search or enter address").font(.system(size: 12))
            } else {
                Image(systemName: secure ? "lock.fill" : "exclamationmark.triangle").font(.system(size: 10)).accessibilityHidden(true)
                if !secure { Text("Not secure").font(.system(size: 11, weight: .medium)) }
                Text(parts?.host ?? url).font(.mono(12, weight: .medium)).foregroundStyle(Palette.onSurface)
                if let path = parts?.path, path.count > 1 {
                    Text(path).font(.mono(12)).lineLimit(1).truncationMode(.middle)
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.onSurfaceVariant)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(blank ? "Address: empty" : "Address: \(url)\(secure ? "" : ", not secure")")
        .help(blank ? "Search or enter address (⌘L)" : "\(url) (⌘L to edit)")
    }
}

/// The browser's tabs, as a browser shows them: the run's tab and every tab or popup a page or the user opened.
/// Clicking one shows it; its × (on hover) closes it; + opens a blank tab. The run's own tab has no ×.
struct TabStrip: View {
    let live: LiveSession
    let newTab: () -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 4) {
                ForEach(live.tabs) { tab in TabButton(live: live, tab: tab) }
                if live.controls {
                    Button(action: newTab) { Image(systemName: "plus") }
                        .buttonStyle(IconButtonStyle(size: 24))
                        .disabled(!live.canDrive)
                        .help("New tab (⌘T)")
                        .accessibilityLabel("New tab")
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
        }
        .background(Palette.containerLow)
    }
}

private struct TabButton: View {
    let live: LiveSession
    let tab: LiveTab
    @State private var hovering = false

    private var closable: Bool { tab.closable && live.controls }

    var body: some View {
        HStack(spacing: 6) {
            if tab.loading { ProgressView().controlSize(.mini).accessibilityHidden(true) }
            Text(tab.name)
                .font(.system(size: 12, weight: tab.active ? .semibold : .regular))
                .foregroundStyle(tab.active ? Palette.onSurface : Palette.onSurfaceVariant)
                .lineLimit(1)
                .truncationMode(.tail)
            if closable {
                Spacer(minLength: 0)
                Button { live.close(tab) } label: { Image(systemName: "xmark").font(.system(size: 9, weight: .bold)) }
                    .buttonStyle(IconButtonStyle(size: 16))
                    .opacity(hovering ? 1 : 0)
                    .disabled(!live.canDrive)
                    .help("Close tab (⌘W)")
            }
        }
        .padding(.leading, 10)
        .padding(.trailing, closable ? 4 : 10)
        .frame(minWidth: 80, maxWidth: 200, minHeight: 24)
        .background(
            RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                .fill(tab.active ? Palette.container : hovering ? Palette.containerHigh : Color.clear)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                .strokeBorder(tab.active ? Palette.outline : Color.clear)
        )
        .contentShape(Rectangle())
        .onTapGesture { if !tab.active, live.canDrive { live.switchTab(tab) } }
        .onHover { hovering = $0 }
        .help(tab.title.isEmpty || tab.title == tab.url ? tab.url : "\(tab.title)\n\(tab.url)")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Tab: \(tab.name)\(tab.loading ? ", loading" : "")")
        .accessibilityAddTraits(tab.active ? [.isButton, .isSelected] : .isButton)
        .accessibilityAction { if !tab.active, live.canDrive { live.switchTab(tab) } }
        .accessibilityAction(named: "Close tab") { if closable { live.close(tab) } }
    }
}

/// The browser's shortcuts (`BrowserShortcut`), caught before the page, the address bar or the menus see them, so
/// ⌘W closes a tab and not the window. `perform` returns false to leave a key alone.
struct BrowserShortcuts: NSViewRepresentable {
    let perform: (BrowserShortcut) -> Bool

    func makeNSView(context: Context) -> MonitorView {
        let view = MonitorView()
        view.perform = perform
        return view
    }

    func updateNSView(_ view: MonitorView, context: Context) { view.perform = perform }

    static func dismantleNSView(_ view: MonitorView, coordinator: ()) { view.stop() }

    final class MonitorView: NSView {
        var perform: ((BrowserShortcut) -> Bool)?
        private var monitor: Any?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            stop()
            guard window != nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self, event.window === self.window, let shortcut = BrowserShortcut.shortcut(for: MacKey(event)),
                      self.perform?(shortcut) == true
                else { return event }
                return nil
            }
        }

        func stop() {
            if let monitor { NSEvent.removeMonitor(monitor) }
            monitor = nil
        }
    }
}

extension MacKey {
    /// The key press `event` reports.
    init(_ event: NSEvent) {
        let flags = event.modifierFlags
        self.init(
            keyCode: event.keyCode,
            characters: event.characters ?? "",
            bare: event.charactersIgnoringModifiers ?? "",
            command: flags.contains(.command),
            option: flags.contains(.option),
            control: flags.contains(.control),
            shift: flags.contains(.shift)
        )
    }
}
