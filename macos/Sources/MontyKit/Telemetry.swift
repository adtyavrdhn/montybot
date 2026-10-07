import Foundation
@preconcurrency import OpenTelemetryApi
import OpenTelemetryProtocolExporterCommon  // OtlpConfiguration, part of the HTTP exporter's product
import OpenTelemetryProtocolExporterHttp
import OpenTelemetrySdk

// The app's traces, in the user's Logfire project next to the server's. They go to the server
// (`/api/telemetry/v1/traces`), which forwards them with its own token, so the app holds none; and only when the
// server says so (`GET /api/telemetry`), after sign-in. Forwarded data skips the server's scrubbing, so what may leave
// the Mac is decided here: routes as templates, ids that are random UUIDs, methods, statuses and timings; what the
// user wrote or Monty answered only when the server includes content, and never passwords, cookies, live-view links,
// hand-off ids, file names or bodies.

/// What the server says about telemetry, `GET /api/telemetry`.
public struct TelemetrySettings: Codable, Equatable, Sendable {
    public let enabled: Bool
    /// Whether what the user wrote and Monty answered may be sent; otherwise only lengths and kinds.
    public let includeContent: Bool
    public let environment: String
    /// The server's commit.
    public let version: String?

    public init(enabled: Bool, includeContent: Bool = false, environment: String = "", version: String? = nil) {
        self.enabled = enabled
        self.includeContent = includeContent
        self.environment = environment
        self.version = version
    }

    public static let off = TelemetrySettings(enabled: false)

    enum CodingKeys: String, CodingKey {
        case enabled, environment, version
        case includeContent = "include_content"
    }
}

/// The app's tracer, one per server (`APIClient.telemetry`). Until it is configured, and when the server says no,
/// every span is a no-op and nothing is sent.
public final class Telemetry: @unchecked Sendable {
    /// The span what happens now belongs to: the user's action. OpenTelemetry's own "active span" does not follow
    /// Swift tasks; this does, into child tasks too.
    @TaskLocal public static var parent: SpanContext?
    /// Polling (the chat list, the browser's picture): requests carry the trace on, but make no spans of their own.
    @TaskLocal public static var quiet = false

    private final class Pipeline: @unchecked Sendable {
        let settings: TelemetrySettings
        var processor: SpanProcessor
        let tracer: any Tracer

        init(settings: TelemetrySettings, processor: SpanProcessor, tracer: any Tracer) {
            self.settings = settings
            self.processor = processor
            self.tracer = tracer
        }
    }

    private let lock = NSLock()
    private var pipeline: Pipeline?
    private var userId: String?

    public init() {}

    public var isEnabled: Bool { lock.withLock { pipeline != nil } }
    public var includeContent: Bool { lock.withLock { pipeline?.settings.includeContent ?? false } }

    // MARK: setting up

    /// The exporter for these settings: OTLP over the app's own URLSession, so the session cookie goes with it, and
    /// the site login (`Authorization`) as every request has it. Nil when the server takes no telemetry.
    static func exporter(_ settings: TelemetrySettings, baseURL: URL, session: URLSession, siteLogin: String?) -> SpanExporter? {
        guard settings.enabled else { return nil }
        return OtlpHttpTraceExporter(
            endpoint: baseURL.appending(path: "/api/telemetry/v1/traces"),
            config: OtlpConfiguration(timeout: 10, compression: .gzip, headers: siteLogin.map { [("Authorization", $0)] }),
            httpClient: BaseHTTPClient(session: session),
            envVarHeaders: nil,  // the headers above, not OTEL_EXPORTER_OTLP_HEADERS
            requeueOnFailure: false  // a server that refuses must not make the app hold spans forever
        )
    }

    /// Starts sending with `exporter` (nil: stops). `batch` is off in tests, to see spans as they end.
    func configure(_ settings: TelemetrySettings, exporter: SpanExporter?, userId: String?, batch: Bool = true) {
        let processor: SpanProcessor? = exporter.map {
            batch ? BatchSpanProcessor(spanExporter: $0, scheduleDelay: 5, exportTimeout: 10, maxQueueSize: 2048) as SpanProcessor
                : SimpleSpanProcessor(spanExporter: $0)
        }
        let next = processor.map { processor in
            let provider = TracerProviderBuilder().with(resource: Self.resource(settings)).add(spanProcessor: processor).build()
            return Pipeline(settings: settings, processor: processor,
                            tracer: provider.get(instrumentationName: "montybot-mac", instrumentationVersion: Self.appVersion))
        }
        let previous = lock.withLock {
            let previous = pipeline
            pipeline = next
            self.userId = userId
            return previous
        }
        if let previous { Self.background { previous.processor.shutdown(explicitTimeout: 3) } }
    }

    /// Whether this is already sending with these settings, for this user.
    func isConfigured(_ settings: TelemetrySettings, userId: String?) -> Bool {
        lock.withLock { pipeline?.settings == settings && self.userId == userId }
    }

    /// Stops sending, after sending what is left (a few seconds at most).
    public func shutdown() async {
        let previous = lock.withLock {
            let previous = pipeline
            pipeline = nil
            userId = nil
            return previous
        }
        guard let previous else { return }
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            Self.background {
                previous.processor.shutdown(explicitTimeout: 3)
                done.resume()
            }
        }
    }

    /// Sends what is waiting now (the app went to the background), off the main thread.
    public func flush() {
        guard let pipeline = lock.withLock({ pipeline }) else { return }
        Self.background { pipeline.processor.forceFlush(timeout: 3) }
    }

    /// Sends what is waiting before the app quits, waiting for it a moment.
    public func flushBeforeQuitting() {
        lock.withLock { pipeline }?.processor.forceFlush(timeout: 2)
    }

    private static func background(_ work: @escaping @Sendable () -> Void) {
        DispatchQueue.global(qos: .utility).async(execute: work)
    }

    // MARK: spans

    /// A span, a child of the task's `parent` unless `root`. A no-op when telemetry is off.
    public func span(_ name: String, kind: SpanKind = .internal, root: Bool = false,
                     _ attributes: [String: AttributeValue?] = [:], start: Date? = nil) -> TraceSpan {
        guard let (pipeline, userId) = lock.withLock({ pipeline.map { ($0, userId) } }) else { return .none }
        let builder = pipeline.tracer.spanBuilder(spanName: name).setSpanKind(spanKind: kind)
        if !root, let parent = Self.parent { builder.setParent(parent) } else { builder.setNoParent() }
        if let start { builder.setStartTime(time: start) }
        if let userId { builder.setAttribute(key: "enduser.id", value: userId) }
        for case let (key, value?) in attributes { builder.setAttribute(key: key, value: value) }
        return TraceSpan(builder.startSpan(), tracer: pipeline.tracer, includeContent: pipeline.settings.includeContent)
    }

    /// Something that happened at one moment (or since `start`): a span that ends at once.
    public func log(_ name: String, _ attributes: [String: AttributeValue?] = [:], start: Date? = nil,
                    content: (TraceSpan) -> Void = { _ in }) {
        guard isEnabled else { return }
        let span = span(name, attributes, start: start)
        content(span)
        span.end()
    }

    /// Runs a user's action in its span: what it asks of the server becomes the span's children, and the server's
    /// spans for those requests join the trace.
    @MainActor
    public func action<T>(_ name: String, _ attributes: [String: AttributeValue?] = [:],
                          _ body: (TraceSpan) async throws -> T) async rethrows -> T {
        let span = span(name, attributes)
        guard let context = span.context else { return try await body(span) }
        defer { span.end() }
        do {
            return try await Self.$quiet.withValue(false) {
                try await Self.$parent.withValue(context) { try await body(span) }
            }
        } catch {
            span.fail(error)
            throw error
        }
    }

    /// An error the user was shown, and where.
    public func shown(_ error: APIError, _ message: String? = nil, _ attributes: [String: AttributeValue?] = [:]) {
        log("error shown", attributes.merging(["error.type": .string(error.kind), "http.response.status_code": error.status.map { .int($0) }]) { $1 }) {
            $0.content("monty.error.message", message ?? error.localizedDescription)
        }
    }

    // MARK: requests

    /// A client span for an API request, named for its route ("GET /api/threads/{thread_id}"), with the trace put
    /// in the request's `traceparent` so the server's spans join it. Polling makes no span but still carries the trace.
    func request(_ request: inout URLRequest) -> TraceSpan {
        guard isEnabled else { return .none }
        let method = request.httpMethod ?? "GET"
        let route = request.url.map(Self.route) ?? "/"
        let span = Self.quiet ? .none : span("\(method) \(route)", kind: .client, [
            "http.request.method": .string(method),
            "url.template": .string(route),
            "http.route": .string(route),
            "server.address": request.url?.host().map { .string($0) },
            "server.port": request.url.flatMap { $0.port ?? ($0.scheme == "https" ? 443 : 80) }.map { .int($0) },
        ])
        inject(span.context ?? Self.parent, into: &request)
        return span
    }

    func inject(_ context: SpanContext?, into request: inout URLRequest) {
        guard let context, context.isValid else { return }
        var carrier: [String: String] = [:]
        W3CTraceContextPropagator().inject(spanContext: context, carrier: &carrier, setter: HeaderSetter())
        for (name, value) in carrier { request.setValue(value, forHTTPHeaderField: name) }
    }

    private struct HeaderSetter: Setter {
        func set(carrier: inout [String: String], key: String, value: String) { carrier[key] = value }
    }

    /// A URL's path as a route: no query or fragment, ids as named placeholders, the live view (whose path is its
    /// secret) as `/live/*`, and anything else that looks like a token or a name as `{id}`.
    public static func route(_ url: URL) -> String {
        route(URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath ?? "/")
    }

    public static func route(_ path: String) -> String {
        let path = String(path.prefix { $0 != "?" && $0 != "#" })
        if path == "/live" || path.hasPrefix("/live/") || path.hasPrefix("live/") { return "/live/*" }
        var segments: [String] = []
        var previous = ""
        for segment in path.split(separator: "/", omittingEmptySubsequences: false).map(String.init) {
            if previous == "sign-ins" { segments.append("{site}"); break }  // a site, then nothing that is not one
            if segment.isEmpty {
                segments.append(segment)
            } else if let placeholder = placeholders[previous] {
                segments.append(placeholder)
            } else {
                segments.append(isToken(segment) ? "{id}" : segment)
            }
            previous = segment
        }
        let route = segments.joined(separator: "/")
        return route.isEmpty ? "/" : route
    }

    static let placeholders = [
        "threads": "{thread_id}", "runs": "{run_id}", "asks": "{ask_id}", "schedules": "{schedule_id}",
        "memories": "{memory_id}",
    ]

    /// Not one of the API's own words: a UUID, a number, something long with digits, or anything with a dot, `%` or `@`.
    static func isToken(_ segment: String) -> Bool {
        if segment.contains(where: { ".%@:~+=".contains($0) }) { return true }
        if segment.allSatisfy(\.isNumber) { return true }
        if segment.contains(where: \.isNumber), segment.count >= 12 { return true }
        return segment.firstMatch(of: /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/) != nil
    }

    /// Text that may be sent (content), without live-view links in it: a reply can mention one.
    static func scrub(_ text: String) -> String {
        text.replacing(/\/live\/[^\s"'<>)\]]*/, with: "/live/*")
    }

    // MARK: the resource

    static var appVersion: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "dev" }
    static var appBuild: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "dev" }

    static func resource(_ settings: TelemetrySettings) -> Resource {
        let os = ProcessInfo.processInfo.operatingSystemVersion
        var attributes: [String: AttributeValue] = [
            "service.name": .string("montybot-mac"),
            "service.version": .string(appVersion),
            "app.build": .string(appBuild),
            "deployment.environment.name": .string(settings.environment),
            "os.type": .string("darwin"),
            "os.name": .string("macOS"),
            "os.version": .string("\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"),
            "device.manufacturer": .string("Apple"),
        ]
        if let model = hardwareModel { attributes["device.model.identifier"] = .string(model) }
        if let server = settings.version { attributes["montybot.server.version"] = .string(server) }
        return Resource().merging(other: Resource(attributes: attributes))
    }

    /// "Mac14,2".
    static let hardwareModel: String? = {
        var size = 0
        guard sysctlbyname("hw.model", nil, &size, nil, 0) == 0, size > 0 else { return nil }
        var bytes = [CChar](repeating: 0, count: size)
        guard sysctlbyname("hw.model", &bytes, &size, nil, 0) == 0 else { return nil }
        return String(decoding: bytes.prefix { $0 != 0 }.map { UInt8(bitPattern: $0) }, as: UTF8.self)
    }()
}

/// A span, or nothing when telemetry is off; safe to use either way.
public final class TraceSpan: @unchecked Sendable {
    public static let none = TraceSpan(nil, tracer: nil, includeContent: false)

    private let span: (any Span)?
    private let tracer: (any Tracer)?
    /// Whether `content` sends text, or only its length.
    public let includeContent: Bool

    init(_ span: (any Span)?, tracer: (any Tracer)?, includeContent: Bool) {
        self.span = span
        self.tracer = tracer
        self.includeContent = includeContent
    }

    public var context: SpanContext? { span?.context }
    public var isRecording: Bool { span != nil }

    public func set(_ key: String, _ value: AttributeValue?) {
        guard let span, let value else { return }
        span.setAttribute(key: key, value: value)
    }

    public func set(_ key: String, _ value: String?) { set(key, value.map { .string($0) }) }
    public func set(_ key: String, _ value: Int?) { set(key, value.map { .int($0) }) }
    public func set(_ key: String, _ value: Bool?) { set(key, value.map { .bool($0) }) }

    /// What the user wrote or Monty said: the text only when the server includes content; its length always.
    public func content(_ key: String, _ text: String?) {
        guard let span, let text else { return }
        span.setAttribute(key: key + ".length", value: text.count)
        if includeContent { span.setAttribute(key: key, value: Telemetry.scrub(text)) }
    }

    public func event(_ name: String, _ attributes: [String: AttributeValue?] = [:]) {
        span?.addEvent(name: name, attributes: attributes.compactMapValues { $0 })
    }

    /// A span inside this one, outliving the task that started it (handing back during a takeover).
    public func child(_ name: String, _ attributes: [String: AttributeValue?] = [:]) -> TraceSpan {
        guard let span, let tracer else { return .none }
        let builder = tracer.spanBuilder(spanName: name).setParent(span.context)
        for case let (key, value?) in attributes { builder.setAttribute(key: key, value: value) }
        return TraceSpan(builder.startSpan(), tracer: tracer, includeContent: includeContent)
    }

    /// The span failed with `error`: its kind always, its message only with content. Cancelling is not failing.
    public func fail(_ error: Error) {
        guard let span else { return }
        if error is CancellationError { span.setAttribute(key: "monty.cancelled", value: true); return }
        let kind = (error as? APIError)?.kind ?? String(describing: type(of: error))
        span.setAttribute(key: "error.type", value: kind)
        if let status = (error as? APIError)?.status { span.setAttribute(key: "http.response.status_code", value: status) }
        span.status = .error(description: includeContent ? Telemetry.scrub(error.localizedDescription) : kind)
    }

    /// Marks an HTTP answer: its status, and an error from 400 up.
    func response(_ status: Int) {
        guard let span else { return }
        span.setAttribute(key: "http.response.status_code", value: status)
        if status >= 400 {
            span.setAttribute(key: "error.type", value: String(status))
            span.status = .error(description: String(status))
        }
    }

    public func end() { span?.end() }
}

extension APIError {
    /// What went wrong, in one word, for telemetry: never the server's text.
    public var kind: String {
        switch self {
        case .signedOut: return "signed_out"
        case .server: return "server"
        case .siteLogin: return "site_login"
        case .offline: return "offline"
        case .unexpected: return "unexpected"
        }
    }
}
