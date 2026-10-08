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

// MARK: - what Sammy asks

/// Its content at its own height, up to `limit`, and scrolling beyond: what sits under the chat can never ask for
/// more height than the window has. (A card measured at the column's narrowest once claimed thousands of points, and
/// the whole window was laid out taller than itself: an empty sidebar, an empty browser panel, the chat cut off.)
struct BoundedHeight<Content: View>: View {
    var limit: CGFloat = 320
    @ViewBuilder let content: Content
    @State private var height: CGFloat = 0

    var body: some View {
        ScrollView(.vertical) {
            content
                .padding(4)  // room for focus rings and the card's edge, which the scroll view would clip
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height = $0 }
        }
        .padding(-4)
        .scrollBounceBehavior(.basedOnSize)
        .scrollIndicators(.automatic)
        .frame(height: min(height, limit))
    }
}

struct AskCard: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    let ask: Ask
    @State private var reason = ""
    @FocusState private var focus: Field?
    @AccessibilityFocusState private var announced: Bool

    enum Field { case answer, reason }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                Text(AppModel.headline(for: ask.kind, from: app.sammyName))
                    .font(.system(size: 13, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityFocused($announced)
                Spacer(minLength: 8)
                // Stop stays at hand where Send was: the box at the bottom.
                Button { Task { await chat.stop() } } label: { Label("Stop task", systemImage: "stop.fill").font(.system(size: 11)) }
                    .buttonStyle(.sammy(.ghost, small: true))
                    .disabled(!chat.canStop)
                    .help("Stop this task instead of answering (⌘.)")
            }
            MarkdownView(text: ask.prompt)
            controls.padding(.top, 2)
            if let error = chat.answerError {
                NoticeBar(notice: .error(error)) { chat.answerError = nil }
            }
        }
        .card(padding: 16)
        .disabled(chat.answering)
        .onChange(of: chat.denying) { _, denying in if denying { focus = .reason } }
        .onAppear {
            // Into the answer box only if the user isn't writing something else.
            if ask.kind == .question { focus = .answer }
            announced = true
        }
    }

    @ViewBuilder private var controls: some View {
        switch ask.kind {
        case .question:
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Your answer", text: $chat.answerDraft, prompt: Text("Your answer"), axis: .vertical)
                    .labelsHidden()
                    .accessibilityLabel("Your answer")
                    .lineLimit(1...6)
                    .focused($focus, equals: .answer)
                    .padding(.vertical, 6)
                    .field(focused: focus == .answer)
                    .onSubmit(sendAnswer)
                Button("Answer", action: sendAnswer)
                    .buttonStyle(.primary)
                    .disabled(chat.answerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        case .approval:
            if chat.denying {
                HStack(spacing: 8) {
                    TextField("Why not?", text: $reason, prompt: Text("Tell \(app.sammyName) why not (optional)"))
                        .labelsHidden()
                        .accessibilityLabel("Why not? Optional")
                        .focused($focus, equals: .reason)
                        .field(focused: focus == .reason)
                        .onSubmit(deny)
                        .onExitCommand { chat.denying = false }
                    Button("Don't do it", action: deny).buttonStyle(.destructive)
                    Button("Back") { chat.denying = false }.buttonStyle(.ghost)
                }
            } else {
                HStack(spacing: 8) {
                    // No keyboard shortcut: going ahead with something that costs money takes a deliberate click.
                    Button("Approve") { Task { await chat.answer(.approve()) } }
                        .buttonStyle(.primary)
                    Button("Don't approve…") { chat.denying = true }
                        .buttonStyle(.outline)
                }
            }
        case .handoff:
            VStack(alignment: .leading, spacing: 8) {
                Button { Task { await chat.takeOver() } } label: {
                    HStack(spacing: 6) {
                        if chat.takingOver { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                        Text("Take over the browser")
                    }
                }
                .buttonStyle(.primary)
                .disabled(chat.takingOver)
                .accessibilityHint("Opens \(app.sammyName)'s browser. VoiceOver reads the page; activating an item clicks it, and typing goes into the page. Shift-Command-T closes it, Command-Return hands it back.")
                // A hand-off is any step Sammy gives the user (a sign-in, a code, a CAPTCHA, a payment, or just because
                // they asked); Sammy's reason above says which, so this note must hold for all of them.
                Text("\(app.sammyName) waits until you hand it back. Password managers can't fill in this page: copy a password and paste it with ⌘V. If you sign in to a site, \(app.sammyName) stays signed in there; you can remove it in Saved sign-ins.")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
            }
        case .connect:
            ConnectControls(chat: chat, ask: ask)
        case .other:
            Text("This app can't show what \(app.sammyName) needs here yet. Answer it in Sammy on the web.")
                .font(.system(size: 12))
                .foregroundStyle(Palette.onSurfaceVariant)
        }
    }

    private func sendAnswer() {
        guard !chat.answerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        Task { await chat.answer(.text(chat.answerDraft.trimmingCharacters(in: .whitespacesAndNewlines))) }
    }

    private func deny() {
        let why = reason.trimmingCharacters(in: .whitespaces)
        Task { await chat.answer(.deny(why.isEmpty ? "The user said no." : why)) }
    }
}

/// What a connect card offers: sign in to the app (or the user's own server) in the browser, or add an MCP server
/// for a service no app is offered for; and "Not now". The server hears when a sign-in finishes and the run carries
/// on by itself; "I've connected it" is for when the browser never came back.
struct ConnectControls: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    let ask: Ask
    /// The token a known MCP server takes (GitHub's), pasted here; cleared once it is added.
    @State private var token = ""
    @State private var connecting = false
    @FocusState private var tokenFocused: Bool

    var body: some View {
        let offer = ask.integration ?? Offer(provider: "mcp", key: "", name: "the app")
        let canSignIn = offer.isApp || offer.serverId != nil || offer.isPreset
        let wantsToken = offer.isPreset && offer.needsToken
        let connected = chat.connectingAsk == ask.id
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                AppLogo(url: offer.logo, name: offer.name, size: 28)
                Text(canSignIn ? "Connect \(offer.name)" : "Add \(offer.name)").font(.system(size: 13, weight: .medium))
            }
            if connected && wantsToken {
                Text("\(offer.name) is added. If \(app.sammyName) doesn't carry on by itself, say you've connected it.")
                    .font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
            } else if connected {
                Text("Finish signing in to \(offer.name) in your browser. \(app.sammyName) carries on when you have.")
                    .font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
            } else if !canSignIn {
                Text("\(offer.name) isn't one of the apps Sammy connects in one click. If it has an MCP server, add it in Integrations.")
                    .font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
            } else if wantsToken {
                SecureField("Token", text: $token, prompt: Text(offer.tokenHint ?? "Token"))
                    .labelsHidden().accessibilityLabel("Token for \(offer.name)")
                    .focused($tokenFocused).field(focused: tokenFocused)
                    .onSubmit { connect(wantsToken) }
            }
            HStack(spacing: 8) {
                if !(connected && wantsToken) {
                    Button(canSignIn ? (offer.serverId == nil ? "Connect \(offer.name)" : "Sign in to \(offer.name)") : "Add an MCP server") {
                        connect(wantsToken)
                    }
                    .buttonStyle(.primary)
                    .disabled(wantsToken && (connecting || token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty))
                }
                if connected {
                    Button("I've connected it") { Task { await chat.answer(.connected(true)) } }.buttonStyle(.outline)
                }
                Button("Not now") { Task { await chat.answer(.connected(false)) } }.buttonStyle(.ghost)
            }
        }
    }

    private func connect(_ wantsToken: Bool) {
        guard !wantsToken || (!connecting && !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) else { return }
        Task {
            connecting = true
            defer { connecting = false }
            await chat.connect(token: wantsToken ? token : nil)
            if chat.connectingAsk == ask.id { token = "" }  // the token is not left on screen
        }
    }
}

// MARK: - the composer

struct Composer: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    var prominent = false
    @FocusState private var focused: Bool
    @State private var picking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
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
