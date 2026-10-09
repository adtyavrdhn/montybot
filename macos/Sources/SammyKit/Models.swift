import Foundation

// The server's JSON, as `sammy/api.py` writes it.

public struct User: Codable, Equatable, Sendable {
    public let id: String
    public let email: String
    public let name: String
}

public enum RunStatus: String, Codable, Sendable {
    case queued, running, waiting, done, failed, stopped

    /// Not finished: the thread takes no new message until it is.
    public var isActive: Bool { self == .queued || self == .running || self == .waiting }
    /// The bot is working, as opposed to waiting for the user or finished.
    public var isWorking: Bool { self == .queued || self == .running }
}

public struct ThreadSummary: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let title: String
    /// The status of the thread's unfinished run (queued, running or waiting); nil when nothing is going on.
    public let status: RunStatus?
    /// How the latest run ended (done, failed or stopped), when nothing is going on.
    public let outcome: RunStatus?
    /// When something last happened in the chat (its latest task started); older servers don't say.
    public let updatedAt: Date?
    /// What a waiting chat waits for: a question, an approval or a sign-in; older servers don't say.
    public let waitingFor: AskKind?

    enum CodingKeys: String, CodingKey {
        case id, title, status, outcome
        case updatedAt = "updated_at"
        case waitingFor = "waiting_for"
    }

    public init(
        id: String, title: String, status: RunStatus? = nil, outcome: RunStatus? = nil, updatedAt: Date? = nil,
        waitingFor: AskKind? = nil
    ) {
        self.id = id
        self.title = title
        self.status = status
        self.outcome = outcome
        self.updatedAt = updatedAt
        self.waitingFor = waitingFor
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        title = try container.decode(String.self, forKey: .title)
        status = try container.decodeIfPresent(RunStatus.self, forKey: .status)
        outcome = try container.decodeIfPresent(RunStatus.self, forKey: .outcome)
        updatedAt = try container.decodeIfPresent(String.self, forKey: .updatedAt).flatMap(Self.date)
        // A kind not known yet is left unsaid.
        waitingFor = (try? container.decodeIfPresent(AskKind.self, forKey: .waitingFor)).flatMap { $0 == .other ? nil : $0 }
    }

    /// Python's `isoformat()`: "2026-10-07T15:58:18.123456+00:00", the fraction only when there is one.
    static func date(_ text: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return withFraction.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    /// The same chat with another status, outcome or title, keeping when it was last active.
    func with(title: String? = nil, status: RunStatus?, outcome: RunStatus?, waitingFor: AskKind? = nil) -> ThreadSummary {
        ThreadSummary(
            id: id, title: title ?? self.title, status: status, outcome: outcome, updatedAt: updatedAt,
            waitingFor: status == .waiting ? waitingFor ?? self.waitingFor : nil
        )
    }
}

/// A length of time as a person says it at a glance: "12s", "1m 3s", "2h 5m".
public func spoken(_ seconds: TimeInterval) -> String {
    let total = max(0, Int(seconds.rounded(.down)))
    if total < 60 { return "\(total)s" }
    if total < 3600 { return "\(total / 60)m \(total % 60)s" }
    return "\(total / 3600)h \(total % 3600 / 60)m"
}

/// A chat list grouped by when each chat was last active, newest first, as ChatGPT's sidebar is.
public enum ChatAge: String, CaseIterable, Sendable {
    case today = "Today", yesterday = "Yesterday", week = "Previous 7 days", month = "Previous 30 days", earlier = "Earlier"

    public static func of(_ date: Date?, now: Date = Date(), calendar: Calendar = .current) -> ChatAge {
        guard let date else { return .today }  // just made here, before the server said when
        if calendar.isDate(date, inSameDayAs: now) || date > now { return .today }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: yesterday) {
            return .yesterday
        }
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 0
        return days <= 7 ? .week : days <= 30 ? .month : .earlier
    }
}

public struct ChatMessage: Codable, Equatable, Sendable {
    /// `event` is a line about what happened rather than something said: "You approved: …", "You took over the browser".
    public enum Role: String, Codable, Sendable {
        case user, assistant, event

        /// A role this app doesn't know yet shows as an event, rather than making the whole chat unreadable.
        public init(from decoder: Decoder) throws {
            self = Role(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .event
        }
    }
    public let role: Role
    /// May be empty for a message of files only.
    public let text: String
    /// The files the user attached, or (on a reply) the ones Sammy shared; older servers send none.
    public let files: [Attachment]

    enum CodingKeys: String, CodingKey {
        case role, text, files
    }

    public init(role: Role, text: String, files: [Attachment] = []) {
        self.role = role
        self.text = text
        self.files = files
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        role = try container.decode(Role.self, forKey: .role)
        text = try container.decode(String.self, forKey: .text)
        files = try container.decodeIfPresent([Attachment].self, forKey: .files) ?? []
    }
}

/// A file in a chat: one the user attached (the upload's answer), or one on a message.
public struct Attachment: Codable, Equatable, Hashable, Identifiable, Sendable {
    /// How Sammy reads it: it sees images and PDFs, reads text, and opens anything else with its code tools.
    public enum Kind: String, Codable, Sendable {
        case image, pdf, text, file

        /// A kind this app doesn't know yet is just a file.
        public init(from decoder: Decoder) throws {
            self = Kind(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .file
        }
    }

    public let id: String
    public let name: String
    public let mediaType: String
    public let size: Int
    public let kind: Kind?

    enum CodingKeys: String, CodingKey {
        case id, name, size, kind
        case mediaType = "media_type"
    }

    public init(id: String, name: String, mediaType: String, size: Int, kind: Kind? = nil) {
        self.id = id
        self.name = name
        self.mediaType = mediaType
        self.size = size
        self.kind = kind
    }

    /// The images the server serves as they are, which can be shown as a picture: by its type, and by its bytes when
    /// the server says (a `.png` that isn't one is a `file`).
    public var isImage: Bool { (kind ?? .image) == .image && Self.pictures.contains(mediaType.lowercased()) }
    public var isPDF: Bool { mediaType.lowercased() == "application/pdf" }

    static let pictures: Set<String> = ["image/png", "image/jpeg", "image/gif", "image/webp"]
}

public enum AskKind: String, Codable, Sendable {
    /// `connect`: Sammy needs an app or MCP server connected (`Ask.integration` says which). `other`: a kind this app
    /// doesn't know yet, shown as a card that sends the user to the web app rather than making the chat unreadable.
    case question, approval, handoff, connect, other

    public init(from decoder: Decoder) throws {
        self = AskKind(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .other
    }
}

public struct Ask: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let kind: AskKind
    public let prompt: String
    /// For a `connect` ask, what to connect.
    public let integration: Offer?

    public init(id: String, kind: AskKind, prompt: String, integration: Offer? = nil) {
        self.id = id
        self.kind = kind
        self.prompt = prompt
        self.integration = integration
    }
}

// MARK: integrations: apps through Composio, and the user's own MCP servers

/// What a chat asks the user to connect: an app (`composio`, `key` its slug), their own server to sign in to again
/// (`mcp` with `serverId`), a known MCP server to add (`mcp` with `url`, such as PostHog's), or (`mcp` with neither)
/// an MCP server for a service no app is offered for.
public struct Offer: Codable, Equatable, Sendable {
    public let provider: String
    public let key: String
    public let name: String
    public let logo: String
    public let serverId: String?
    /// The address of a known MCP server to add; older servers don't send it.
    public let url: String?
    /// How a known MCP server signs the user in: `oauth` (in the browser) or `token` (one they paste); older servers
    /// don't send it.
    public let auth: String?
    /// What token to paste ("A GitHub personal access token"), and the header it goes in as `Bearer <token>`.
    public let tokenHint: String?
    public let tokenHeader: String?

    enum CodingKeys: String, CodingKey {
        case provider, key, name, logo, url, auth
        case serverId = "server_id"
        case tokenHint = "token_hint"
        case tokenHeader = "token_header"
    }

    public init(
        provider: String, key: String, name: String, logo: String = "", serverId: String? = nil, url: String? = nil,
        auth: String? = nil, tokenHint: String? = nil, tokenHeader: String? = nil
    ) {
        self.provider = provider
        self.key = key
        self.name = name
        self.logo = logo
        self.serverId = serverId
        self.url = url
        self.auth = auth
        self.tokenHint = tokenHint
        self.tokenHeader = tokenHeader
    }

    public var isApp: Bool { provider == "composio" }
    /// A known MCP server Sammy adds in one click, rather than one the user adds by hand.
    public var isPreset: Bool { provider == "mcp" && serverId == nil && url != nil }
    /// Whether adding this known server takes a token the user pastes, rather than a sign-in in the browser.
    public var needsToken: Bool { auth == "token" }
}

/// One of the user's connections.
public struct Connection: Codable, Equatable, Identifiable, Sendable {
    /// What removing it names: Composio's account id, or the server's id.
    public let id: String
    /// What Sammy calls it: "linear", "mcp:notes".
    public let key: String
    /// `composio` or `mcp`.
    public let provider: String
    public let name: String
    /// The app's description, or the server's host.
    public let detail: String
    public let logo: String
    /// `connected`, `needs_sign_in` or `broken`.
    public let state: String

    public var isApp: Bool { provider == "composio" }
    public var isConnected: Bool { state == "connected" }
}

public struct Integrations: Codable, Equatable, Sendable {
    /// Whether this server connects apps at all (it has a Composio key).
    public let appsAvailable: Bool
    public let connections: [Connection]

    enum CodingKeys: String, CodingKey {
        case connections
        case appsAvailable = "apps_available"
    }

    public init(appsAvailable: Bool, connections: [Connection]) {
        self.appsAvailable = appsAvailable
        self.connections = connections
    }
}

/// An integration the user can connect in one click: an app through Composio, or a known MCP server (`mcp`, with its
/// `url` and `host`). Featured ones have a `kind` (chat, code, issues…) and come first, in the order to show them.
/// Older servers send only the slug, name, logo, description and categories: all apps, none featured.
public struct CatalogApp: Codable, Equatable, Identifiable, Sendable {
    public let key: String
    public let slug: String
    public let name: String
    public let logo: String
    public let description: String
    public let categories: [String]
    public let kind: String?
    /// The kind as a heading says it: "Chat", "Issues & projects".
    public let kindLabel: String?
    public let featured: Bool
    /// `composio` or `mcp`.
    public let provider: String
    /// A known MCP server's address and host (its connection's `detail`); nil for an app.
    public let url: String?
    public let host: String?
    /// `oauth` or `token` for a known MCP server (nil for an app), and for `token`, what to paste and the header it
    /// goes in as `Bearer <token>`.
    public let auth: String?
    public let tokenHint: String?
    public let tokenHeader: String?
    /// Keys repeat across providers (Linear's own server, and Linear through Composio), so the provider is in it.
    public var id: String { "\(provider):\(key)" }

    enum CodingKeys: String, CodingKey {
        case key, slug, name, logo, description, categories, kind, featured, provider, url, host, auth
        case kindLabel = "kind_label"
        case tokenHint = "token_hint"
        case tokenHeader = "token_header"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let slug = try container.decodeIfPresent(String.self, forKey: .slug)
        key = try container.decodeIfPresent(String.self, forKey: .key) ?? slug ?? container.decode(String.self, forKey: .slug)
        self.slug = slug ?? key
        name = try container.decode(String.self, forKey: .name)
        logo = try container.decodeIfPresent(String.self, forKey: .logo) ?? ""
        description = try container.decodeIfPresent(String.self, forKey: .description) ?? ""
        categories = try container.decodeIfPresent([String].self, forKey: .categories) ?? []
        kind = try container.decodeIfPresent(String.self, forKey: .kind)
        kindLabel = try container.decodeIfPresent(String.self, forKey: .kindLabel)
        featured = try container.decodeIfPresent(Bool.self, forKey: .featured) ?? false
        provider = try container.decodeIfPresent(String.self, forKey: .provider) ?? "composio"
        url = try container.decodeIfPresent(String.self, forKey: .url)
        host = try container.decodeIfPresent(String.self, forKey: .host)
        auth = try container.decodeIfPresent(String.self, forKey: .auth)
        tokenHint = try container.decodeIfPresent(String.self, forKey: .tokenHint)
        tokenHeader = try container.decodeIfPresent(String.self, forKey: .tokenHeader)
    }

    public var isApp: Bool { provider == "composio" }
    /// Whether connecting takes a token the user pastes, rather than a sign-in in the browser.
    public var needsToken: Bool { auth == "token" }

    /// Whether `connection` is this one: the same app, or a server at this preset's host.
    public func matches(_ connection: Connection) -> Bool {
        isApp ? connection.isApp && connection.key == key : !connection.isApp && host != nil && connection.detail == host
    }

    /// Whether `query` is in its name, key, description, kind or categories.
    public func matches(_ query: String) -> Bool {
        let query = query.trimmingCharacters(in: .whitespaces)
        return query.isEmpty
            || ([name, key, description, kindLabel ?? ""] + categories).contains { $0.localizedCaseInsensitiveContains(query) }
    }
}

public struct SignInLink: Codable, Equatable, Sendable {
    public let url: String
}

public struct AddedServer: Codable, Equatable, Sendable {
    public let connection: Connection
    /// Where the user signs in, for a server with an OAuth sign-in; nil when it is ready.
    public let signInUrl: String?

    enum CodingKeys: String, CodingKey {
        case connection
        case signInUrl = "sign_in_url"
    }
}

public struct Run: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let threadId: String
    public let status: RunStatus
    /// What the user asked for (or the schedule's task), to try again as it was. Older servers don't send it.
    public let prompt: String?
    public let output: String?
    /// What the bot did so far, oldest first, in words: "Opening example.com".
    public let activity: [String]
    /// What the bot waits for, while `status` is `waiting`.
    public let ask: Ask?
    /// When it started, and finished (ISO 8601, as the server says them); older servers don't say.
    let startedAt: String?
    let completedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, status, prompt, output, activity, ask
        case threadId = "thread_id"
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    public var started: Date? { startedAt.flatMap(ThreadSummary.date) }
    public var completed: Date? { completedAt.flatMap(ThreadSummary.date) }

    public init(
        id: String, threadId: String, status: RunStatus, prompt: String? = nil, output: String? = nil, activity: [String] = [],
        ask: Ask? = nil, started: Date? = nil
    ) {
        startedAt = started.map { ISO8601DateFormatter().string(from: $0) }
        completedAt = nil
        self.id = id
        self.threadId = threadId
        self.status = status
        self.prompt = prompt
        self.output = output
        self.activity = activity
        self.ask = ask
    }
}

public struct ThreadDetail: Codable, Equatable, Sendable {
    public let id: String
    public let title: String
    public let messages: [ChatMessage]
    public let run: Run?
    /// What Sammy did for each earlier reply (the latest run's steps are in `run`); older servers don't say.
    public let steps: [PastSteps]?
}

/// The steps of an earlier run, shown folded under its reply: `after` is the reply's position in the messages.
public struct PastSteps: Codable, Equatable, Sendable {
    public let after: Int
    public let activity: [String]
    let startedAt: String?
    let completedAt: String?

    enum CodingKeys: String, CodingKey {
        case after, activity
        case startedAt = "started_at"
        case completedAt = "completed_at"
    }

    public init(after: Int, activity: [String]) {
        self.after = after
        self.activity = activity
        startedAt = nil
        completedAt = nil
    }

    /// The steps as a person reads them (pages of one site as one step).
    public var steps: [String] { ChatModel.grouped(activity) }

    /// "Worked for 1m 3s · 5 steps", or just the steps when the server doesn't say when.
    public var summary: String {
        let count = "\(steps.count) step\(steps.count == 1 ? "" : "s")"
        guard let started = startedAt.flatMap(ThreadSummary.date), let completed = completedAt.flatMap(ThreadSummary.date)
        else { return count }
        return "Worked for \(spoken(completed.timeIntervalSince(started))) · \(count)"
    }
}

struct SearchResult: Codable, Sendable {
    let ids: [String]
}

public struct Created: Codable, Equatable, Sendable {
    public let threadId: String
    public let runId: String

    enum CodingKeys: String, CodingKey {
        case threadId = "thread_id"
        case runId = "run_id"
    }
}

/// What the bot is writing now: provisional, replaced by the stored reply when the run ends.
public struct Preview: Codable, Equatable, Sendable {
    public let revision: Int
    public let text: String
    public let activity: String

    public init(revision: Int, text: String, activity: String) {
        self.revision = revision
        self.text = text
        self.activity = activity
    }
}

public struct LiveLink: Codable, Equatable, Sendable {
    /// The live view's page, `/live/handoff/<id>`; its WebSocket is at the same path plus `/ws`.
    public let url: String
    public let reason: String
}

public struct Schedule: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let name: String
    /// "Mondays at 09:00 (0 9 * * 1, Europe/London)".
    public let when: String
    public let paused: Bool
    public let watch: Bool
    public let threadId: String
    /// When it runs next (none while paused), when it last ran and how that went; older servers don't say.
    let nextRunAt: String?
    let lastRunAt: String?
    public let lastStatus: RunStatus?

    enum CodingKeys: String, CodingKey {
        case id, name, when, paused, watch
        case threadId = "thread_id"
        case nextRunAt = "next_run_at"
        case lastRunAt = "last_run_at"
        case lastStatus = "last_status"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        name = try container.decode(String.self, forKey: .name)
        when = try container.decode(String.self, forKey: .when)
        paused = try container.decode(Bool.self, forKey: .paused)
        watch = try container.decode(Bool.self, forKey: .watch)
        threadId = try container.decode(String.self, forKey: .threadId)
        nextRunAt = try container.decodeIfPresent(String.self, forKey: .nextRunAt)
        lastRunAt = try container.decodeIfPresent(String.self, forKey: .lastRunAt)
        lastStatus = try? container.decodeIfPresent(RunStatus.self, forKey: .lastStatus)  // a status not known yet: unsaid
    }

    public var nextRun: Date? { nextRunAt.flatMap(ThreadSummary.date) }
    public var lastRun: Date? { lastRunAt.flatMap(ThreadSummary.date) }

    /// The schedule in words, without the cron line and time zone the server appends: "Mondays at 09:00".
    public var plainWhen: String {
        guard when.hasSuffix(")"), let open = when.lastIndex(of: "(") else { return when }
        return when[..<open].trimmingCharacters(in: .whitespaces)
    }

    /// The time zone the server appends, "Europe/London", or nil.
    public var timeZone: String? {
        // "(Europe/London)" from the server now; "(0 9 * * 1, Europe/London)" from older ones.
        guard when.hasSuffix(")"), let open = when.lastIndex(of: "(") else { return nil }
        let inside = when[when.index(after: open)...].dropLast()
        let zone = (inside.split(separator: ",").last ?? inside).trimmingCharacters(in: .whitespaces)
        return zone.isEmpty ? nil : zone
    }
}

public struct Memory: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let text: String
}

public struct SavedSite: Codable, Equatable, Identifiable, Sendable {
    public let site: String
    public var id: String { site }
}

/// What the model has cost the user, in US dollars as genai-prices estimates it: today and this month (in their time
/// zone), the spending limits (none when unset), and the month by chat and by schedule, most first.
public struct Usage: Codable, Equatable, Sendable {
    public struct Spender: Codable, Equatable, Identifiable, Sendable {
        /// None for chats since deleted.
        public let id: String?
        public let name: String?
        public let cost: Double
    }

    public let today: Double
    public let month: Double
    public let dailyCap: Double?
    public let monthlyCap: Double?
    public let threads: [Spender]
    public let schedules: [Spender]

    enum CodingKeys: String, CodingKey {
        case today, month, threads, schedules
        case dailyCap = "daily_cap"
        case monthlyCap = "monthly_cap"
    }
}

extension String {
    /// A chat title as a person reads it: links shown as their site, "Order eggs from shop.example" rather than
    /// "Order eggs from https://shop.example/?ref=…".
    public var readableTitle: String {
        let shortened = replacingOccurrences(of: #"https?://(www\.)?([^/\s?#]+)[^\s]*"#, with: "$2", options: .regularExpression)
        let trimmed = shortened.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "Untitled" : trimmed
    }
}

extension Schedule {
    /// "Next: Mon 12 Oct at 09:00 · Last ran 2 hours ago", in this Mac's time; the last run said plainly if it failed.
    public func times(now: Date = .now) -> (text: String, failed: Bool)? {
        var parts: [String] = []
        if let next = nextRun {
            parts.append("Next: " + next.formatted(.dateTime.weekday(.abbreviated).day().month(.abbreviated).hour().minute()))
        }
        var failed = false
        if let last = lastRun {
            let ago = RelativeDateTimeFormatter().localizedString(for: last, relativeTo: now)
            switch lastStatus {
            case .failed: parts.append("Last run couldn't finish (\(ago))"); failed = true
            case .stopped: parts.append("Last run stopped (\(ago))")
            case .waiting: parts.append("Waiting for you since \(ago)")
            case .queued: parts.append("About to run")
            case .running: parts.append("Running now")
            default: parts.append("Last ran \(ago)")
            }
        }
        return parts.isEmpty ? nil : (parts.joined(separator: " · "), failed)
    }
}
