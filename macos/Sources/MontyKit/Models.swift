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

    public init(id: String, title: String, status: RunStatus? = nil, outcome: RunStatus? = nil) {
        self.id = id
        self.title = title
        self.status = status
        self.outcome = outcome
    }
}

public struct ChatMessage: Codable, Equatable, Sendable {
    public enum Role: String, Codable, Sendable { case user, assistant }
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
    public let output: String?
    /// What the bot did so far, oldest first, in words: "Opening example.com".
    public let activity: [String]
    /// What the bot waits for, while `status` is `waiting`.
    public let ask: Ask?

    enum CodingKeys: String, CodingKey {
        case id, status, output, activity, ask
        case threadId = "thread_id"
    }

    public init(id: String, threadId: String, status: RunStatus, output: String? = nil, activity: [String] = [], ask: Ask? = nil) {
        self.id = id
        self.threadId = threadId
        self.status = status
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
