import Foundation
import Observation

/// Something to tell the user about what they just did, shown once above the composer.
public struct ChatNotice: Equatable, Sendable {
    public let text: String
    public let isError: Bool

    public static func error(_ text: String) -> ChatNotice { ChatNotice(text: text, isError: true) }
    public static func info(_ text: String) -> ChatNotice { ChatNotice(text: text, isError: false) }
}

/// One chat as the user sees it: its messages, the run in progress, what the bot asks, and the composer.
///
/// The run is followed over its event stream; the stored thread is the truth, and is read again whenever the stream
/// says the run changed state, so what is on screen never stays wrong for long. A stream that fails falls back to
/// reading the thread until it reconnects. Once `close()`d (the user looked elsewhere), nothing it started may
/// start anything again: requests still in flight finish into a model nobody sees.
@MainActor
@Observable
public final class ChatModel {
    /// nil until the first message creates the thread.
    public private(set) var threadId: String?
    public private(set) var title: String
    public private(set) var messages: [ChatMessage] = []
    public private(set) var run: Run?
    /// What the bot is writing now; shown as a draft below the messages, never stored.
    public private(set) var preview: Preview?
    public private(set) var loading = false
    /// The first load failed: the chat could not be shown at all.
    public private(set) var loadError: String?
    /// The live stream dropped; updates come from polling until it reconnects.
    public private(set) var reconnecting = false
    /// A message the user sent that the server has not stored yet.
    public private(set) var pendingMessage: String?
    public private(set) var sending = false
    public private(set) var answering = false
    public private(set) var stopping = false
    public private(set) var takingOver = false
    /// The bot's browser as it works (PNG), while `watching`.
    public private(set) var screen: Data?
    public private(set) var live: LiveSession?
    /// Shown once above the composer, then cleared by the view.
    public var notice: ChatNotice?

    public var draft: String {
        didSet { if !closed { app?.drafts[draftKey] = draft } }
    }
    /// What the user is typing in answer to the open question, kept per question so it survives redraws.
    public var answerDraft: String {
        get { ask.map { app?.answerDrafts[$0.id]?.text ?? "" } ?? "" }
        set {
            guard let ask, let threadId else { return }
            app?.answerDrafts[ask.id] = AnswerDraft(threadId: threadId, text: newValue)
        }
    }
    /// Whether the bot's browser panel is open. It opens while the run is going on (working, or waiting for the
    /// user, when they most want to look), and closes when it ends.
    public var watching = false {
        didSet {
            if watching, !isActive || closed { watching = false; return }
            watching ? startWatching() : stopWatching()
        }
    }

    public var ask: Ask? { run?.status == .waiting ? run?.ask : nil }
    public var isWorking: Bool { pendingMessage != nil || run?.status.isWorking == true }
    public var isActive: Bool { pendingMessage != nil || run?.status.isActive == true }
    public var canSend: Bool {
        !sending && !isActive && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    /// There is a run on the server to stop (not just a message on its way).
    public var canStop: Bool { run?.status.isActive == true && !stopping }
    /// The latest thing the bot did, in words, for the status line.
    public var activity: String? {
        guard isWorking else { return nil }
        if let text = preview?.activity, !text.isEmpty { return text }
        return run?.activity.last
    }
    /// What the bot did in the latest run, the live step included, oldest first.
    public var steps: [String] {
        var steps = run?.activity ?? []
        if isWorking, let live = preview?.activity, !live.isEmpty, live != steps.last { steps.append(live) }
        return steps
    }
    /// The last task failed: it can be sent again as it was.
    public var canRetry: Bool {
        run?.status == .failed && !isActive && messages.last(where: { $0.role == .user }) != nil
    }

    private weak var app: AppModel?
    private let client: APIClient
    private var following: Task<Void, Never>?
    private var followingRun: String?
    private var watchTask: Task<Void, Never>?
    private var draftKey: String { threadId ?? "new" }
    /// The run the pending message started, once the server has made it.
    private var pendingRun: String?
    private var closed = false
    /// The question being answered right now: it closing is the answer arriving, not something to rescue.
    private var answeringAsk: String?

    init(app: AppModel, threadId: String?, title: String = "") {
        self.app = app
        client = app.client
        self.threadId = threadId
        self.title = title
        draft = app.drafts[threadId ?? "new"] ?? ""
    }

    // MARK: loading

    public func load() async {
        guard threadId != nil else { return }
        loading = messages.isEmpty
        await refresh()
        loading = false
    }

    /// Reads the stored thread and shows it; false if it could not. Starts following the run if one is going on.
    @discardableResult
    public func refresh() async -> Bool {
        guard let threadId, !closed else { return false }
        do {
            let detail = try await client.thread(threadId)
            guard detail.id == self.threadId, !closed else { return false }
            loadError = nil
            title = detail.title
            messages = detail.messages
            if let pending = pendingMessage, let pendingRun,
               detail.run?.id == pendingRun || detail.messages.contains(where: { $0.role == .user && $0.text == pending }) {
                pendingMessage = nil  // the server shows the sent message now
                self.pendingRun = nil
            }
            apply(detail.run)
            return true
        } catch let error as APIError {
            if closed { return false }
            if error == .signedOut { app?.sessionEnded(); return false }
            if error.status == 404 { app?.chatVanished(threadId); return false }
            if messages.isEmpty { loadError = error.localizedDescription }
            return false
        } catch {
            return false
        }
    }

    private func apply(_ run: Run?) {
        guard !closed else { return }
        self.run = run
        if !isActive, watching { watching = false }
        if run?.status.isActive != true { preview = nil; screen = nil }
        if let live, !live.state.isOver, !live.givingBack, run?.status != .waiting || run?.ask?.kind != .handoff {
            live.close()  // the hand-off was answered elsewhere, or the run stopped: the live view is over
        }
        rescueAnswers()
        if let run, run.status.isActive { follow(run.id) } else { stopFollowing() }
        app?.chatChanged(self)
    }

    /// A question the user was answering in this chat went away (answered elsewhere, stopped, or replaced), now or
    /// while they were looking at another chat: keep what they typed.
    private func rescueAnswers() {
        guard let app, let threadId else { return }
        let stale = app.answerDrafts.filter { $0.value.threadId == threadId && $0.key != ask?.id && $0.key != answeringAsk }
        let texts = stale.keys.sorted().compactMap { app.answerDrafts.removeValue(forKey: $0)?.text.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        guard !texts.isEmpty else { return }
        let where_ = keep(texts.joined(separator: "\n\n"))
        notice = .info("That question was closed before you sent your answer. What you wrote is \(where_).")
    }

    /// Puts text the user wrote back where they can use it: in the answer to the question now open if it is one and
    /// that is empty, otherwise in the composer after anything typed since. Says where, for the notice.
    @discardableResult
    private func keep(_ text: String) -> String {
        if let ask, ask.kind == .question, answerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            answerDraft = text
            return "in the answer box"
        }
        let current = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        draft = current.isEmpty ? text : current + "\n\n" + text
        return "in the message box"
    }

    // MARK: following the run

    private func follow(_ run: String) {
        guard followingRun != run, !closed else { return }
        stopFollowing()
        followingRun = run
        following = Task { [weak self] in await self?.followLoop(run) }
    }

    private func stopFollowing() {
        following?.cancel()
        following = nil
        followingRun = nil
        reconnecting = false
    }

    private func followLoop(_ runId: String) async {
        defer { if followingRun == runId, !Task.isCancelled { followingRun = nil } }
        var failures = 0
        while !Task.isCancelled, followingRun == runId, !closed {
            var gotEvents = false
            var firstPreview = true  // a restarted server counts revisions from zero again
            do {
                for try await event in client.events(run: runId) {
                    guard followingRun == runId, !closed else { return }
                    gotEvents = true
                    failures = 0
                    reconnecting = false
                    switch event {
                    case .preview(let preview):
                        if firstPreview || preview.revision >= (self.preview?.revision ?? 0) { self.preview = preview }
                        firstPreview = false
                    case .status(let status):
                        guard status.id == runId else { continue }
                        if status.status.isActive {
                            apply(status)
                        } else {
                            // The stored reply and "finished" show together. If the thread can't be read right now,
                            // show that it finished anyway (without stopping this loop), and keep trying for the reply.
                            if await refresh() { return }
                            run = status
                            preview = nil
                            app?.chatChanged(self)
                            await refreshUntilShown()
                            return
                        }
                    }
                }
            } catch APIError.signedOut {
                app?.sessionEnded()
                return
            } catch {
                if Task.isCancelled || closed { return }
                failures += 1
                reconnecting = true
            }
            if Task.isCancelled || followingRun != runId || closed { return }
            // The stream ended: the server closes it every few minutes, or the connection dropped. Check the stored
            // state (it may have finished meanwhile), then reconnect, backing off while it keeps failing.
            await refresh()
            guard followingRun == runId, run?.status.isActive == true else { return }
            let delay = gotEvents ? 0.2 : min(0.75 * pow(2, Double(max(failures - 1, 0))), 8)
            try? await Task.sleep(for: .seconds(delay))
        }
    }

    /// Reads the thread until it works, backing off: after a run ends, its reply must show even through a blip.
    private func refreshUntilShown() async {
        var delay = 0.5
        while !closed, !Task.isCancelled {
            if await refresh() { return }
            reconnecting = true
            try? await Task.sleep(for: .seconds(delay))
            delay = min(delay * 2, 8)
        }
    }

    // MARK: sending

    /// Sends the draft. A new chat is created by its first message.
    public func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canSend else { return }
        draft = ""
        guard await !submit(text) else { return }
        // Nothing the user wrote is lost: back in the box (before anything typed since), or in this chat's saved
        // draft if it closed meanwhile.
        if !closed {
            let typed = draft.trimmingCharacters(in: .whitespacesAndNewlines)
            draft = typed.isEmpty ? text : text + "\n\n" + typed
        } else {
            app?.keepDraft(text, for: draftKey)
        }
    }

    /// Sends `text` as the user's next message; false if the server did not take it.
    private func submit(_ text: String) async -> Bool {
        guard let app, !sending, !isActive, !closed else { return false }
        sending = true
        pendingMessage = text
        defer { sending = false }
        do {
            let created = threadId == nil ? try await client.startThread(text) : try await client.send(text, to: threadId!)
            app.taskSent()
            guard !closed else { app.refreshThreads(); return true }
            if threadId == nil {
                // What the user typed while it sent belongs to this chat now, not to the next new one.
                app.drafts["new"] = nil
                threadId = created.threadId
                app.drafts[created.threadId] = draft
                title = String(text.split(separator: "\n").first ?? Substring(text))
                app.chatCreated(self)
            }
            pendingRun = created.runId
            run = Run(id: created.runId, threadId: created.threadId, status: .queued)
            follow(created.runId)
            await refresh()
            app.refreshThreads()
            return true
        } catch let error as APIError {
            pendingMessage = nil
            if error == .signedOut { app.sessionEnded(); return false }
            if closed { return false }
            notice = .error(error.status == 409 ? "Monty is still on the last task in this chat. Wait for it, or stop it first." : error.localizedDescription)
            return false
        } catch {
            pendingMessage = nil
            return false
        }
    }

    public func retry() async {
        guard canRetry, let last = messages.last(where: { $0.role == .user }) else { return }
        _ = await submit(last.text)
    }

    /// The messages to show: the stored ones, then the one being sent.
    public var shownMessages: [ChatMessage] {
        guard let pendingMessage else { return messages }
        return messages + [ChatMessage(role: .user, text: pendingMessage)]
    }

    // MARK: answering

    public func answer(_ body: AnswerBody) async {
        guard let ask, !answering, !closed else { return }
        answering = true
        answeringAsk = ask.id
        defer { answering = false; answeringAsk = nil }
        do {
            try await client.answer(ask.id, body)
            app?.answerDrafts[ask.id] = nil
            guard !closed else { return }
            if let run { self.run = Run(id: run.id, threadId: run.threadId, status: .running, activity: run.activity) }
            app?.chatChanged(self)
            await refresh()
        } catch let error as APIError {
            if error == .signedOut { app?.sessionEnded(); return }
            if error.status == 409 {  // answered already, somewhere else
                if let text = app?.answerDrafts.removeValue(forKey: ask.id)?.text.trimmingCharacters(in: .whitespacesAndNewlines),
                   !text.isEmpty, body.text != nil {
                    answeringAsk = nil
                    await refresh()  // first see what is open now, so the text goes where it can be used
                    let where_ = keep(text)
                    notice = .info("That question was already answered, maybe in another window. What you wrote is \(where_).")
                    return
                } else {
                    notice = .info("That was already answered, maybe in another window. Here's where things are now.")
                }
                await refresh()
                return
            }
            notice = .error(error.localizedDescription)
        } catch {}
    }

    public func stop() async {
        guard let run, run.status.isActive, !stopping, !closed else { return }
        stopping = true
        defer { stopping = false }
        do {
            try await client.stop(run: run.id)
        } catch let error as APIError {
            if error == .signedOut { app?.sessionEnded(); return }
            if error.status != 409 { notice = .error(error.localizedDescription) }
        } catch {}
        live?.close()
        await refresh()
        app?.refreshThreads()
    }

    // MARK: the bot's browser

    /// Opens the run's browser for the user to drive, when the bot handed off to them. One at a time.
    public func takeOver() async {
        guard let run, let ask, ask.kind == .handoff, !takingOver, !closed else { return }
        if let live, !live.state.isOver { return }
        takingOver = true
        defer { takingOver = false }
        do {
            let link = try await client.liveLink(run: run.id)
            guard !closed, self.ask?.id == ask.id, let request = client.liveSocketRequest(link) else { return }
            live?.close()
            let session = LiveSession(request: request, reason: link.reason, session: client.session)
            live = session
            session.connect()
        } catch let error as APIError {
            if error == .signedOut { app?.sessionEnded(); return }
            if error.status == 404 { await refresh(); return }
            notice = .error(error.localizedDescription)
        } catch {}
    }

    /// Closes the live view without giving the browser back; the bot keeps waiting.
    public func leaveLiveView() {
        live?.close()
        live = nil
    }

    /// The live view ended: the run carries on (or the hand-off ended some other way). Read the new state.
    public func liveViewEnded() async {
        live = nil
        await refresh()
    }

    /// The live view gave up (it could not connect): start a new one with a fresh link.
    public func retryTakeOver() async {
        live = nil
        await takeOver()
    }

    private func startWatching() {
        guard watchTask == nil, !closed else { return }
        watchTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.fetchScreen()  // no strong reference while sleeping
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    func renamed(_ id: String, to title: String) {
        if threadId == id { self.title = title }
    }

    /// The live view was refused because the session ended.
    public func liveSignedOut() {
        live = nil
        app?.sessionEnded()
    }

    private func fetchScreen() async {
        guard let run, run.status == .running, !closed else { return }  // waiting: keep the last picture
        if let png = try? await client.screen(run: run.id), !Task.isCancelled, !closed, self.run?.id == run.id {
            screen = png
        }
    }

    private func stopWatching() {
        watchTask?.cancel()
        watchTask = nil
    }

    /// The chat is no longer on screen: stop everything it was doing, for good.
    public func close() {
        closed = true
        stopFollowing()
        stopWatching()
        live?.close()
    }
}
