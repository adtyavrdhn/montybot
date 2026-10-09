import SammyKit
import SwiftUI

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

