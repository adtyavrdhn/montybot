import MontyKit
import SwiftUI

/// The address bar of Monty's browser while the user drives it. It shows the page's address as a browser does (the
/// host stands out, the rest is quiet); clicking it, or ⌘L, edits it, and Return opens what was typed in the active
/// tab: an address, a bare host such as walmart.com, or a web search. Escape puts the page's address back.
struct AddressField: View {
    let live: LiveSession
    /// Called once an address is opened or editing is cancelled, so the page gets the keyboard back.
    let done: () -> Void
    @State private var draft = ""
    @FocusState private var editing: Bool

    private var url: String { live.activeTab?.url ?? "" }

    var body: some View {
        ZStack {
            if editing {
                TextField("Search or type an address", text: $draft)
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
                Button { start() } label: { AddressPill(url: url) }
                    .buttonStyle(.plain)
                    .accessibilityHint("Type an address to open")
            }
        }
        .frame(minWidth: 160, idealWidth: 340, maxWidth: 420)
        .frame(height: Metrics.controlSmall)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.containerLow))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                .strokeBorder(editing ? Palette.link : Palette.outlineVariant, lineWidth: editing ? 1.5 : 1)
        )
        .background {
            Button("Edit address") { start() }.keyboardShortcut("l", modifiers: .command).hidden()
        }
        .onChange(of: editing) { _, now in if !now { draft = url } }
        .disabled(live.state != .driving || live.givingBack)
    }

    private func start() {
        draft = url
        editing = true
    }
}

/// The page's address, as a browser shows it: the host stands out, the rest is quiet.
struct AddressPill: View {
    let url: String
    var body: some View {
        let parts = URLComponents(string: url)
        let secure = parts?.scheme == "https"
        HStack(spacing: 5) {
            Image(systemName: secure ? "lock.fill" : "exclamationmark.triangle").font(.system(size: 10)).accessibilityHidden(true)
            if !secure { Text("Not secure").font(.system(size: 11, weight: .medium)) }
            Text(parts?.host ?? url).font(.mono(12, weight: .medium)).foregroundStyle(Palette.onSurface)
            if let path = parts?.path, path.count > 1 {
                Text(path).font(.mono(12)).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.onSurfaceVariant)
        .padding(.horizontal, 10)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Address: \(url)\(secure ? "" : ", not secure")")
        .help(url)
    }
}

/// The browser's tabs, as a browser shows them: the run's tab and every tab or popup a page opened. Clicking one
/// shows it and sends the input there.
struct TabStrip: View {
    let live: LiveSession

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 4) {
                ForEach(live.tabs) { tab in
                    Button { live.switchTab(tab) } label: { label(tab) }
                        .buttonStyle(.plain)
                        .disabled(tab.active || live.state != .driving || live.givingBack)
                        .help(tab.url)
                        .accessibilityLabel("Tab: \(name(tab))")
                        .accessibilityAddTraits(tab.active ? .isSelected : [])
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
        }
        .background(Palette.containerLow)
    }

    private func label(_ tab: LiveTab) -> some View {
        Text(name(tab))
            .font(.system(size: 12, weight: tab.active ? .semibold : .regular))
            .foregroundStyle(tab.active ? Palette.onSurface : Palette.onSurfaceVariant)
            .lineLimit(1)
            .truncationMode(.tail)
            .padding(.horizontal, 10)
            .frame(minWidth: 80, maxWidth: 200, minHeight: 24)
            .background(
                RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                    .fill(tab.active ? Palette.container : Color.clear)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                    .strokeBorder(tab.active ? Palette.outline : Color.clear)
            )
            .contentShape(Rectangle())
    }

    private func name(_ tab: LiveTab) -> String {
        if !tab.title.isEmpty { return tab.title }
        return URLComponents(string: tab.url)?.host ?? tab.url
    }
}
