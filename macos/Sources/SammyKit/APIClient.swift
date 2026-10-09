import Foundation
@preconcurrency import OpenTelemetryApi

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
            case 422: return "Sammy could not accept that."
            case 500...: return "Sammy's server had a problem. Try again in a moment."
            default: return "Something went wrong (\(status))."
            }
        case .siteLogin:
            return "Sammy's server is private. Enter its site login to continue."
        case .offline:
            return "Can't reach Sammy. Check your connection."
        case .unexpected:
            return "Sammy's server sent something this app doesn't understand. Is the app up to date?"
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
    public var connected: Bool?

    public static func text(_ text: String) -> AnswerBody { AnswerBody(text: text) }
    public static func approve() -> AnswerBody { AnswerBody(approved: true) }
    public static func deny(_ reason: String) -> AnswerBody { AnswerBody(approved: false, reason: reason) }
    public static func handBack(note: String = "") -> AnswerBody { AnswerBody(done: true, note: note) }
    /// A connect ask: true once they have connected it (the run checks for itself), false for not now.
    public static func connected(_ connected: Bool) -> AnswerBody { AnswerBody(connected: connected) }
}

/// What a run's event stream says: the run's state (authoritative), or a provisional preview of the reply.
public enum RunEvent: Equatable, Sendable {
    case status(Run)
    case preview(Preview)
}

/// The sammy server's API, signed in with the session cookie the server sets, as the web app is.
public final class APIClient: Sendable {
    public let baseURL: URL
    public let session: URLSession
    let cookies: HTTPCookieStorage
    /// A private server's site login, as the `Authorization` header every request carries.
    let siteLogin: String?
    /// The app's traces for this server: off until `startTelemetry`, and when the server takes none.
    public let telemetry = Telemetry()
    public let modelSelection = ModelPreferencesModel()

    /// `cookies` keeps the session: the app's shared storage outlives restarts; tests pass their own, one per user.
    /// `siteLogin` is a private server's login, from `basicAuthorization`.
    public convenience init(baseURL: URL, cookies: HTTPCookieStorage = .shared, siteLogin: String? = nil) {
        self.init(baseURL: baseURL, cookies: cookies, siteLogin: siteLogin, protocols: nil)
    }

    /// `protocols` stand in for the network, in tests.
    init(baseURL: URL, cookies: HTTPCookieStorage, siteLogin: String?, protocols: [AnyClass]?) {
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
        if let protocols { configuration.protocolClasses = protocols }
        session = URLSession(configuration: configuration)
    }

    /// Whether there is a session cookie for this server; the server still decides if it is valid.
    public var hasSessionCookie: Bool {
        (cookies.cookies(for: baseURL) ?? []).contains { $0.name == "sammy_session" }
    }

    /// Forgets the session cookie: on sign-out, and when the server says it is over.
    public func clearSession() {
        for cookie in cookies.cookies(for: baseURL) ?? [] { cookies.deleteCookie(cookie) }
    }

    // MARK: telemetry

    /// Whether the server takes the app's telemetry, and with content or not (signed in only).
    public func telemetrySettings() async throws -> TelemetrySettings {
        try await decode(request("GET", "/api/telemetry"), quiet: true)
    }

    /// Sends the app's traces for this user if the server takes them, through this client's session and site login;
    /// stops if it no longer does. Never fails: telemetry is not worth bothering the user about.
    public func startTelemetry(userId: String) async {
        let generation = telemetry.currentGeneration  // signing out while the server answers wins
        let settings = (try? await telemetrySettings()) ?? .off
        guard !telemetry.isConfigured(settings, userId: userId) else { return }
        let exporter = Telemetry.exporter(settings, baseURL: baseURL, session: session, siteLogin: siteLogin)
        telemetry.configure(settings, exporter: exporter, userId: userId, generation: generation)
    }

    // MARK: accounts

    public func signUp(email: String, password: String, name: String = "") async throws -> User {
        try await send("POST", "/api/signup", body: ["email": email, "password": password, "name": name])
    }

    public func signIn(email: String, password: String) async throws -> User {
        try await send("POST", "/api/signin", body: ["email": email, "password": password])
    }

    public func signOut() async throws {
        await modelSelection.reset()
        let _: Ok = try await send("POST", "/api/signout", body: [String: String]())
        clearSession()
    }

    public func modelPreferences() async throws -> ModelPreferences {
        try await send("GET", "/api/model-preferences")
    }

    public func setModelPreferences(_ update: ModelPreferencesUpdate) async throws -> ModelPreferences {
        try await send("PUT", "/api/model-preferences", body: update)
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

    /// The ids of the chats whose title, tasks or replies mention `query`, latest first.
    public func search(_ query: String) async throws -> [String] {
        var search = request("GET", "/api/search")
        search.url = search.url?.appending(queryItems: [URLQueryItem(name: "q", value: query)])  // not in the path: escaped
        let found: SearchResult = try await decode(search)
        return found.ids
    }

    /// Messages carry the Mac's time zone, so the bot knows what "today" and "9am" mean for the user, and the name the
    /// user gave their squirrel, which the bot answers to (empty forgets it; nil leaves it as it is). `attachments` are
    /// the ids of files uploaded for it (`upload`); with some, `text` may be empty.
    public func startThread(_ text: String, attachments: [String] = [], squirrelName: String? = nil) async throws -> Created {
        try await modelSelection.waitForSave()
        return try await send("POST", "/api/threads", body: MessageBody(text, attachments, squirrelName))
    }

    public func send(_ text: String, to thread: String, attachments: [String] = [], squirrelName: String? = nil) async throws -> Created {
        try await modelSelection.waitForSave()
        return try await send("POST", "/api/threads/\(thread)/messages", body: MessageBody(text, attachments, squirrelName))
    }

    struct MessageBody: Encodable {
        let text: String
        let timezone = TimeZone.current.identifier
        /// Left out when there are none, as older servers know no such field.
        let attachments: [String]?
        let squirrelName: String?

        enum CodingKeys: String, CodingKey {
            case text, timezone, attachments
            case squirrelName = "squirrel_name"
        }

        init(_ text: String, _ attachments: [String], _ squirrelName: String?) {
            self.text = text
            self.attachments = attachments.isEmpty ? nil : attachments
            self.squirrelName = squirrelName
        }
    }

    // MARK: attachments

    /// Uploads a file for the user's next message, as it is: its bytes are the body, its name a header (which the
    /// server requires, so a web page can't upload for the user). It is sent with a message by its id.
    public func upload(data: Data, name: String, mediaType: String) async throws -> Attachment {
        try await decode(uploadRequest(data: data, name: name, mediaType: mediaType))
    }

    func uploadRequest(data: Data, name: String, mediaType: String) -> URLRequest {
        var request = request("POST", "/api/attachments")
        request.httpBody = data
        request.setValue(mediaType.isEmpty ? "application/octet-stream" : mediaType, forHTTPHeaderField: "Content-Type")
        request.setValue(name.addingPercentEncoding(withAllowedCharacters: Self.unreserved) ?? "file", forHTTPHeaderField: "X-Filename")
        return request
    }

    /// What URLs never escape (RFC 3986), as JavaScript's `encodeURIComponent` leaves them.
    private static let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    /// A file in a chat, as its bytes.
    public func attachment(id: String) async throws -> Data {
        try await raw(request("GET", "/api/attachments/\(Self.pathPart(id))", accept: "*/*")) { data, response in
            guard response.statusCode == 200 else { throw error(response.statusCode, data) }
            return data
        }
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
        // Every second while the user watches: no span of its own.
        try await raw(request("GET", "/api/runs/\(run)/screen"), quiet: true) { data, response in
            switch response.statusCode {
            case 200: return data
            case 404: return nil
            default: throw error(response.statusCode, data)
            }
        }
    }

    /// The run's live updates. Ends when the run finishes or the server closes the stream (every few minutes, to
    /// check the session again); the caller reconnects while the run is active, `attempt` counting the reconnections.
    /// Its span lasts as long as the stream, with each status the run goes through as an event.
    public func events(run: String, attempt: Int = 0) -> AsyncThrowingStream<RunEvent, Error> {
        // The server sends a keep-alive every 0.75 s, so 15 s of silence is a dead connection (sleep, a Wi-Fi change).
        var request = request("GET", "/api/runs/\(run)/events", accept: "text/event-stream", timeout: 15)
        let span = telemetry.request(&request)
        span.set("sammy.run_id", run)
        span.set("sammy.stream.attempt", attempt)
        let session = session
        let traced = request
        return AsyncThrowingStream { continuation in
            let task = Task {
                var statuses = 0, previews = 0
                func end(_ how: String, _ error: Error? = nil) {
                    span.set("sammy.stream.end", how)
                    span.set("sammy.stream.statuses", statuses)
                    span.set("sammy.stream.previews", previews)
                    if let error { span.fail(error) }
                    span.end()
                }
                do {
                    let (bytes, response) = try await session.bytes(for: traced)
                    guard let http = response as? HTTPURLResponse else { throw APIError.unexpected("no HTTP response") }
                    span.response(http.statusCode)
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
                            switch decoded {
                            case .status(let run):
                                statuses += 1
                                span.event("run status", ["sammy.run.status": .string(run.status.rawValue),
                                                          "sammy.ask.kind": run.ask.map { .string($0.kind.rawValue) }])
                            case .preview: previews += 1
                            }
                            continuation.yield(decoded)
                        }
                        line.removeAll(keepingCapacity: true)
                    }
                    end(Task.isCancelled ? "cancelled" : "closed")
                    continuation.finish()
                } catch let error as URLError where error.code == .cancelled {
                    end("cancelled")
                    continuation.finish()
                } catch let error as URLError {
                    let lost = APIError.offline(error.localizedDescription)
                    end("lost", lost)
                    continuation.finish(throwing: lost)
                } catch {
                    end("failed", error)
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    // MARK: schedules, memories, sign-ins

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

    // MARK: integrations

    public func integrations() async throws -> Integrations { try await send("GET", "/api/integrations") }

    public func apps() async throws -> [CatalogApp] { try await send("GET", "/api/integrations/apps") }

    /// Where the user signs in to the app, in their browser. The server hears when they have.
    public func connect(app slug: String) async throws -> URL {
        let link: SignInLink = try await send("POST", "/api/integrations/apps/\(Self.pathPart(slug))/connect", body: [String: String]())
        return try Self.url(link.url)
    }

    public func disconnect(app account: String) async throws {
        let _: Ok = try await send("DELETE", "/api/integrations/apps/accounts/\(Self.pathPart(account))")
    }

    /// Adds an MCP server; one with an OAuth sign-in comes back with where to sign in.
    public func addServer(name: String, url: String, headers: [String: String]) async throws -> AddedServer {
        struct Body: Encodable { let name: String, url: String, headers: [String: String] }
        return try await send("POST", "/api/integrations/servers", body: Body(name: name, url: url, headers: headers))
    }

    public func signIn(server: String) async throws -> URL {
        let link: SignInLink = try await send("POST", "/api/integrations/servers/\(server)/sign-in", body: [String: String]())
        return try Self.url(link.url)
    }

    public func remove(server: String) async throws {
        let _: Ok = try await send("DELETE", "/api/integrations/servers/\(server)")
    }

    private static func pathPart(_ text: String) -> String {
        text.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed.subtracting(["/"])) ?? text
    }

    /// A sign-in address, only if it is http or https: anything else (`file:`, an app's scheme) is refused.
    static func url(_ text: String) throws -> URL {
        guard let url = URL(string: text), ["https", "http"].contains(url.scheme?.lowercased() ?? "") else {
            throw APIError.unexpected("not a sign-in address")
        }
        return url
    }

    // MARK: the live view

    /// The WebSocket request of a live-view link, with the session cookie. No `Origin`: the server accepts clients
    /// that are not browsers without one. `trace` is the takeover's span, for the server's spans to join.
    public func liveSocketRequest(_ link: LiveLink, trace: SpanContext? = Telemetry.parent) -> URLRequest? {
        socketRequest(path: link.url.hasSuffix("/") ? link.url + "ws" : link.url + "/ws", trace: trace)
    }

    /// The Mac tunnel's WebSocket request (`MacTunnel`), with the session cookie. No `Origin`, which the server
    /// requires: a web page can't open the tunnel. It says where this Mac is (time zone and language), so the browser
    /// going out through it has a clock and language that agree with the address sites see.
    public func tunnelSocketRequest() -> URLRequest? {
        guard var request = socketRequest(path: "/api/tunnel", trace: nil) else { return nil }
        request.setValue(TimeZone.current.identifier, forHTTPHeaderField: "X-Sammy-Timezone")
        if let language = Locale.preferredLanguages.first {
            request.setValue(language, forHTTPHeaderField: "X-Sammy-Locale")
        }
        return request
    }

    private func socketRequest(path: String, trace: SpanContext?) -> URLRequest? {
        guard var parts = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else { return nil }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        parts.path = path
        guard let url = parts.url else { return nil }
        var request = URLRequest(url: url)
        for (name, value) in HTTPCookie.requestHeaderFields(with: cookies.cookies(for: baseURL) ?? []) {
            request.setValue(value, forHTTPHeaderField: name)
        }
        request.setValue(siteLogin, forHTTPHeaderField: "Authorization")
        telemetry.inject(trace, into: &request)
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

    private func decode<T: Decodable>(_ request: URLRequest, quiet: Bool = false) async throws -> T {
        try await raw(request, quiet: quiet) { data, response in
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
    }

    /// Sends `request` and reads its answer with `read`, in a client span (none when `quiet`) whose trace the
    /// request carries.
    private func raw<T>(_ request: URLRequest, quiet: Bool = false, _ read: (Data, HTTPURLResponse) throws -> T) async throws -> T {
        var request = request
        let span = Telemetry.$quiet.withValue(quiet || Telemetry.quiet) { telemetry.request(&request) }
        defer { span.end() }
        do {
            let data: Data
            let response: URLResponse
            do {
                (data, response) = try await session.data(for: request)
            } catch let error as URLError {
                if error.code == .cancelled { throw CancellationError() }
                throw APIError.offline(error.localizedDescription)
            }
            guard let http = response as? HTTPURLResponse else { throw APIError.unexpected("no HTTP response") }
            span.response(http.statusCode)
            if let realm = Self.basicRealm(http) { throw APIError.siteLogin(realm: realm) }
            return try read(data, http)
        } catch {
            span.fail(error)
            throw error
        }
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
        struct ErrorBody: Decodable { let detail: String? }
        let detail = (try? JSONDecoder().decode(ErrorBody.self, from: data))?.detail
        if status == 401, !signingIn { return .signedOut }
        // A validation error's detail is a list of problems; show the server's text only when it is a sentence.
        return .server(status: status, detail: detail)
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
