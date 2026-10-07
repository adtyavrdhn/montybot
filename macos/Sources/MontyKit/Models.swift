import Foundation

// The server's JSON, as `montybot/api.py` writes it.

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

    enum CodingKeys: String, CodingKey {
        case id, title, status, outcome
        case updatedAt = "updated_at"
    }

    public init(id: String, title: String, status: RunStatus? = nil, outcome: RunStatus? = nil, updatedAt: Date? = nil) {
        self.id = id
        self.title = title
        self.status = status
        self.outcome = outcome
        self.updatedAt = updatedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        title = try container.decode(String.self, forKey: .title)
        status = try container.decodeIfPresent(RunStatus.self, forKey: .status)
        outcome = try container.decodeIfPresent(RunStatus.self, forKey: .outcome)
        updatedAt = try container.decodeIfPresent(String.self, forKey: .updatedAt).flatMap(Self.date)
    }

    /// Python's `isoformat()`: "2026-10-07T15:58:18.123456+00:00", the fraction only when there is one.
    static func date(_ text: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return withFraction.date(from: text) ?? ISO8601DateFormatter().date(from: text)
    }

    /// The same chat with another status, outcome or title, keeping when it was last active.
    func with(title: String? = nil, status: RunStatus?, outcome: RunStatus?) -> ThreadSummary {
        ThreadSummary(id: id, title: title ?? self.title, status: status, outcome: outcome, updatedAt: updatedAt)
    }
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
    public let text: String

    public init(role: Role, text: String) {
        self.role = role
        self.text = text
    }
}

public enum AskKind: String, Codable, Sendable { case question, approval, handoff }

public struct Ask: Codable, Equatable, Identifiable, Sendable {
    public let id: String
    public let kind: AskKind
    public let prompt: String

    public init(id: String, kind: AskKind, prompt: String) {
        self.id = id
        self.kind = kind
        self.prompt = prompt
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

    enum CodingKeys: String, CodingKey {
        case id, status, prompt, output, activity, ask
        case threadId = "thread_id"
    }

    public init(
        id: String, threadId: String, status: RunStatus, prompt: String? = nil, output: String? = nil, activity: [String] = [],
        ask: Ask? = nil
    ) {
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

    enum CodingKeys: String, CodingKey {
        case id, name, when, paused, watch
        case threadId = "thread_id"
    }

    /// The schedule in words, without the cron line and time zone the server appends: "Mondays at 09:00".
    public var plainWhen: String {
        guard when.hasSuffix(")"), let open = when.lastIndex(of: "(") else { return when }
        return when[..<open].trimmingCharacters(in: .whitespaces)
    }

    /// The time zone the server appends, "Europe/London", or nil.
    public var timeZone: String? {
        guard when.hasSuffix(")"), let comma = when.lastIndex(of: ",") else { return nil }
        let zone = when[when.index(after: comma)...].dropLast().trimmingCharacters(in: .whitespaces)
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

public struct WorkspaceFile: Codable, Equatable, Identifiable, Sendable {
    /// Relative to the user's workspace: "downloads/invoice-2024-05.pdf".
    public let path: String
    public let size: Int
    public var id: String { path }
    public var name: String { (path as NSString).lastPathComponent }
    public var folder: String { (path as NSString).deletingLastPathComponent }
    /// The folder as the user knows it: inside their workspace, not the sandbox's `/work`.
    public var shownFolder: String {
        var folder = folder
        for prefix in ["/work/", "/work"] where folder.hasPrefix(prefix) { folder.removeFirst(prefix.count) }
        return folder.hasPrefix("/") ? String(folder.dropFirst()) : folder
    }
}

public struct FileList: Codable, Equatable, Sendable {
    public let files: [WorkspaceFile]
    public let truncated: Bool
    public let maxDownloadBytes: Int

    enum CodingKeys: String, CodingKey {
        case files, truncated
        case maxDownloadBytes = "max_download_bytes"
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
