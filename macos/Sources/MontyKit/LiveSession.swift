import Foundation
import Observation

/// The user driving the run's browser during a hand-off: frames in, input out, over the live view's WebSocket.
@MainActor
@Observable
public final class LiveSession {
    public enum State: Equatable, Sendable {
        case connecting
        case driving
        /// The hand-off was opened somewhere else (another window, the web app); `reconnect()` takes it back.
        case elsewhere
        case reconnecting
        /// Over: given back from here (`givenBack`), or ended some other way.
        case ended(givenBack: Bool)
        case failed(String)

        public var isOver: Bool {
            switch self {
            case .ended, .failed: return true
            default: return false
            }
        }
    }

    public private(set) var state: State = .connecting
    public private(set) var reason: String
    public private(set) var frame: LiveFrame?
    public private(set) var tabs: [LiveTab] = []
    /// What is on the page, for a screen reader; kept fresh while `wantsOutline` (VoiceOver is on).
    public private(set) var outline: PageOutline?
    /// Set while a screen reader is running: the page's outline is asked for and kept up to date.
    public var wantsOutline = false {
        didSet { if wantsOutline, !oldValue { refreshOutline(after: 0) } }
    }
    /// The server refused the connection because the session ended.
    public private(set) var signedOut = false
    /// The latest input the browser refused, in words; cleared after a few seconds.
    public private(set) var notice: String?
    public private(set) var givingBack = false

    public var activeTab: LiveTab? { tabs.first(where: \.active) }

    private let request: URLRequest
    private let session: URLSession
    private var task: URLSessionWebSocketTask?
    private var receiving: Task<Void, Never>?
    private var retries = 0
    private var pendingMove: (x: Double, y: Double)?
    private var pendingScroll: (x: Double, y: Double, dx: Double, dy: Double)?
    private var flushScheduled = false
    private var noticeClear: Task<Void, Never>?
    private var givingBackTimeout: Task<Void, Never>?
    private var outlineRefresh: Task<Void, Never>?
    static let maxRetries = 6

    public init(request: URLRequest, reason: String, session: URLSession) {
        self.request = request
        self.reason = reason
        self.session = session
    }

    public func connect() {
        guard !state.isOver else { return }
        receiving?.cancel()
        task?.cancel(with: .goingAway, reason: nil)
        let task = session.webSocketTask(with: request)
        task.maximumMessageSize = 16 * 1024 * 1024
        self.task = task
        state = retries == 0 ? .connecting : .reconnecting
        task.resume()
        receiving = Task { [weak self] in await self?.receive(on: task) }
    }

    /// Take the hand-off back after it was opened somewhere else.
    public func reconnect() {
        retries = 0
        connect()
    }

    public func close() {
        outlineRefresh?.cancel()
        receiving?.cancel()
        receiving = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        if !state.isOver { state = .ended(givenBack: false) }
    }

    public func giveBack() {
        guard state == .driving, !givingBack else { return }
        flush()
        givingBack = true  // from here on the page is Monty's again: no more input from the user
        send(.giveBack)
        givingBackTimeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(10))
            guard let self, !Task.isCancelled, self.givingBack, !self.state.isOver else { return }
            self.givingBack = false
            self.show("Monty didn't confirm. Try giving it back again.")
        }
    }

    public func switchTab(_ tab: LiveTab) { send(.switchTab(tab.id)) }

    // MARK: input

    /// Mouse moves and scrolls are coalesced to one message per display frame; everything else goes at once, after
    /// any pending move, so a click lands where the pointer is.
    public func input(_ input: LiveInput) {
        guard state == .driving, !givingBack else { return }
        switch input {
        case .mouseMove(let x, let y):
            pendingMove = (x, y)
            scheduleFlush()
        case .scroll(let x, let y, let dx, let dy):
            let previous = pendingScroll
            pendingScroll = (x, y, (previous?.dx ?? 0) + dx, (previous?.dy ?? 0) + dy)
            scheduleFlush()
        default:
            flush()
            send(input)
            refreshOutline(after: 0.4)  // the page changes after a click or key; read it again once it settles
        }
    }

    /// Asks for the page's outline after `delay` seconds, then every few seconds while wanted: pages change on
    /// their own too (a message appears, a spinner ends).
    public func refreshOutline(after delay: Double) {
        guard wantsOutline, !state.isOver else { return }
        outlineRefresh?.cancel()
        outlineRefresh = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            while !Task.isCancelled {
                guard let self, self.wantsOutline, !self.state.isOver else { return }
                if self.state == .driving { self.send(.outline) }
                try? await Task.sleep(for: .seconds(3))
            }
        }
    }

    public func type(_ text: String) {
        guard !text.isEmpty else { return }
        input(.type(text))
    }

    private func scheduleFlush() {
        guard !flushScheduled else { return }
        flushScheduled = true
        Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(16))
            self?.flush()
        }
    }

    private func flush() {
        flushScheduled = false
        if let move = pendingMove {
            pendingMove = nil
            send(.mouseMove(x: move.x, y: move.y))
        }
        if let scroll = pendingScroll {
            pendingScroll = nil
            send(.scroll(x: scroll.x, y: scroll.y, deltaX: scroll.dx, deltaY: scroll.dy))
        }
    }

    private func send(_ input: LiveInput) {
        task?.send(.string(input.json)) { _ in }
    }

    // MARK: receiving

    private func receive(on task: URLSessionWebSocketTask) async {
        while !Task.isCancelled {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await task.receive()
            } catch {
                if Task.isCancelled || self.task !== task { return }
                closed(code: task.closeCode.rawValue)
                return
            }
            if self.task !== task { return }
            if state != .driving, !state.isOver { state = .driving }
            switch message {
            case .data(let data):
                guard let frame = LiveFrame(data: data), frame.seq >= (self.frame?.seq ?? 0) || self.frame == nil else { continue }
                self.frame = frame
                retries = 0  // only a picture proves the connection works; a hello alone does not
            case .string(let text):
                guard let message = LiveServerMessage(json: text) else { continue }
                handle(message)
            @unknown default:
                continue
            }
        }
    }

    private func handle(_ message: LiveServerMessage) {
        switch message {
        case .hello(_, let reason):
            self.reason = reason
            frame = nil  // a new connection numbers its frames from 1
            refreshOutline(after: 0)
        case .tabs(let tabs):
            self.tabs = tabs
            refreshOutline(after: 0.6)
        case .outline(let outline):
            self.outline = outline
        case .error(let text):
            show(text)
        case .ended(let givenBack):
            state = .ended(givenBack: givenBack)
            givingBack = false
            givingBackTimeout?.cancel()
            task?.cancel(with: .normalClosure, reason: nil)
        }
    }

    private func show(_ text: String) {
        notice = text
        noticeClear?.cancel()
        noticeClear = Task { [weak self] in
            try? await Task.sleep(for: .seconds(4))
            if !Task.isCancelled { self?.notice = nil }
        }
    }

    private func closed(code: Int) {
        guard !state.isOver else { return }
        givingBack = false
        givingBackTimeout?.cancel()
        // A refused upgrade never opens the socket: the HTTP status says why.
        switch (task?.response as? HTTPURLResponse)?.statusCode {
        case 401: signedOut = true; state = .failed("You were signed out. Sign in again to take over."); return
        case 403: state = .failed("Monty's server refused this connection."); return
        case 404: state = .failed("This hand-off is no longer open."); return
        default: break
        }
        switch code {
        case LiveCloseCode.ended: state = .ended(givenBack: false)
        case LiveCloseCode.notFound: state = .failed("This hand-off is no longer open.")
        case LiveCloseCode.signedOut: signedOut = true; state = .failed("You were signed out. Sign in again to take over.")
        case LiveCloseCode.replaced: state = .elsewhere
        case 1008: state = .failed("Monty's server refused this connection.")
        default:
            retries += 1
            guard retries <= Self.maxRetries else {
                state = .failed("Couldn't reconnect to Monty's browser.")
                return
            }
            state = .reconnecting
            let delay = min(0.5 * pow(2, Double(retries - 1)), 8)
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard let self, self.state == .reconnecting else { return }
                self.connect()
            }
        }
    }
}
