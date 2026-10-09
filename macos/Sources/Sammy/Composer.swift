import SammyKit
import SwiftUI

// MARK: - the composer

struct Composer: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    var prominent = false
    @FocusState private var focused: Bool
    @State private var picking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ComposerModelPicker()
            if let notice = chat.notice {
                NoticeBar(notice: notice) { chat.notice = nil }
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            if let queued = chat.queued {
                QueuedMessage(text: queued, files: chat.queuedAttachments.count, edit: chat.unqueue, remove: chat.dropQueued)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            VStack(alignment: .leading, spacing: 6) {
                if !chat.attachments.isEmpty {
                    AttachmentTray(chat: chat).padding(.leading, 4)
                }
                HStack(alignment: .bottom, spacing: 4) {
                    Button { picking = true } label: { Image(systemName: "paperclip").font(.system(size: 14, weight: .medium)) }
                        .buttonStyle(IconButtonStyle(size: Metrics.control))
                        .help("Attach files (or drop them on the chat, or paste them)")
                        .accessibilityLabel("Attach files")
                    TextField("Message \(app.sammyName)", text: $chat.draft, prompt: Text(placeholder).foregroundStyle(Palette.onSurfaceVariant), axis: .vertical)
                        .labelsHidden()
                        .accessibilityLabel("Message \(app.sammyName)")
                        .accessibilityHint(placeholder)
                        .textFieldStyle(.plain)
                        .font(.system(size: prominent ? 15 : 14))
                        .lineLimit(prominent ? 3...10 : 1...10)
                        .focused($focused)
                        .padding(.vertical, 6)
                        .onSubmit {
                            if chat.canSend { Task { await chat.send() } } else if chat.canQueue { chat.queue() }
                        }
                        // ↑ in an empty box brings back the last task, to change and send again (as T3 Code's history).
                        .onKeyPress(.upArrow) {
                            guard chat.draft.isEmpty, let last = chat.lastTask else { return .ignored }
                            chat.draft = last
                            return .handled
                        }
                        .disabled(chat.ask != nil && chat.draft.isEmpty)  // text kept here stays reachable
                    if chat.isActive {
                        // While Sammy works (or waits for an answer), Send is Stop, as in other chat apps.
                        Button { Task { await chat.stop() } } label: {
                            Image(systemName: "stop.fill")
                                .font(.system(size: 11, weight: .bold))
                                .frame(width: Metrics.control, height: Metrics.control)
                                .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.onSurface))
                                .foregroundStyle(Palette.surface)
                                .opacity(chat.canStop ? 1 : 0.45)
                        }
                        .buttonStyle(.plain)
                        .disabled(!chat.canStop)
                        .help("Stop this task (⌘.)")
                        .accessibilityLabel(chat.stopping ? "Stopping" : "Stop task")
                        .transition(.scale(scale: 0.8).combined(with: .opacity))
                    } else {
                        Button { Task { await chat.send() } } label: {
                            Image(systemName: "arrow.up")
                                .font(.system(size: 13, weight: .bold))
                                .frame(width: Metrics.control, height: Metrics.control)
                                .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(chat.canSend ? Palette.action : Palette.containerHigh))
                                .foregroundStyle(chat.canSend ? Palette.onLink : Palette.onSurfaceVariant)
                        }
                        .buttonStyle(.plain)
                        .disabled(!chat.canSend)
                        .help(chat.isUploading ? "Sends once your files have uploaded" : "Send (↩). ⌥↩ starts a new line.")
                        .accessibilityLabel(chat.isUploading ? "Send, waiting for files to upload" : "Send")
                        .animation(.easeOut(duration: 0.15), value: chat.canSend)
                        .transition(.scale(scale: 0.8).combined(with: .opacity))
                    }
                }
            }
            .padding(.leading, 6)
            .padding(.trailing, 6)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(chat.ask != nil ? Palette.containerLowest : Palette.container))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(focused ? Palette.link : Palette.outline, lineWidth: focused ? 1.5 : 1))
            .motion(.easeOut(duration: 0.18), value: focused)
            .shadow(color: .black.opacity(0.04), radius: 6, y: 2)
            .onTapGesture { focused = true }
        }
        .animation(.easeOut(duration: 0.2), value: chat.notice)
        .animation(.easeOut(duration: 0.2), value: chat.queued)
        .animation(.easeOut(duration: 0.15), value: chat.isActive)
        .animation(.easeOut(duration: 0.15), value: chat.attachments.isEmpty)
        .onAppear { if chat.ask == nil { focused = true } }
        .onReceive(NotificationCenter.default.publisher(for: .sammyFocusMessage)) { _ in focused = true }
        .onChange(of: chat.ask == nil) { _, free in if free { focused = true } }
        .fileImporter(isPresented: $picking, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            if case .success(let urls) = result { chat.attach(contentsOf: urls) }
        }
        .modifier(PasteFiles(chat: chat, active: focused))
    }

    private var placeholder: String {
        if let ask = chat.ask {
            switch ask.kind {
            case .question: return "Answer \(app.sammyName)'s question above"
            case .approval: return "Approve or decline above to carry on"
            case .handoff: return "\(app.sammyName) is waiting for you to take over the browser"
            case .connect: return "\(app.sammyName) is waiting for you to connect \(ask.integration?.name ?? "an app")"
            case .other: return "\(app.sammyName) is waiting for you: answer in the web app"
            }
        }
        if chat.queued != nil { return "Your next message is queued. Stop the task, or wait." }
        if chat.isActive { return "\(app.sammyName) is on it. Write your next message: ↩ queues it for when it's done." }
        if chat.threadId == nil { return "Ask \(app.sammyName) to do something on the web…" }
        return "Reply to \(app.sammyName)…"
    }
}

/// The message the user queued while Sammy works: it goes when the task is done; Edit takes it back to the box.
struct QueuedMessage: View {
    @Environment(AppModel.self) private var app
    let text: String
    var files = 0
    let edit: () -> Void
    let remove: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "clock.arrow.circlepath").accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("Sends when \(app.sammyName) is done").font(.system(size: 11, weight: .medium)).foregroundStyle(Palette.onSurfaceVariant)
                if !text.isEmpty { Text(text).lineLimit(2).foregroundStyle(Palette.onSurface) }
                if files > 0 {
                    Label("\(files) file\(files == 1 ? "" : "s")", systemImage: "paperclip").foregroundStyle(Palette.onSurfaceVariant)
                }
            }
            Spacer(minLength: 4)
            Button("Edit", action: edit).buttonStyle(.sammy(.ghost, small: true)).help("Back to the message box")
            Button(action: remove) { Image(systemName: "xmark").font(.system(size: 10, weight: .semibold)) }
                .buttonStyle(IconButtonStyle(size: 24))
                .accessibilityLabel("Don't send")
                .help("Don't send it")
        }
        .font(.system(size: 12))
        .padding(.leading, 10)
        .padding(.vertical, 6)
        .padding(.trailing, 4)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.containerHigh))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Queued message, sends when \(app.sammyName) is done: \(text)\(files > 0 ? ", with \(files) file\(files == 1 ? "" : "s")" : "")")
    }
}

/// A notice above the composer: neutral for information, red for errors. Errors stay until dismissed.
struct NoticeBar: View {
    let notice: ChatNotice
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: notice.isError ? "exclamationmark.circle" : "info.circle").accessibilityHidden(true)
            // Wraps in the width it has. Not fixedSize: measured at its narrowest, a long notice made the window's
            // content taller than the window, and pushed the message box below its edge.
            Text(notice.text).lineLimit(4).frame(maxWidth: .infinity, alignment: .leading)
            Button(action: dismiss) { Image(systemName: "xmark").font(.system(size: 10, weight: .semibold)) }
                .buttonStyle(IconButtonStyle(size: 24))
                .accessibilityLabel("Dismiss")
        }
        .font(.system(size: 12))
        .foregroundStyle(notice.isError ? Palette.onErrorContainer : Palette.onSurface)
        .padding(.leading, 10)
        .padding(.vertical, 4)
        .padding(.trailing, 4)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(notice.isError ? Palette.errorContainer : Palette.containerHigh))
        .task(id: notice) {
            guard !notice.isError else { return }
            try? await Task.sleep(for: .seconds(10))
            dismiss()
        }
        .onAppear { AccessibilityNotification.Announcement(notice.text).post() }
    }
}

// MARK: - a new task

struct NewTaskView: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel

    let suggestions: [(String, String, String)] = [
        ("airplane", "Find cheap flights", "Find the three cheapest flights to Lisbon next Friday."),
        ("shippingbox", "Track an order", "Check where my last Amazon order is."),
        ("calendar.badge.clock", "Make it a routine", "Every Tuesday at 9am, check the price of my usual shopping list."),
    ]

    var body: some View {
        VStack(spacing: 0) {
            Spacer()
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .bottom, spacing: -30) {  // the squirrel's frame is wider than the squirrel
                    SammySquirrel(mood: .idle, size: 168, layoutHeight: 120)
                    SquirrelNameTag().padding(.bottom, 14)
                }
                .padding(.leading, -42)
                .padding(.bottom, -6)
                Text("What should \(app.sammyName) do?")
                    .font(.system(size: 22, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Text("\(app.sammyName) works on the web in its own browser and asks when it needs you.")
                    .font(.system(size: 14))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .padding(.top, 4)
                    .padding(.bottom, 20)
                Composer(chat: chat, prominent: true)
                VStack(spacing: 2) {
                    ForEach(suggestions, id: \.1) { icon, title, prompt in
                        SuggestionRow(icon: icon, title: title, prompt: prompt) { chat.draft = prompt }
                    }
                }
                .padding(.top, 16)
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "lock").font(.system(size: 11)).accessibilityHidden(true)
                    Text("\(app.sammyName) asks before it buys or sends anything, and you sign in to sites yourself.")
                }
                .font(.system(size: 12))
                .foregroundStyle(Palette.onSurfaceVariant)
                .padding(.top, 20)
            }
            .frame(maxWidth: Metrics.readingWidth)
            .padding(.horizontal, Metrics.gutter)
            Spacer()
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct SuggestionRow: View {
    let icon: String
    let title: String
    let prompt: String
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: icon).font(.system(size: 13)).foregroundStyle(Palette.onSurfaceVariant).frame(width: 20).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text(title).font(.system(size: 13, weight: .medium)).foregroundStyle(Palette.onSurface)
                    Text(prompt).font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(1)
                }
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(hovering ? Palette.containerLowest : .clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("Put this in the message box to edit before sending")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title): \(prompt)")
        .accessibilityHint("Puts this in the message box")
        .accessibilityAddTraits(.isButton)
    }
}

// MARK: - watching Sammy's browser

struct BrowserPanel: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("\(app.sammyName)'s browser").sectionLabel()
                Spacer()
                Text(chat.ask != nil ? "Paused: waiting for you" : "View only")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .lineLimit(1)
                    .layoutPriority(-1)  // gives way to the expand button in a narrow panel
                Button { chat.browserExpanded = true } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(IconButtonStyle(size: 24))
                .help("Make \(app.sammyName)'s browser fill the window (⇧⌘F)")
                .accessibilityLabel("Expand \(app.sammyName)'s browser")
            }
            BrowserPicture(chat: chat)
                .aspectRatio(16 / 10, contentMode: .fit)
                .onTapGesture(count: 2) { chat.browserExpanded = true }
                .help("Double-click to make it fill the window")
            if chat.ask?.kind == .handoff {
                // Sammy is stopped at a hand-off: what the user came to the browser for is to take it over.
                Button { Task { await chat.takeOver() } } label: {
                    HStack(spacing: 6) {
                        if chat.takingOver { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                        Text("Take over the browser")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.primary)
                .disabled(chat.takingOver)
                Text("\(app.sammyName) is waiting for you to take over the browser.")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
            } else if let activity = chat.activity {
                Text(activity).font(.mono(12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(2)
            }
            Spacer()
        }
        .padding(14)
        .background(Palette.surface)
    }
}

/// Sammy's browser as it works, as the latest picture, or what is coming.
struct BrowserPicture: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerLow)
            if let png = chat.screen, let image = NSImage(data: png) {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
                    .accessibilityLabel("\(app.sammyName)'s browser as it works")
            } else {
                VStack(spacing: 8) {
                    if chat.isWorking { SammyMark(mood: .working, size: 18) }
                    Text(chat.isWorking ? "Waiting for \(app.sammyName) to open a page…" : "No picture yet")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                }
            }
        }
        .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.outline))
    }
}

/// Sammy's browser filling the window, to see what it does; view only. The Mac's full screen is a click (or ⌃⌘F)
/// away, and leaving this view leaves full screen too if this view entered it.
struct ExpandedBrowserView: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel
    @State private var fullScreen = false
    @State private var enteredFullScreen = false

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("\(app.sammyName)'s browser").font(.system(size: 13, weight: .semibold))
                    Text(chat.ask != nil ? "Paused: waiting for you" : chat.activity ?? "View only")
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.onSurfaceVariant)
                        .lineLimit(1)
                }
                Spacer(minLength: 12)
                if chat.ask?.kind == .handoff {
                    Button("Take over") { Task { await chat.takeOver() } }
                        .buttonStyle(.sammy(.primary, small: true))
                        .disabled(chat.takingOver)
                }
                Button {
                    enteredFullScreen = !fullScreen
                    window?.toggleFullScreen(nil)
                } label: {
                    Label(fullScreen ? "Exit Full Screen" : "Full Screen",
                          systemImage: fullScreen ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                }
                .buttonStyle(.sammy(.outline, small: true))
                .help(fullScreen ? "Leave full screen (⌃⌘F)" : "Use the whole screen (⌃⌘F)")
                Button("Done") { chat.browserExpanded = false }
                    .buttonStyle(.sammy(.outline, small: true))
                    .keyboardShortcut(.cancelAction)
                    .help("Back to the chat (esc)")
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(Palette.container)
            Divider().overlay(Palette.outline)
            BrowserPicture(chat: chat)
                .padding(12)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(Palette.surface)
        .onAppear { fullScreen = window?.styleMask.contains(.fullScreen) == true }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didEnterFullScreenNotification)) { _ in fullScreen = true }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didExitFullScreenNotification)) { _ in
            fullScreen = false
            enteredFullScreen = false
        }
        .onDisappear {
            if enteredFullScreen, window?.styleMask.contains(.fullScreen) == true { window?.toggleFullScreen(nil) }
        }
    }

    private var window: NSWindow? {
        NSApp.windows.first { $0.identifier?.rawValue.hasPrefix("main") == true } ?? NSApp.keyWindow
    }
}
