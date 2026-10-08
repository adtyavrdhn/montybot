import AppKit
import ImageIO
import SammyKit
import SwiftUI
import UniformTypeIdentifiers

// Files in the chat: the chips above the message box while they upload, the files on messages (the user's, and those
// Sammy shares), and the ways files come in (the paperclip, dropping them on the chat, pasting them).

/// A file's size as Finder says it: "12 KB".
func fileSize(_ bytes: Int) -> String { ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file) }

/// The symbol for a file of this media type.
func fileIcon(_ mediaType: String) -> String {
    guard let type = UTType(mimeType: mediaType) else { return "doc" }
    if type.conforms(to: .image) { return "photo" }
    if type.conforms(to: .pdf) { return "doc.richtext" }
    if type.conforms(to: .spreadsheet) || type.conforms(to: .commaSeparatedText) { return "tablecells" }
    if type.conforms(to: .archive) { return "doc.zipper" }
    if type.conforms(to: .text) { return "doc.plaintext" }
    return "doc"
}

/// A small picture made from image bytes, without decoding the whole image at full size; nil if it isn't one.
@MainActor
func thumbnail(_ data: Data, maxPixels: Int) -> NSImage? {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    let options = [
        kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPixels,
    ] as CFDictionary
    guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options) else { return nil }
    return NSImage(cgImage: image, size: NSSize(width: image.width, height: image.height))
}

// MARK: - opening and saving

/// Opening a file from a chat, or saving it. Only pictures and PDFs open (in Preview, or whatever opens them): a file
/// Sammy made could be anything, and opening a script or an app would run it. Everything else is saved where the user
/// says, and shown in Finder.
@MainActor
enum ChatFiles {
    static func canOpen(_ file: Attachment) -> Bool { file.isImage || file.isPDF }

    static func open(_ file: Attachment, app: AppModel) {
        guard canOpen(file) else { return save(file, app: app) }
        Task {
            guard let data = await app.download(file) else { return }
            do {
                let folder = FileManager.default.temporaryDirectory.appending(path: "Sammy/\(file.id)", directoryHint: .isDirectory)
                try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
                let url = folder.appending(path: safeName(file.name))
                try data.write(to: url)
                NSWorkspace.shared.open(url)
            } catch {
                app.actionError = "Couldn't open \(file.name). \(error.localizedDescription)"
            }
        }
    }

    static func save(_ file: Attachment, app: AppModel) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = safeName(file.name)
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task {
            guard let data = await app.download(file) else { return }
            do {
                try data.write(to: url)
                NSWorkspace.shared.activateFileViewerSelecting([url])
            } catch {
                app.actionError = "Couldn't save \(file.name). \(error.localizedDescription)"
            }
        }
    }

    /// The name as a file on this Mac can have it: no folders in it.
    static func safeName(_ name: String) -> String {
        let last = name.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
        return last.trimmingCharacters(in: .whitespaces).isEmpty ? "file" : last
    }
}

// MARK: - the chips above the message box

/// The files the user is adding to their next message, in a row that scrolls when there are many.
struct AttachmentTray: View {
    @Bindable var chat: ChatModel

    var body: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 6) {
                ForEach(chat.attachments) { file in
                    AttachmentChip(file: file) { withAnimation(.easeOut(duration: 0.15)) { chat.removeAttachment(file.id) } }
                        .transition(.scale(scale: 0.9).combined(with: .opacity))
                }
            }
            .padding(.vertical, 2)
        }
        .scrollIndicators(.never)
        .animation(.easeOut(duration: 0.15), value: chat.attachments.map(\.id))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Attached files")
    }
}

/// A file being added to the message: its picture or icon, its name, and how its upload is going; × takes it out.
struct AttachmentChip: View {
    let file: PendingAttachment
    let remove: () -> Void
    @State private var picture: NSImage?

    var body: some View {
        HStack(spacing: 8) {
            ZStack {
                RoundedRectangle(cornerRadius: 4).fill(Palette.container)
                if let picture {
                    Image(nsImage: picture).resizable().aspectRatio(contentMode: .fill)
                } else {
                    Image(systemName: fileIcon(file.mediaType)).font(.system(size: 14)).foregroundStyle(Palette.onSurfaceVariant)
                }
                if file.isUploading {
                    Color.black.opacity(picture == nil ? 0 : 0.3)
                    ProgressView().controlSize(.small)
                }
            }
            .frame(width: 34, height: 34)
            .clipShape(RoundedRectangle(cornerRadius: 4))
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(file.name).font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                Text(status)
                    .font(.system(size: 11))
                    .foregroundStyle(file.error == nil ? Palette.onSurfaceVariant : Palette.onErrorContainer)
                    .lineLimit(2)
            }
            .frame(maxWidth: 180, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
            Button(action: remove) { Image(systemName: "xmark").font(.system(size: 9, weight: .semibold)) }
                .buttonStyle(IconButtonStyle(size: 20))
                .help("Remove")
                .accessibilityLabel("Remove \(file.name)")
        }
        .padding(.leading, 4)
        .padding(.trailing, 2)
        .padding(.vertical, 4)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(file.error == nil ? Palette.containerHigh : Palette.errorContainer))
        .help(file.error ?? "\(file.name), \(fileSize(file.size))")
        .task(id: file.id) {
            if let image = file.image { picture = thumbnail(image, maxPixels: 96) }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(file.name), \(status)")
    }

    private var status: String {
        switch file.state {
        case .uploading: "Uploading…"
        case .uploaded: fileSize(file.size)
        case .failed(let why): why
        }
    }
}

// MARK: - files on messages

/// The files on a message: pictures as pictures, other files as chips, as many to a row as fit.
struct MessageFiles: View {
    let files: [Attachment]
    var trailing = false

    var body: some View {
        Flow(spacing: 6, trailing: trailing) {
            ForEach(files) { file in
                if file.isImage { MessagePicture(file: file) } else { FileChip(file: file) }
            }
        }
    }
}

/// A picture on a message, fetched once (and kept in memory); clicking it opens it in Preview.
struct MessagePicture: View {
    @Environment(AppModel.self) private var app
    let file: Attachment
    @State private var picture: NSImage?
    @State private var failed = false

    var body: some View {
        if failed {
            FileChip(file: file)  // it wouldn't load as a picture: still there to open or save
        } else {
            Button { ChatFiles.open(file, app: app) } label: {
                Group {
                    if let picture {
                        Image(nsImage: picture).resizable().interpolation(.high)
                    } else {
                        ZStack {
                            Palette.containerHigh
                            ProgressView().controlSize(.small)
                        }
                    }
                }
                .frame(width: size.width, height: size.height)
                .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
                .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.outline))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("\(file.name). Click to open it in Preview.")
            .contextMenu { FileMenu(file: file) }
            .accessibilityLabel("Picture: \(file.name)")
            .accessibilityHint("Opens it in Preview")
            .accessibilityAction(named: "Save") { ChatFiles.save(file, app: app) }
            .task(id: file.id) {
                guard picture == nil, let data = await app.picture(file) else { failed = picture == nil; return }
                picture = thumbnail(data, maxPixels: 480)
                failed = picture == nil
            }
        }
    }

    /// At most 220 by 160, in the picture's proportions.
    private var size: CGSize {
        let limit = CGSize(width: 220, height: 160)
        guard let picture, picture.size.width > 0, picture.size.height > 0 else { return CGSize(width: 160, height: 120) }
        let scale = min(limit.width / picture.size.width, limit.height / picture.size.height, 1)
        return CGSize(width: max(picture.size.width * scale, 40), height: max(picture.size.height * scale, 40))
    }
}

/// A file on a message that isn't a picture: its icon, name and size. Clicking opens a PDF, and saves anything else.
struct FileChip: View {
    @Environment(AppModel.self) private var app
    let file: Attachment
    @State private var hovering = false

    var body: some View {
        let opens = ChatFiles.canOpen(file)
        Button { ChatFiles.open(file, app: app) } label: {
            HStack(spacing: 8) {
                Image(systemName: fileIcon(file.mediaType))
                    .font(.system(size: 15))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .frame(width: 22)
                VStack(alignment: .leading, spacing: 1) {
                    Text(file.name).font(.system(size: 12, weight: .medium)).foregroundStyle(Palette.onSurface)
                        .lineLimit(1).truncationMode(.middle)
                    Text(fileSize(file.size)).font(.system(size: 11)).foregroundStyle(Palette.onSurfaceVariant)
                }
                .frame(maxWidth: 220, alignment: .leading)
                Image(systemName: opens ? "arrow.up.forward.square" : "arrow.down.circle")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .opacity(hovering ? 1 : 0.6)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(hovering ? Palette.containerHighest : Palette.containerHigh))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(opens ? "Open \(file.name)" : "Save \(file.name) to your Mac")
        .contextMenu { FileMenu(file: file) }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("File: \(file.name), \(fileSize(file.size))")
        .accessibilityHint(opens ? "Opens it" : "Saves it to your Mac")
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { ChatFiles.open(file, app: app) }
        .accessibilityAction(named: "Save") { ChatFiles.save(file, app: app) }
    }
}

/// What a file on a message offers on right-click.
struct FileMenu: View {
    @Environment(AppModel.self) private var app
    let file: Attachment

    var body: some View {
        if ChatFiles.canOpen(file) { Button("Open") { ChatFiles.open(file, app: app) } }
        Button("Save…") { ChatFiles.save(file, app: app) }
    }
}

/// Views in rows, as many to a row as fit, as words wrap; rows to the right edge when `trailing`.
struct Flow: Layout {
    var spacing: CGFloat = 6
    var trailing = false

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = rows(in: proposal.width ?? .infinity, subviews)
        let height = rows.map(\.height).reduce(0, +) + spacing * CGFloat(max(rows.count - 1, 0))
        return CGSize(width: rows.map(\.width).max() ?? 0, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in rows(in: bounds.width, subviews) {
            var x = trailing ? bounds.maxX - row.width : bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + spacing
        }
    }

    private func rows(in width: CGFloat, _ subviews: Subviews) -> [(indices: [Int], width: CGFloat, height: CGFloat)] {
        var rows: [(indices: [Int], width: CGFloat, height: CGFloat)] = []
        for (index, subview) in subviews.enumerated() {
            let size = subview.sizeThatFits(.unspecified)
            if let last = rows.last, !last.indices.isEmpty, last.width + spacing + size.width <= width {
                rows[rows.count - 1].indices.append(index)
                rows[rows.count - 1].width += spacing + size.width
                rows[rows.count - 1].height = max(last.height, size.height)
            } else {
                rows.append(([index], size.width, size.height))
            }
        }
        return rows
    }
}

// MARK: - how files come in

/// Files dropped anywhere on the chat go with the next message, with the chat marked as the place to drop them
/// while they are dragged over it. Pictures dragged from a web page (not files) come as PNGs.
struct DropFiles: ViewModifier {
    let chat: ChatModel
    @State private var targeted = false

    func body(content: Content) -> some View {
        content
            .onDrop(of: [.fileURL, .image], isTargeted: $targeted) { providers in
                Task { await attach(providers) }
                return true
            }
            .overlay {
                if targeted { DropOverlay().transition(.opacity) }
            }
            .animation(.easeOut(duration: 0.12), value: targeted)
    }

    private func attach(_ providers: [NSItemProvider]) async {
        var urls: [URL] = []
        var pictures: [Data] = []
        for provider in providers {
            if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
                if let url = await provider.fileURL() { urls.append(url) }
            } else if let data = await provider.data(.image), let png = pngData(data) {
                pictures.append(png)
            }
        }
        if !urls.isEmpty { chat.attach(contentsOf: urls) }
        for png in pictures { chat.attach(png, name: "Dropped image.png", mediaType: "image/png") }
    }
}

private struct DropOverlay: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 12)
            .fill(Palette.link.opacity(0.06))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Palette.link, style: StrokeStyle(lineWidth: 2, dash: [7, 5])))
            .overlay {
                VStack(spacing: 6) {
                    Image(systemName: "paperclip").font(.system(size: 22, weight: .medium))
                    Text("Drop files to attach").font(.system(size: 14, weight: .semibold))
                    Text("Up to \(ChatModel.maxAttachments) files, \(fileSize(ChatModel.maxAttachmentBytes)) each")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                }
                .foregroundStyle(Palette.actionText)
                .padding(.horizontal, 20)
                .padding(.vertical, 14)
                .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.container))
                .shadow(color: .black.opacity(0.08), radius: 8, y: 2)
            }
            .padding(10)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

@MainActor
extension NSItemProvider {
    func fileURL() async -> URL? {
        await withCheckedContinuation { done in _ = loadObject(ofClass: URL.self) { @Sendable url, _ in done.resume(returning: url) } }
    }

    func data(_ type: UTType) async -> Data? {
        await withCheckedContinuation { done in _ = loadDataRepresentation(for: type) { @Sendable data, _ in done.resume(returning: data) } }
    }
}

/// Image bytes of any kind macOS reads, as PNG.
func pngData(_ data: Data) -> Data? {
    if data.starts(with: [0x89, 0x50, 0x4E, 0x47]) { return data }  // PNG already
    guard let image = NSImage(data: data) else { return nil }
    return png(image)
}

func png(_ image: NSImage) -> Data? {
    guard let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) else { return nil }
    return bitmap.representation(using: .png, properties: [:])
}

/// ⌘V with files (copied in Finder) or a picture (a screenshot, say) on the clipboard attaches them while the
/// message box has the keyboard; anything with text pastes as text, as always. A key-down monitor, as the text
/// field's own paste takes only text, and only while `active`.
struct PasteFiles: ViewModifier {
    let chat: ChatModel
    let active: Bool
    @State private var monitor: Any?

    func body(content: Content) -> some View {
        content
            .onChange(of: active, initial: true) { _, active in active ? install() : remove() }
            .onDisappear(perform: remove)
    }

    private func install() {
        guard monitor == nil else { return }
        let chat = chat
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers == "v" else { return event }
            return MainActor.assumeIsolated { Self.paste(into: chat) } ? nil : event
        }
    }

    private func remove() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    /// True if the clipboard had files or a picture for the message, which are attached; false to paste as usual.
    @MainActor
    static func paste(into chat: ChatModel) -> Bool {
        let board = NSPasteboard.general
        let urls = board.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        if !urls.isEmpty {
            chat.attach(contentsOf: urls)
            return true
        }
        // Rich text (from Pages, Word) comes with a picture of itself: it is text, and pastes as text.
        guard board.string(forType: .string) == nil else { return false }
        let picture = board.data(forType: .png) ?? NSImage(pasteboard: board).flatMap(png)
        guard let picture else { return false }
        chat.attach(picture, name: "Pasted image.png", mediaType: "image/png")
        return true
    }
}
