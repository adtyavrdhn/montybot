import AppKit
import MontyKit
import SwiftUI
import UniformTypeIdentifiers

/// A library page: a title, a sentence on what it is, then its items in one card, or a calm empty state.
struct Page<Items: RandomAccessCollection, Row: View>: View where Items.Element: Identifiable {
    @Environment(AppModel.self) private var app
    let title: String
    let subtitle: String
    let items: Items?
    let emptyIcon: String
    let emptyTitle: String
    let emptyText: String
    @ViewBuilder let row: (Items.Element) -> Row

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                Text(title).font(.system(size: 20, weight: .semibold)).accessibilityAddTraits(.isHeader)
                Text(subtitle).font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant).padding(.top, 4)
                if let error = app.libraryError, items != nil {  // a list on screen, and an action on it failed
                    NoticeBar(notice: .error(error)) { app.libraryError = nil }.padding(.top, 16)
                }
                Group {
                    if let items {
                        if items.isEmpty {
                            EmptyState(icon: emptyIcon, title: emptyTitle, text: emptyText).card(padding: 0)
                        } else {
                            VStack(spacing: 0) {
                                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                    if index > 0 { Divider().overlay(Palette.outlineVariant) }
                                    row(item).padding(.horizontal, 14).padding(.vertical, 10)
                                }
                            }
                            .card(padding: 0)
                        }
                    } else if let error = app.libraryError {
                        EmptyState(icon: "exclamationmark.triangle", title: "Couldn't load \(title.lowercased())", text: error) {
                            Button("Try again") { app.libraryError = nil; app.reloadPage() }.buttonStyle(.outline)
                        }
                        .card(padding: 0)
                    } else {
                        MontyMark(mood: .working, size: 22).frame(maxWidth: .infinity).padding(.vertical, 48)
                    }
                }
                .padding(.top, 20)
            }
            .frame(maxWidth: Metrics.readingWidth, alignment: .leading)
            .padding(.horizontal, Metrics.gutter)
            .padding(.vertical, 28)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle(title)
        .navigationSubtitle("")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { app.libraryError = nil; app.reloadPage() } label: { Label("Reload", systemImage: "arrow.clockwise") }
                    .help("Read this page again (⌘R)")
            }
        }
    }
}

struct SchedulesView: View {
    @Environment(AppModel.self) private var app
    @State private var deleting: Schedule?

    var body: some View {
        Page(
            title: "Schedules",
            subtitle: "Tasks \(app.montyName) runs on its own. Ask for one in a chat: \"Every Monday at 9, …\"",
            items: app.schedules,
            emptyIcon: "calendar.badge.clock",
            emptyTitle: "No schedules yet",
            emptyText: "Tell \(app.montyName) what to do and when, and it will run the task for you and say how it went."
        ) { schedule in
            HStack(spacing: 12) {
                Image(systemName: schedule.watch ? "eye" : "clock")
                    .accessibilityHidden(true)
                    .font(.system(size: 13))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .frame(width: 18)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(schedule.name).font(.system(size: 13, weight: .medium))
                        if schedule.paused { Badge(text: "Paused") }
                    }
                    HStack(spacing: 6) {
                        Text(schedule.watch ? "Checks \(schedule.plainWhen) and tells you when it finds something" : schedule.plainWhen.prefix(1).uppercased() + schedule.plainWhen.dropFirst())
                        if let zone = schedule.timeZone, zone != TimeZone.current.identifier {
                            Text("(\(zone.replacingOccurrences(of: "_", with: " ")) time)")
                        }
                    }
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    if let times = schedule.times() {
                        Text(times.text)
                            .font(.system(size: 11))
                            .foregroundStyle(times.failed ? Palette.onWarningContainer : Palette.onSurfaceVariant)
                    }
                }
                Spacer()
                Menu {
                    Button("Open Chat") { app.open(.chat(schedule.threadId)) }
                    Button(schedule.paused ? "Resume" : "Pause") { Task { await app.setPaused(schedule, !schedule.paused) } }
                    Divider()
                    Button("Delete…", role: .destructive) { deleting = schedule }
                } label: {
                    Image(systemName: "ellipsis").frame(width: Metrics.controlSmall, height: Metrics.controlSmall)
                }
                .menuStyle(.button)
                .buttonStyle(IconButtonStyle())
                .menuIndicator(.hidden)
                .fixedSize()
                .accessibilityLabel("Actions for \(schedule.name)")
            }
            .contentShape(Rectangle())
            .onTapGesture { app.open(.chat(schedule.threadId)) }
            .accessibilityAction(named: "Open chat") { app.open(.chat(schedule.threadId)) }
        }
        .confirmationDialog("Delete “\(deleting?.name ?? "")”?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } })) {
            Button("Delete schedule", role: .destructive) { if let deleting { Task { await app.delete(deleting) } } }
        } message: {
            Text("\(app.montyName) stops running it. Its chat stays.")
        }
    }
}

struct FilesView: View {
    @Environment(AppModel.self) private var app
    @State private var saving: String?

    var body: some View {
        Page(
            title: "Files",
            subtitle: "What \(app.montyName) downloaded or made for you. Save a file to keep it on your Mac.",
            items: app.files?.files,
            emptyIcon: "doc.on.doc",
            emptyTitle: "No files yet",
            emptyText: "Ask \(app.montyName) to download something, like your invoices, and it will be here."
        ) { file in
            let tooLarge = file.size > (app.files?.maxDownloadBytes ?? .max)
            HStack(spacing: 12) {
                Image(nsImage: NSWorkspace.shared.icon(for: UTType(filenameExtension: (file.name as NSString).pathExtension) ?? .data))
                    .resizable()
                    .frame(width: 22, height: 22)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(file.name).font(.system(size: 13, weight: .medium)).lineLimit(1).truncationMode(.middle)
                    HStack(spacing: 6) {
                        if !file.shownFolder.isEmpty { Text(file.shownFolder).font(.mono(11)) }
                        Text(ByteCountFormatter.string(fromByteCount: Int64(file.size), countStyle: .file))
                        if tooLarge {
                            Text("· Too large to download (over \(ByteCountFormatter.string(fromByteCount: Int64(app.files?.maxDownloadBytes ?? 0), countStyle: .file)))")
                        }
                    }
                    .font(.system(size: 11))
                    .foregroundStyle(Palette.onSurfaceVariant)
                }
                Spacer()
                if saving == file.path { ProgressView().controlSize(.small) }
                Button("Save…") { save(file) }
                    .buttonStyle(.monty(.outline, small: true))
                    .disabled(tooLarge || saving != nil)
                    .accessibilityLabel("Save \(file.name)")
            }
        }
    }

    private func save(_ file: WorkspaceFile) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = file.name
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        guard panel.runModal() == .OK, let url = panel.url else { return }
        saving = file.path
        Task {
            defer { saving = nil }
            guard let downloaded = await app.download(file) else { return }
            do {
                try downloaded.data.write(to: url)
                NSWorkspace.shared.activateFileViewerSelecting([url])
            } catch {
                app.libraryError = "Couldn't save \(file.name): \(error.localizedDescription)"
            }
        }
    }
}

struct SavedSitesView: View {
    @Environment(AppModel.self) private var app
    @State private var forgetting: SavedSite?

    var body: some View {
        Page(
            title: "Saved sign-ins",
            subtitle: "\(app.montyName)'s browser stays signed in to these sites because you signed in for it, so \(app.montyName) can use them as you. It never sees your passwords. Forget a site to sign \(app.montyName) out.",
            items: app.savedSites,
            emptyIcon: "key",
            emptyTitle: "No saved sign-ins",
            emptyText: "When \(app.montyName) asks you to sign in to a site, it stays signed in there for next time."
        ) { site in
            HStack(spacing: 12) {
                Image(systemName: "globe").foregroundStyle(Palette.onSurfaceVariant).frame(width: 18).accessibilityHidden(true)
                Text(site.site).font(.mono(12, weight: .medium))
                Spacer()
                Button("Forget…") { forgetting = site }
                    .buttonStyle(.monty(.outline, small: true))
                    .accessibilityLabel("Forget \(site.site)")
            }
        }
        .confirmationDialog("Forget \(forgetting?.site ?? "")?", isPresented: Binding(get: { forgetting != nil }, set: { if !$0 { forgetting = nil } })) {
            Button("Forget", role: .destructive) { if let forgetting { Task { await app.forget(forgetting) } } }
        } message: {
            Text("\(app.montyName)'s browser signs out of this site. You can sign in again when \(app.montyName) next asks.")
        }
    }
}

struct MemoryView: View {
    @Environment(AppModel.self) private var app
    @State private var forgetting: Memory?

    var body: some View {
        Page(
            title: "Memory",
            subtitle: "What \(app.montyName) remembers about you, to do better next time.",
            items: app.memories,
            emptyIcon: "brain",
            emptyTitle: "Nothing remembered yet",
            emptyText: "Tell \(app.montyName) something worth remembering, like your usual shop or your size."
        ) { memory in
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(memory.text).font(.system(size: 13)).textSelection(.enabled)
                Spacer()
                Button("Forget…") { forgetting = memory }
                    .buttonStyle(.monty(.outline, small: true))
                    .accessibilityLabel("Forget “\(memory.text)”")
            }
        }
        .confirmationDialog("Forget this?", isPresented: Binding(get: { forgetting != nil }, set: { if !$0 { forgetting = nil } })) {
            Button("Forget", role: .destructive) { if let forgetting { Task { await app.forget(forgetting) } } }
        } message: {
            Text("“\(forgetting?.text ?? "")”. \(app.montyName) won't remember it in future tasks.")
        }
    }
}
