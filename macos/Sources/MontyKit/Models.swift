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
        waitingFor = try? container.decodeIfPresent(AskKind.self, forKey: .waitingFor)  // a kind not known yet: unsaid
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
    /// What Monty did for each earlier reply (the latest run's steps are in `run`); older servers don't say.
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
            case .queued, .running: parts.append("Running now")
            default: parts.append("Last ran \(ago)")
            }
        }
        return parts.isEmpty ? nil : (parts.joined(separator: " · "), failed)
    }
}
