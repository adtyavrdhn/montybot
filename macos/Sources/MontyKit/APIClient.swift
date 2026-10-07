import Foundation

public enum APIError: Error, Equatable, LocalizedError {
    /// The session is over (signed out elsewhere, expired, or never signed in).
    case signedOut
    /// The server said no, with its reason when it gave one.
    case server(status: Int, detail: String?)
    /// The server is private: a site login (HTTP basic auth, in front of the app) is needed first. Carries its realm.
    case siteLogin(realm: String)
    /// The server could not be reached.
    case offline(String)
    /// The server answered with something this app does not understand.
    case unexpected(String)

    public var status: Int? {
        if case .server(let status, _) = self { return status }
        return nil
    }

    public var errorDescription: String? {
        switch self {
        case .signedOut:
            return "You were signed out. Sign in again to carry on."
        case .server(let status, let detail):
            if let detail, !detail.isEmpty { return detail.prefix(1).uppercased() + detail.dropFirst() + (detail.hasSuffix(".") ? "" : ".") }
            switch status {
            case 404: return "That is no longer there."
            case 409: return "That changed meanwhile. Try again."
            case 413: return "That is too large."
            case 422: return "Monty could not accept that."
            case 500...: return "Monty's server had a problem. Try again in a moment."
            default: return "Something went wrong (\(status))."
            }
        case .siteLogin:
            return "Monty's server is private. Enter its site login to continue."
        case .offline:
            return "Can't reach Monty. Check your connection."
        case .unexpected:
            return "Monty's server sent something this app doesn't understand. Is the app up to date?"
        }
    }
}

/// An answer to an ask: text for a question, approved for an approval, done for a hand-off.
public struct AnswerBody: Encodable, Equatable, Sendable {
    public var text: String?
    public var approved: Bool?
    public var reason: String?
    public var done: Bool?
    public var note: String?

    public static func text(_ text: String) -> AnswerBody { AnswerBody(text: text) }
    public static func approve() -> AnswerBody { AnswerBody(approved: true) }
    public static func deny(_ reason: String) -> AnswerBody { AnswerBody(approved: false, reason: reason) }
    public static func handBack(note: String = "") -> AnswerBody { AnswerBody(done: true, note: note) }
}

/// What a run's event stream says: the run's state (authoritative), or a provisional preview of the reply.
public enum RunEvent: Equatable, Sendable {
    case status(Run)
    case preview(Preview)
}

/// The montybot server's API, signed in with the session cookie the server sets, as the web app is.
public final class APIClient: Sendable {
    public let baseURL: URL
    public let session: URLSession
    let cookies: HTTPCookieStorage
    /// A private server's site login, as the `Authorization` header every request carries.
    let siteLogin: String?

    /// `cookies` keeps the session: the app's shared storage outlives restarts; tests pass their own, one per user.
    /// `siteLogin` is a private server's login, from `basicAuthorization`.
    public init(baseURL: URL, cookies: HTTPCookieStorage = .shared, siteLogin: String? = nil) {
        self.baseURL = baseURL
        self.cookies = cookies
        self.siteLogin = siteLogin
        let configuration = URLSessionConfiguration.default
        // No credential storage: on macOS it is the keychain, and every 401 would have it ask the user for access
        // (once per request, again after each rebuild of the ad-hoc-signed app). The site login is sent by hand.
        configuration.urlCredentialStorage = nil
        configuration.httpCookieStorage = cookies
        configuration.httpCookieAcceptPolicy = .always
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.timeoutIntervalForRequest = 30
        configuration.waitsForConnectivity = false
        session = URLSession(configuration: configuration)
    }

    /// Whether there is a session cookie for this server; the server still decides if it is valid.
    public var hasSessionCookie: Bool {
        (cookies.cookies(for: baseURL) ?? []).contains { $0.name == "montybot_session" }
    }

    /// Forgets the session cookie: on sign-out, and when the server says it is over.
    public func clearSession() {
        for cookie in cookies.cookies(for: baseURL) ?? [] { cookies.deleteCookie(cookie) }
    }

    // MARK: accounts

    public func signUp(email: String, password: String, name: String = "") async throws -> User {
        try await send("POST", "/api/signup", body: ["email": email, "password": password, "name": name])
    }

    public func signIn(email: String, password: String) async throws -> User {
        try await send("POST", "/api/signin", body: ["email": email, "password": password])
    }

    public func signOut() async throws {
        let _: Ok = try await send("POST", "/api/signout", body: [String: String]())
        clearSession()
    }

    public func me() async throws -> User { try await send("GET", "/api/me") }

    /// Emails a reset code to the account's address (the answer is the same for any address).
    public func requestPasswordReset(email: String) async throws {
        let _: Ok = try await send("POST", "/api/password/reset", body: ["email": email])
    }

    /// Sets a new password with the emailed code, and signs in.
    public func resetPassword(email: String, code: String, password: String) async throws -> User {
        try await send("POST", "/api/password/reset/confirm", body: ["email": email, "code": code, "password": password])
    }

    // MARK: chats

    public func threads() async throws -> [ThreadSummary] { try await send("GET", "/api/threads") }

    public func thread(_ id: String) async throws -> ThreadDetail { try await send("GET", "/api/threads/\(id)") }

    /// Messages carry the Mac's time zone, so the bot knows what "today" and "9am" mean for the user.
    public func startThread(_ text: String) async throws -> Created {
        try await send("POST", "/api/threads", body: ["text": text, "timezone": TimeZone.current.identifier])
    }

    public func send(_ text: String, to thread: String) async throws -> Created {
        try await send("POST", "/api/threads/\(thread)/messages", body: ["text": text, "timezone": TimeZone.current.identifier])
    }

    public func rename(thread: String, to title: String) async throws {
        let _: Ok = try await send("PATCH", "/api/threads/\(thread)", body: ["title": title])
    }

    /// Deletes the chat and everything in it; a task still going is stopped, and a schedule reporting there deleted.
    public func delete(thread: String) async throws {
        let _: Ok = try await send("DELETE", "/api/threads/\(thread)")
    }

    /// Stops the run, whatever it is doing or waiting for.
    public func stop(run: String) async throws {
        let _: Ok = try await send("POST", "/api/runs/\(run)/stop", body: [String: String]())
    }

    public func run(_ id: String) async throws -> Run { try await send("GET", "/api/runs/\(id)") }

    public func answer(_ ask: String, _ body: AnswerBody) async throws {
        let _: Ok = try await send("POST", "/api/asks/\(ask)", body: body)
    }

    /// Starts (or renews) the run's hand-off and says where its live view is.
    public func liveLink(run: String) async throws -> LiveLink {
        try await send("POST", "/api/runs/\(run)/live", body: [String: String]())
    }

    /// The bot's browser as it works, as PNG; nil when there is nothing to show (not running, or busy).
    public func screen(run: String) async throws -> Data? {
        let (data, response) = try await raw(request("GET", "/api/runs/\(run)/screen"))
        switch response.statusCode {
        case 200: return data
        case 404: return nil
        default: throw error(response.statusCode, data)
        }
    }

    /// The run's live updates. Ends when the run finishes or the server closes the stream (every few minutes, to
    /// check the session again); the caller reconnects while the run is active.
    public func events(run: String) -> AsyncThrowingStream<RunEvent, Error> {
        // The server sends a keep-alive every 0.75 s, so 15 s of silence is a dead connection (sleep, a Wi-Fi change).
        let request = request("GET", "/api/runs/\(run)/events", accept: "text/event-stream", timeout: 15)
        let session = session
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else { throw APIError.unexpected("no HTTP response") }
                    if let realm = Self.basicRealm(http) { throw APIError.siteLogin(realm: realm) }
                    if http.statusCode == 401 { throw APIError.signedOut }
                    guard http.statusCode == 200 else { throw APIError.server(status: http.statusCode, detail: nil) }
                    var parser = EventStreamParser()
                    var line = Data()
                    for try await byte in bytes {
                        if byte != UInt8(ascii: "\n") {
                            line.append(byte)
                            continue
                        }
                        if line.last == UInt8(ascii: "\r") { line.removeLast() }
                        if let event = parser.feed(String(decoding: line, as: UTF8.self)), let decoded = RunEvent(event) {
                            continuation.yield(decoded)
                        }
                        line.removeAll(keepingCapacity: true)
                    }
                    continuation.finish()
                } catch let error as URLError where error.code == .cancelled {
                    continuation.finish()
                } catch let error as URLError {
                    continuation.finish(throwing: APIError.offline(error.localizedDescription))
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: schedules, memories, sign-ins, files

    public func schedules() async throws -> [Schedule] { try await send("GET", "/api/schedules") }

    public func setPaused(_ schedule: String, _ paused: Bool) async throws -> Schedule {
        try await send("POST", "/api/schedules/\(schedule)/\(paused ? "pause" : "resume")", body: [String: String]())
    }

    public func deleteSchedule(_ schedule: String) async throws {
        let _: Ok = try await send("DELETE", "/api/schedules/\(schedule)")
    }

    public func memories() async throws -> [Memory] { try await send("GET", "/api/memories") }

    public func deleteMemory(_ memory: String) async throws {
        let _: Ok = try await send("DELETE", "/api/memories/\(memory)")
    }

    public func savedSites() async throws -> [SavedSite] { try await send("GET", "/api/sign-ins") }

    public func forget(site: String) async throws {
        let escaped = site.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(["/"])) ?? site
        let _: Ok = try await send("DELETE", "/api/sign-ins/\(escaped)")
    }

    public func files() async throws -> FileList { try await send("GET", "/api/files") }

    /// A file from the workspace, and the name the server suggests saving it as.
    public func download(_ path: String) async throws -> (data: Data, name: String) {
        var request = request("POST", "/api/files/download")
        request.httpBody = try JSONEncoder().encode(["path": path])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await raw(request)
        guard response.statusCode == 200 else { throw error(response.statusCode, data) }
        let disposition = response.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        let name = disposition.components(separatedBy: "filename*=UTF-8''").dropFirst().first?.removingPercentEncoding
        return (data, name ?? (path as NSString).lastPathComponent)
    }

    // MARK: the live view

    /// The WebSocket request of a live-view link, with the session cookie. No `Origin`: the server accepts clients
    /// that are not browsers without one.
    public func liveSocketRequest(_ link: LiveLink) -> URLRequest? {
        guard var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { return nil }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        parts.path = link.url.hasSuffix("/") ? link.url + "ws" : link.url + "/ws"
        guard let url = parts.url else { return nil }
        var request = URLRequest(url: url)
        for (name, value) in HTTPCookie.requestHeaderFields(with: cookies.cookies(for: baseURL) ?? []) {
            request.setValue(value, forHTTPHeaderField: name)
        }
        request.setValue(siteLogin, forHTTPHeaderField: "Authorization")
        return request
    }

    // MARK: plumbing

    private struct Ok: Decodable {}

    func request(_ method: String, _ path: String, accept: String = "application/json", timeout: TimeInterval = 30) -> URLRequest {
        var request = URLRequest(url: baseURL.appending(path: path), timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue(accept, forHTTPHeaderField: "Accept")
        request.setValue(siteLogin, forHTTPHeaderField: "Authorization")
        return request
    }

    private func send<T: Decodable>(_ method: String, _ path: String) async throws -> T {
        try await decode(request(method, path))
    }

    private func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, body: B) async throws -> T {
        var request = request(method, path)
        request.httpBody = try JSONEncoder().encode(body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return try await decode(request)
    }

    private func decode<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await raw(request)
        guard (200..<300).contains(response.statusCode) else {
            // Signing in with the wrong password is a 401 too, but not a session that ended.
            let signingIn = ["/api/signin", "/api/signup", "/api/password/reset/confirm"].contains(request.url?.path ?? "")
            throw error(response.statusCode, data, signingIn: signingIn)
        }
        do {
            return try Self.decoder.decode(T.self, from: data)
        } catch {
            throw APIError.unexpected("\(request.url?.path ?? ""): \(error)")
        }
    }

    private func raw(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch let error as URLError {
            if error.code == .cancelled { throw CancellationError() }
            throw APIError.offline(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.unexpected("no HTTP response") }
        if let realm = Self.basicRealm(http) { throw APIError.siteLogin(realm: realm) }
        return (data, http)
    }

    /// The realm of a private server's site login, when this answer asks for one (a 401 that wants basic auth).
    static func basicRealm(_ response: HTTPURLResponse) -> String? {
        guard response.statusCode == 401,
              let challenge = response.value(forHTTPHeaderField: "WWW-Authenticate"), challenge.lowercased().hasPrefix("basic")
        else { return nil }
        return challenge.firstMatch(of: /realm="([^"]*)"/).map { String($0.1) } ?? ""
    }

    /// The `Authorization` header value of a site login.
    public static func basicAuthorization(user: String, password: String) -> String {
        "Basic " + Data("\(user):\(password)".utf8).base64EncodedString()
    }

    private func error(_ status: Int, _ data: Data, signingIn: Bool = false) -> APIError {
        let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"]
        if status == 401, !signingIn { return .signedOut }
        // A validation error's detail is a list of problems; show the server's text only when it is a sentence.
        return .server(status: status, detail: detail as? String)
    }

    static let decoder = JSONDecoder()
}

extension RunEvent {
    init?(_ event: ServerSentEvent) {
        let data = Data(event.data.utf8)
        switch event.name {
        case "status":
            guard let run = try? APIClient.decoder.decode(Run.self, from: data) else { return nil }
            self = .status(run)
        case "preview":
            guard let preview = try? APIClient.decoder.decode(Preview.self, from: data) else { return nil }
            self = .preview(preview)
        default:
            return nil
        }
    }
}
