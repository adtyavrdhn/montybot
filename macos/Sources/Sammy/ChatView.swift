import SammyKit
import SwiftUI

struct ChatView: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    /// The end of the chat is on screen: new content scrolls into view. Scrolled up to read, it stays put.
    @State private var atBottom = true
    /// Something arrived below while the user was reading further up.
    @State private var missed = false

    var body: some View {
        Group {
            if chat.threadId == nil && chat.pendingMessage == nil {
                NewTaskView(chat: chat)
            } else if let error = chat.loadError {
                EmptyState(icon: "exclamationmark.triangle", title: "Couldn't open this chat", text: error) {
                    Button("Try again") { Task { await chat.load() } }.buttonStyle(.outline)
                }
            } else {
                conversation
            }
        }
        .modifier(DropFiles(chat: chat))
        .navigationTitle(chat.title.isEmpty ? "New task" : chat.title.readableTitle)
        .navigationSubtitle(subtitle)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                if chat.threadId != nil { SammyMark(mood: mood, size: 14).help(subtitle) }
            }
            ToolbarItemGroup(placement: .primaryAction) {
                if chat.isActive && app.tunnelStatus.browsing > 0 {
                    Label("Browsing from your Mac", systemImage: "laptopcomputer.and.arrow.down")
                        .labelStyle(.titleAndIcon)
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .help("Sites see this Mac's internet connection. You can turn this off in Settings.")
                }
                if chat.run != nil {
                    Button { chat.watching.toggle() } label: {
                        Label(chat.watching ? "Hide \(app.sammyName)'s browser" : "Watch \(app.sammyName)'s browser", systemImage: "macwindow")
                    }
                    .help(chat.watching ? "Hide \(app.sammyName)'s browser (⇧⌘B)" : "Watch \(app.sammyName)'s browser (⇧⌘B)")
                }
                if let thread = app.openThread {
                    Menu {
                        let isPinned = app.pinned.contains(thread.id)
                        Button(isPinned ? "Unpin" : "Pin to Top") { app.setPinned(thread, !isPinned) }
                        Button("Rename…") { app.renaming = thread }
                        Button("Copy \(app.sammyName)'s Last Reply") { copy(lastReply) }.disabled(lastReply == nil)
                        Divider()
                        Button("Delete Chat…", role: .destructive) { app.deleting = thread }
                    } label: {
                        Label("Chat", systemImage: "ellipsis.circle")
                    }
                    .help("Pin, rename, copy or delete this chat")
                }
            }
        }
        .inspector(isPresented: Binding(get: { chat.watching }, set: { chat.watching = $0 })) {
            BrowserPanel(chat: chat)
                .inspectorColumnWidth(min: 280, ideal: 340, max: 720)
        }
        .onExitCommand { if chat.watching { chat.watching = false } }  // esc closes Sammy's browser beside the chat
        .onChange(of: chat.run?.status) { old, new in
            guard old?.isActive == true, let new, !new.isActive else { return }
            let words = new == .done ? "\(app.sammyName) finished." : new == .failed ? "\(app.sammyName) couldn't finish this task." : "Task stopped."
            AccessibilityNotification.Announcement(words).post()
        }
    }

    private var lastReply: String? { chat.messages.last { $0.role == .assistant }?.text }

    /// The mascot's mood for this chat: working, waiting for the user, or how the last task ended.
    private var mood: SammyMark.Mood {
        if chat.ask != nil { return .waiting }
        if chat.isWorking { return .working }
        switch chat.run?.status {
        case .failed: return .failed
        case .done: return .done
        default: return .idle
        }
    }

    private var subtitle: String {
        if let ask = chat.ask { return AppModel.headline(for: ask.kind, from: app.sammyName) }
        if chat.isWorking { return chat.reconnecting ? "Connection lost. Reconnecting…" : "Working" }
        switch chat.run?.status {
        case .failed: return "Couldn't finish"
        case .stopped: return "Stopped"
        default: return ""
        }
    }

    private var conversation: some View {
        VStack(spacing: 0) {
            ScrollViewReader { scroller in
                GeometryReader { geometry in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            Spacer(minLength: 0)  // a short chat sits just above the composer, as in Messages
                            if chat.loading {
                                SammyMark(mood: .working, size: 22).frame(maxWidth: .infinity)
                            }
                            if !chat.loading, chat.shownMessages.isEmpty, !chat.isActive {
                                EmptyState(
                                    icon: "text.bubble",
                                    title: "Nothing here yet",
                                    text: "When a schedule runs, \(app.sammyName) reports here. You can also write to \(app.sammyName) below."
                                )
                            }
                            ForEach(Array(chat.shownMessages.enumerated()), id: \.offset) { index, message in
                                MessageView(message: message).transition(.arrive)
                                if let past = chat.pastSteps[index] { PastStepsView(steps: past) }
                            }
                            if let text = chat.preview?.text, !text.isEmpty, chat.isWorking {
                                MessageView(message: ChatMessage(role: .assistant, text: text), draft: true)
                            }
                            if !chat.steps.isEmpty || chat.isWorking {
                                StepsView(chat: chat)
                            }
                            if chat.canRetry {
                                HStack(spacing: 8) {
                                    Button { Task { await chat.retry() } } label: {
                                        Label("Try again", systemImage: "arrow.clockwise")
                                    }
                                    .buttonStyle(.sammy(.outline, small: true))
                                    .help("Send the same task again (⌘R)")
                                    Button { chat.editLastTask() } label: {
                                        Label("Edit and send again", systemImage: "pencil")
                                    }
                                    .buttonStyle(.sammy(.ghost, small: true))
                                    .help("Put the task in the message box to change it first")
                                }
                                .transition(.opacity)
                            }
                            Color.clear.frame(height: 1).id("end")
                                .background(GeometryReader { end in
                                    Color.clear.preference(key: EndOfChat.self, value: end.frame(in: .named("chat")).minY)
                                })
                        }
                        .motion(.spring(response: 0.42, dampingFraction: 0.86), value: chat.shownMessages.count)
                        .motion(.spring(response: 0.42, dampingFraction: 0.86), value: chat.ask?.id)
                        .frame(maxWidth: Metrics.readingWidth, alignment: .leading)
                        .padding(.horizontal, Metrics.gutter)
                        .padding(.top, 24)
                        .padding(.bottom, 12)
                        .frame(maxWidth: .infinity, minHeight: geometry.size.height)
                    }
                    .coordinateSpace(.named("chat"))
                    .defaultScrollAnchor(.bottom)
                    .onPreferenceChange(EndOfChat.self) { end in
                        // Within a few lines of the end counts as at the end.
                        atBottom = end <= geometry.size.height + 60
                        if atBottom { missed = false }
                    }
                    .overlay(alignment: .bottom) {
                        if !atBottom {
                            Button { scrollToEnd(scroller) } label: {
                                Label(missed ? "New messages" : "Jump to latest", systemImage: "arrow.down")
                                    .font(.system(size: 12, weight: .medium))
                            }
                            .buttonStyle(.sammy(missed ? .primary : .outline, small: true))
                            .shadow(color: .black.opacity(0.08), radius: 6, y: 2)
                            .padding(.bottom, 10)
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .help("Scroll to the end of the chat")
                        }
                    }
                    .animation(.easeOut(duration: 0.18), value: atBottom)
                }
                .onChange(of: chat.shownMessages.count) {
                    // What the user just sent always shows; anything else waits until they are at the end.
                    if atBottom || chat.pendingMessage != nil { scrollToEnd(scroller) } else { missed = true }
                }
                .onChange(of: chat.ask?.id) { if atBottom { scrollToEnd(scroller) } else if chat.ask != nil { missed = true } }
                .onChange(of: chat.preview?.text) { if atBottom { scroller.scrollTo("end", anchor: .bottom) } }
            }
            HStack(alignment: .bottom, spacing: 2) {
                // Sammy keeps you company by the composer, reacting as the task goes. A new chat gets a fresh squirrel.
                SammySquirrel(mood: mood, size: 112, layoutHeight: 52)
                    .padding(.leading, -30)
                    .padding(.trailing, -18)
                    .padding(.bottom, -8)
                    .id(chat.threadId)
                // What Sammy asks takes the message box's place, as in T3 Code: the user answers where they type.
                Group {
                    if let ask = chat.ask {
                        VStack(alignment: .leading, spacing: 8) {
                            if let notice = chat.notice { NoticeBar(notice: notice) { chat.notice = nil } }
                            BoundedHeight {
                                AskCard(chat: chat, ask: ask).id(ask.id)
                            }
                            .shadow(color: .black.opacity(0.06), radius: 8, y: 2)
                            // What the user had written, or queued, stays in sight under the card, for after.
                            if chat.queued != nil || !chat.attachments.isEmpty
                                || !chat.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                                Composer(chat: chat)
                            }
                        }
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                    } else {
                        Composer(chat: chat).transition(.opacity)
                    }
                }
                .motion(.spring(response: 0.38, dampingFraction: 0.88), value: chat.ask?.id)
            }
            .frame(maxWidth: Metrics.readingWidth)
            .padding(.horizontal, Metrics.gutter)
            .padding(.bottom, 16)
            .frame(maxWidth: .infinity)
        }
    }

    private func scrollToEnd(_ scroller: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.2)) { scroller.scrollTo("end", anchor: .bottom) }
    }
}

extension View {
    /// The view as a plain button when `enabled`, otherwise as it is.
    @ViewBuilder func wrappedInButton(enabled: Bool, action: @escaping () -> Void) -> some View {
        if enabled { Button(action: action) { self }.buttonStyle(.plain) } else { self }
    }
}

/// Where the end of the chat is, from the top of what is on screen.
private struct EndOfChat: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

extension Notification.Name {
    /// ⌘L: the message box takes the keyboard.
    static let sammyFocusMessage = Notification.Name("sammyFocusMessage")
}

/// Puts text on the Mac's clipboard.
func copy(_ text: String?) {
    guard let text else { return }
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

/// A calm empty state, the same everywhere: an icon, a title and a sentence, centred.
struct EmptyState<Actions: View>: View {
    let icon: String
    let title: String
    let text: String
    @ViewBuilder var actions: () -> Actions

    init(icon: String, title: String, text: String, @ViewBuilder actions: @escaping () -> Actions = { EmptyView() }) {
        self.icon = icon
        self.title = title
        self.text = text
        self.actions = actions
    }

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: icon).font(.system(size: 20)).foregroundStyle(Palette.onSurfaceVariant).accessibilityHidden(true)
            Text(title).font(.system(size: 14, weight: .medium))
            Text(text)
                .font(.system(size: 13))
                .foregroundStyle(Palette.onSurfaceVariant)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 340)
            actions().padding(.top, 6)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 40)
        .accessibilityElement(children: .contain)
    }
}

// MARK: - messages

struct MessageView: View {
    @Environment(AppModel.self) private var app
    let message: ChatMessage
    var draft = false
    @State private var hovering = false
    @State private var copied = false

    var body: some View {
        switch message.role {
        case .user:
            HStack {
                Spacer(minLength: 80)
                VStack(alignment: .trailing, spacing: 6) {
                    if !message.files.isEmpty {
                        MessageFiles(files: message.files, trailing: true)
                            .accessibilityElement(children: .contain)
                            .accessibilityLabel("You attached \(message.files.count) file\(message.files.count == 1 ? "" : "s")")
                    }
                    if !message.text.isEmpty {  // a message of files alone is just its files
                        Text(message.text)
                            .font(.system(size: 14))
                            .lineSpacing(3)
                            .textSelection(.enabled)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 8)
                            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerHigh))
                            .contextMenu { Button("Copy") { copy(message.text) } }
                            .accessibilityLabel("You said: \(message.text)")
                            .accessibilityAction(named: "Copy") { copy(message.text) }
                    }
                }
            }
            .accessibilityElement(children: .contain)
        case .assistant:
            VStack(alignment: .leading, spacing: 6) {
                if draft {
                    Text("Writing…").font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
                }
                MarkdownView(text: message.text)
                    .opacity(draft ? 0.7 : 1)
                    .contentTransition(.opacity)
                    .motion(.easeOut(duration: 0.2), value: message.text)
                if !message.files.isEmpty {
                    MessageFiles(files: message.files)
                        .padding(.top, 2)
                        .accessibilityElement(children: .contain)
                        .accessibilityLabel("Sammy shared \(message.files.count) file\(message.files.count == 1 ? "" : "s")")
                }
                if !draft {
                    // Under each reply, as in other chat apps: shown on hover, so replies stay calm to read.
                    Button {
                        copy(message.text)
                        copied = true
                    } label: {
                        Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 11))
                    }
                    .buttonStyle(.sammy(.ghost, small: true))
                    .opacity(hovering || copied ? 1 : 0)
                    .help("Copy this reply")
                    .accessibilityHidden(true)  // the reply's own Copy action does this for VoiceOver
                    .task(id: copied) {
                        guard copied else { return }
                        try? await Task.sleep(for: .seconds(1.5))
                        copied = false
                    }
                }
            }
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
            .contextMenu { if !draft { Button("Copy") { copy(message.text) } } }
            .accessibilityElement(children: .contain)
            .accessibilityLabel(draft ? "\(app.sammyName) is writing" : "\(app.sammyName) said")
            .accessibilityAction(named: "Copy") { copy(message.text) }
        case .event:
            // What happened along the way (an approval, a takeover): a quiet line, not a message.
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: Self.icon(for: message.text)).accessibilityHidden(true)
                Text(message.text)
            }
            .font(.system(size: 12))
            .foregroundStyle(Palette.onSurfaceVariant)
            .textSelection(.enabled)
        }
    }

    /// The server words these lines in `chat_messages` (sammy/api.py); anything else gets the plain mark.
    private static func icon(for text: String) -> String {
        if text.hasPrefix("You approved") { return "checkmark.circle" }
        if text.hasPrefix("You said no") { return "xmark.circle" }
        if text.hasPrefix("You took over") { return "hand.point.up.left" }
        if text.hasPrefix("Not answered") { return "clock" }
        return "smallcircle.filled.circle"
    }
}

/// What Sammy did in this run, one line per step: live while it works, then folded under the reply.
struct StepsView: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel
    @State private var expanded = false

    /// After the run: how long it took, when the server says, and how many steps. "Worked for 1m 3s · 5 steps".
    private var summary: String {
        let count = chat.steps.count
        let steps = "\(count) step\(count == 1 ? "" : "s")"
        guard let started = chat.run?.started, let completed = chat.run?.completed else { return steps }
        return "Worked for \(spoken(completed.timeIntervalSince(started))) · \(steps)"
    }

    var body: some View {
        let steps = chat.steps
        let working = chat.isWorking
        let shown = working && !expanded ? Array(steps.suffix(3)) : expanded ? steps : []
        VStack(alignment: .leading, spacing: 0) {
            let expandable = !working || steps.count > 3
            HStack(spacing: 0) {
                Group {
                    if working {
                        SammyMark(mood: .working, size: 10)
                    } else {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                            .motion(.spring(response: 0.3, dampingFraction: 0.8), value: expanded)
                    }
                }
                .frame(width: 16, height: 18, alignment: .leading)  // the same slot either way, so the text never moves
                .accessibilityHidden(true)
                if working, chat.run?.status.isWorking == true, chat.pendingMessage == nil, let started = chat.run?.started {
                    // As T3 Code: how long Sammy has been at it, ticking, with the steps below.
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        Text("Working for \(spoken(context.date.timeIntervalSince(started)))").monospacedDigit()
                    }
                    .lineLimit(1)
                } else {
                    Text(working ? (chat.activity ?? "Starting…") : summary)
                        .lineLimit(1)
                        .contentTransition(.opacity)
                        .motion(.easeInOut(duration: 0.25), value: chat.activity)
                }
                if working, steps.count > 3 {
                    Text(expanded ? "  ·  Show fewer" : "  ·  Show all \(steps.count)").foregroundStyle(Palette.actionText)
                }
                Spacer(minLength: 0)
            }
            .font(.system(size: 12))
            .foregroundStyle(Palette.onSurfaceVariant)
            .frame(minHeight: 24)
            .contentShape(Rectangle())
            // A button, so the keyboard reaches it too (with keyboard navigation on), not only the pointer.
            .wrappedInButton(enabled: expandable) { withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() } }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(working ? "\(app.sammyName) is working: \(chat.activity ?? "starting")" : "\(summary), what \(app.sammyName) did")
            .accessibilityAddTraits(expandable ? .isButton : [])
            .accessibilityAction { if expandable { expanded.toggle() } }
            .accessibilityValue(expandable ? (expanded ? "Shown" : "Hidden") : "")

            if !shown.isEmpty {
                StepList(steps: shown, liveLast: working).padding(.top, 6).transition(.opacity)
            }
            if chat.reconnecting {
                Text("Connection lost. Reconnecting…")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onWarningContainer)
                    .padding(.top, 6)
            }
        }
        .animation(.easeOut(duration: 0.2), value: steps.count)
    }
}

/// Steps as a line down the side, oldest first; the last one stands out while Sammy is on it.
struct StepList: View {
    let steps: [String]
    var liveLast = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                let live = liveLast && index == steps.count - 1
                HStack(alignment: .top, spacing: 10) {
                    VStack(spacing: 0) {
                        Circle()
                            .fill(live ? Palette.onSurfaceVariant : Palette.outlineHover)
                            .frame(width: 5, height: 5)
                            .padding(.top, 6)
                        if index < steps.count - 1 { Rectangle().fill(Palette.outline).frame(width: 1).frame(maxHeight: .infinity) }
                    }
                    .frame(width: 6)
                    .padding(.leading, 2)
                    Text(step)
                        .font(.mono(12))
                        .foregroundStyle(live ? Palette.onSurface : Palette.onSurfaceVariant)
                        .lineLimit(2)
                        .padding(.bottom, index < steps.count - 1 ? 6 : 0)
                    Spacer(minLength: 0)
                }
                .fixedSize(horizontal: false, vertical: true)  // inside the chat's scroll view: no window to stretch
            }
        }
    }
}

/// What Sammy did for an earlier reply: folded, as "Worked for 1m 3s · 5 steps", to open.
struct PastStepsView: View {
    @Environment(AppModel.self) private var app
    let steps: PastSteps
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .rotationEffect(.degrees(expanded ? 90 : 0))
                    .motion(.spring(response: 0.3, dampingFraction: 0.8), value: expanded)
                    .frame(width: 16, height: 18, alignment: .leading)
                    .accessibilityHidden(true)
                Text(steps.summary).lineLimit(1)
                Spacer(minLength: 0)
            }
            .font(.system(size: 12))
            .foregroundStyle(Palette.onSurfaceVariant)
            .frame(minHeight: 24)
            .contentShape(Rectangle())
            .wrappedInButton(enabled: true) { withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() } }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(steps.summary), what \(app.sammyName) did")
            .accessibilityAddTraits(.isButton)
            .accessibilityValue(expanded ? "Shown" : "Hidden")
            .accessibilityAction { expanded.toggle() }
            if expanded {
                StepList(steps: steps.steps).padding(.top, 6).transition(.opacity)
            }
        }
    }
}
