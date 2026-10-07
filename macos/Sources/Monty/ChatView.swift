import MontyKit
import SwiftUI

struct ChatView: View {
    @Environment(AppModel.self) private var app
    @Bindable var chat: ChatModel
    @State private var confirmingStop = false

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
        .navigationTitle(chat.title.isEmpty ? "New task" : chat.title.readableTitle)
        .navigationSubtitle(subtitle)
        .toolbar {
            ToolbarItem(placement: .navigation) {
                if chat.threadId != nil { MontyMark(mood: mood, size: 14).help(subtitle) }
            }
            ToolbarItemGroup(placement: .primaryAction) {
                if chat.isActive {
                    Button { chat.watching.toggle() } label: {
                        Label(chat.watching ? "Hide Monty's browser" : "Watch Monty's browser", systemImage: "macwindow")
                    }
                    .help(chat.watching ? "Hide Monty's browser (⇧⌘B)" : "Watch Monty's browser (⇧⌘B)")
                }
                if chat.isActive {
                    Button { confirmingStop = true } label: { Label("Stop", systemImage: "stop.circle") }
                        .disabled(!chat.canStop)
                        .help("Stop this task (⌘.)")
                }
            }
        }
        .inspector(isPresented: Binding(get: { chat.watching && chat.isActive }, set: { chat.watching = $0 })) {
            BrowserPanel(chat: chat)
                .inspectorColumnWidth(min: 280, ideal: 420, max: 720)
        }
        .confirmationDialog("Stop this task?", isPresented: $confirmingStop) {
            Button("Stop task", role: .destructive) { Task { await chat.stop() } }
            Button("Keep going", role: .cancel) {}
        } message: {
            Text("Monty stops where it is and won't finish this task.")
        }
        .onReceive(NotificationCenter.default.publisher(for: .montyStopTask)) { _ in
            if chat.canStop { confirmingStop = true }
        }
        .onChange(of: chat.run?.status) { old, new in
            guard old?.isActive == true, let new, !new.isActive else { return }
            let words = new == .done ? "Monty finished." : new == .failed ? "Monty couldn't finish this task." : "Task stopped."
            AccessibilityNotification.Announcement(words).post()
        }
    }

    /// The mascot's mood for this chat: working, waiting for the user, or how the last task ended.
    private var mood: MontyMark.Mood {
        if chat.ask != nil { return .waiting }
        if chat.isWorking { return .working }
        switch chat.run?.status {
        case .failed: return .failed
        case .done: return .done
        default: return .idle
        }
    }

    private var subtitle: String {
        if let ask = chat.ask { return AppModel.headline(for: ask.kind) }
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
                                MontyMark(mood: .working, size: 22).frame(maxWidth: .infinity)
                            }
                            if !chat.loading, chat.shownMessages.isEmpty, !chat.isActive {
                                EmptyState(
                                    icon: "text.bubble",
                                    title: "Nothing here yet",
                                    text: "When a schedule runs, Monty reports here. You can also write to Monty below."
                                )
                            }
                            ForEach(Array(chat.shownMessages.enumerated()), id: \.offset) { _, message in
                                MessageView(message: message).transition(.arrive)
                            }
                            if let text = chat.preview?.text, !text.isEmpty, chat.isWorking {
                                MessageView(message: ChatMessage(role: .assistant, text: text), draft: true)
                            }
                            if !chat.steps.isEmpty || chat.isWorking {
                                StepsView(chat: chat)
                            }
                            if let ask = chat.ask {
                                AskCard(chat: chat, ask: ask).id(ask.id).transition(.arrive)
                            }
                            if chat.canRetry {
                                Button { Task { await chat.retry() } } label: {
                                    Label("Try again", systemImage: "arrow.clockwise")
                                }
                                .buttonStyle(.monty(.outline, small: true))
                                .disabled(chat.sending)
                            }
                            Color.clear.frame(height: 1).id("end")
                        }
                        .motion(.spring(response: 0.42, dampingFraction: 0.86), value: chat.shownMessages.count)
                        .motion(.spring(response: 0.42, dampingFraction: 0.86), value: chat.ask?.id)
                        .frame(maxWidth: Metrics.readingWidth, alignment: .leading)
                        .padding(.horizontal, Metrics.gutter)
                        .padding(.top, 24)
                        .padding(.bottom, 12)
                        .frame(maxWidth: .infinity, minHeight: geometry.size.height)
                    }
                    .defaultScrollAnchor(.bottom)
                }
                .onChange(of: chat.shownMessages.count) { scrollToEnd(scroller) }
                .onChange(of: chat.ask?.id) { scrollToEnd(scroller) }
                .onChange(of: chat.preview?.text) { scroller.scrollTo("end", anchor: .bottom) }
            }
            HStack(alignment: .bottom, spacing: 2) {
                // Monty keeps you company by the composer, reacting as the task goes. A new chat gets a fresh squirrel.
                MontySquirrel(mood: mood, size: 112, layoutHeight: 52)
                    .padding(.leading, -30)
                    .padding(.trailing, -18)
                    .padding(.bottom, -8)
                    .id(chat.threadId)
                Composer(chat: chat)
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

extension Notification.Name {
    /// ⌘. from the Task menu: the open chat asks before stopping.
    static let montyStopTask = Notification.Name("montyStopTask")
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
    let message: ChatMessage
    var draft = false

    var body: some View {
        switch message.role {
        case .user:
            HStack {
                Spacer(minLength: 80)
                Text(message.text)
                    .font(.system(size: 14))
                    .lineSpacing(3)
                    .textSelection(.enabled)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerHigh))
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("You said: \(message.text)")
        case .assistant:
            VStack(alignment: .leading, spacing: 6) {
                if draft {
                    Text("Writing…").font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
                }
                MarkdownView(text: message.text)
                    .opacity(draft ? 0.7 : 1)
                    .contentTransition(.opacity)
                    .motion(.easeOut(duration: 0.2), value: message.text)
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel(draft ? "Monty is writing" : "Monty said")
        }
    }
}

/// What Monty did in this run, one line per step: live while it works, then folded under the reply.
struct StepsView: View {
    let chat: ChatModel
    @State private var expanded = false

    var body: some View {
        let steps = chat.steps
        let working = chat.isWorking
        let shown = working && !expanded ? Array(steps.suffix(3)) : expanded ? steps : []
        VStack(alignment: .leading, spacing: 0) {
            let expandable = !working || steps.count > 3
            HStack(spacing: 0) {
                Group {
                    if working {
                        MontyMark(mood: .working, size: 10)
                    } else {
                        Image(systemName: "chevron.right")
                            .font(.system(size: 9, weight: .semibold))
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                            .motion(.spring(response: 0.3, dampingFraction: 0.8), value: expanded)
                    }
                }
                .frame(width: 16, height: 18, alignment: .leading)  // the same slot either way, so the text never moves
                .accessibilityHidden(true)
                Text(working ? (chat.activity ?? "Starting…") : "\(steps.count) step\(steps.count == 1 ? "" : "s")")
                    .lineLimit(1)
                    .contentTransition(.opacity)
                    .motion(.easeInOut(duration: 0.25), value: chat.activity)
                if working, steps.count > 3 {
                    Text(expanded ? "  ·  Show fewer" : "  ·  Show all \(steps.count)").foregroundStyle(Palette.actionText)
                }
                Spacer(minLength: 0)
            }
            .font(.system(size: 12))
            .foregroundStyle(Palette.onSurfaceVariant)
            .frame(minHeight: 24)
            .contentShape(Rectangle())
            .onTapGesture { if expandable { withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() } } }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(working ? "Monty is working: \(chat.activity ?? "starting")" : "\(steps.count) steps Monty took")
            .accessibilityAddTraits(expandable ? .isButton : [])
            .accessibilityAction { if expandable { expanded.toggle() } }
            .accessibilityValue(expandable ? (expanded ? "Shown" : "Hidden") : "")

            if !shown.isEmpty {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(shown.enumerated()), id: \.offset) { index, step in
                        let live = working && index == shown.count - 1
                        HStack(alignment: .top, spacing: 10) {
                            VStack(spacing: 0) {
                                Circle()
                                    .fill(live ? Palette.onSurfaceVariant : Palette.outlineHover)
                                    .frame(width: 5, height: 5)
                                    .padding(.top, 6)
                                if index < shown.count - 1 { Rectangle().fill(Palette.outline).frame(width: 1).frame(maxHeight: .infinity) }
                            }
                            .frame(width: 6)
                            .padding(.leading, 2)
                            Text(step)
                                .font(.mono(12))
                                .foregroundStyle(live ? Palette.onSurface : Palette.onSurfaceVariant)
                                .lineLimit(2)
                                .padding(.bottom, index < shown.count - 1 ? 6 : 0)
                            Spacer(minLength: 0)
                        }
                        .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(.top, 6)
                .transition(.opacity)
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

// MARK: - what Monty asks

struct AskCard: View {
    @Bindable var chat: ChatModel
    let ask: Ask
    @State private var denying = false
    @State private var reason = ""
    @FocusState private var focus: Field?
    @AccessibilityFocusState private var announced: Bool

    enum Field { case answer, reason }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(AppModel.headline(for: ask.kind))
                .font(.system(size: 13, weight: .semibold))
                .accessibilityAddTraits(.isHeader)
                .accessibilityFocused($announced)
            MarkdownView(text: ask.prompt)
            controls.padding(.top, 2)
        }
        .card(padding: 16)
        .disabled(chat.answering)
        .onAppear {
            // Into the answer box only if the user isn't writing something else.
            if ask.kind == .question, chat.draft.isEmpty { focus = .answer }
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
            if denying {
                HStack(spacing: 8) {
                    TextField("Why not?", text: $reason, prompt: Text("Tell Monty why not (optional)"))
                        .labelsHidden()
                        .accessibilityLabel("Why not? Optional")
                        .focused($focus, equals: .reason)
                        .field(focused: focus == .reason)
                        .onSubmit(deny)
                        .onExitCommand { denying = false }
                    Button("Don't do it", action: deny).buttonStyle(.destructive)
                    Button("Back") { denying = false }.buttonStyle(.ghost)
                }
            } else {
                HStack(spacing: 8) {
                    // No keyboard shortcut: going ahead with something that costs money takes a deliberate click.
                    Button("Approve") { Task { await chat.answer(.approve()) } }
                        .buttonStyle(.primary)
                    Button("Don't approve…") { denying = true; focus = .reason }
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
                .accessibilityHint("Opens Monty's browser. VoiceOver reads the page; activating an item clicks it, and typing goes into the page. Shift-Command-T closes it, Command-Return hands it back.")
                Text("You sign in on the page yourself, and Monty waits until you're done. Password managers can't fill it in: copy your password and paste it with ⌘V. Afterwards Monty stays signed in to this site; you can remove it in Saved sign-ins.")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onSurfaceVariant)
                    .fixedSize(horizontal: false, vertical: true)
            }
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

// MARK: - the composer

struct Composer: View {
    @Bindable var chat: ChatModel
    var prominent = false
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let notice = chat.notice {
                NoticeBar(notice: notice) { chat.notice = nil }
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Message Monty", text: $chat.draft, prompt: Text(placeholder).foregroundStyle(Palette.onSurfaceVariant), axis: .vertical)
                    .labelsHidden()
                    .accessibilityLabel("Message Monty")
                    .accessibilityHint(placeholder)
                    .textFieldStyle(.plain)
                    .font(.system(size: prominent ? 15 : 14))
                    .lineLimit(prominent ? 3...10 : 1...10)
                    .focused($focused)
                    .padding(.vertical, 6)
                    .onSubmit { if chat.canSend { Task { await chat.send() } } }
                    .disabled(chat.ask != nil && chat.draft.isEmpty)  // text kept here stays reachable
                Button { Task { await chat.send() } } label: {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 13, weight: .bold))
                        .frame(width: Metrics.control, height: Metrics.control)
                        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(chat.canSend ? Palette.action : Palette.containerHigh))
                        .foregroundStyle(chat.canSend ? Palette.onLink : Palette.onSurfaceVariant)
                }
                .buttonStyle(.plain)
                .disabled(!chat.canSend)
                .help("Send (↩). ⌥↩ starts a new line.")
                .accessibilityLabel("Send")
                .animation(.easeOut(duration: 0.15), value: chat.canSend)
            }
            .padding(.leading, 16)
            .padding(.trailing, 6)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(chat.ask != nil ? Palette.containerLowest : Palette.container))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(focused ? Palette.link : Palette.outline, lineWidth: focused ? 1.5 : 1))
            .motion(.easeOut(duration: 0.18), value: focused)
            .shadow(color: .black.opacity(0.04), radius: 6, y: 2)
            .onTapGesture { focused = true }
        }
        .animation(.easeOut(duration: 0.2), value: chat.notice)
        .onAppear { if chat.ask == nil { focused = true } }
        .onChange(of: chat.ask == nil) { _, free in if free { focused = true } }
    }

    private var placeholder: String {
        if let ask = chat.ask {
            switch ask.kind {
            case .question: return "Answer Monty's question above"
            case .approval: return "Approve or decline above to carry on"
            case .handoff: return "Monty is waiting for you to take over the browser"
            }
        }
        if chat.isActive { return "Monty is working. You can reply when it's done." }
        if chat.threadId == nil { return "Ask Monty to do something on the web…" }
        return "Reply to Monty…"
    }
}

/// A notice above the composer: neutral for information, red for errors. Errors stay until dismissed.
struct NoticeBar: View {
    let notice: ChatNotice
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: notice.isError ? "exclamationmark.circle" : "info.circle").accessibilityHidden(true)
            Text(notice.text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 4)
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
                MontySquirrel(mood: .idle, size: 168, layoutHeight: 120).padding(.leading, -42).padding(.bottom, -6)
                Text("What should Monty do?")
                    .font(.system(size: 22, weight: .semibold))
                    .accessibilityAddTraits(.isHeader)
                Text("Monty works on the web in its own browser and asks when it needs you.")
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
                    Text("Monty asks before it buys or sends anything, and you sign in to sites yourself.")
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

// MARK: - watching Monty's browser

struct BrowserPanel: View {
    let chat: ChatModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("Monty's browser").sectionLabel()
                Spacer()
                Text(chat.ask != nil ? "Paused: waiting for you" : "View only").font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant)
            }
            ZStack {
                RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.containerLow)
                if let png = chat.screen, let image = NSImage(data: png) {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.high)
                        .aspectRatio(contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
                        .accessibilityLabel("Monty's browser as it works")
                } else {
                    VStack(spacing: 8) {
                        if chat.isWorking { MontyMark(mood: .working, size: 18) }
                        Text(chat.isWorking ? "Waiting for Monty to open a page…" : "No picture yet")
                            .font(.system(size: 12))
                            .foregroundStyle(Palette.onSurfaceVariant)
                    }
                }
            }
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.outline))
            .aspectRatio(16 / 10, contentMode: .fit)
            if let activity = chat.activity {
                Text(activity).font(.mono(12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(2)
            }
            Spacer()
        }
        .padding(14)
        .background(Palette.surface)
    }
}
