import Foundation
@preconcurrency import OpenTelemetryApi
import OpenTelemetrySdk
import Testing
@testable import MontyKit

// What the app's telemetry may send (routes, never links or content unless allowed), and that it sends it with the
// trace in each request, through a stand-in server (`Stub`).

@Suite struct RouteTests {
    let thread = "3f2a8c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b"

    @Test func idsBecomeNamedPlaceholders() {
        #expect(Telemetry.route("/api/threads/\(thread)") == "/api/threads/{thread_id}")
        #expect(Telemetry.route("/api/threads/\(thread)/messages") == "/api/threads/{thread_id}/messages")
        #expect(Telemetry.route("/api/runs/\(thread)/live") == "/api/runs/{run_id}/live")
        #expect(Telemetry.route("/api/runs/\(thread)/events") == "/api/runs/{run_id}/events")
        #expect(Telemetry.route("/api/asks/\(thread)") == "/api/asks/{ask_id}")
        #expect(Telemetry.route("/api/schedules/\(thread)/pause") == "/api/schedules/{schedule_id}/pause")
        #expect(Telemetry.route("/api/memories/\(thread)") == "/api/memories/{memory_id}")
        #expect(Telemetry.route("/api/attachments") == "/api/attachments")
        #expect(Telemetry.route("/api/attachments/\(thread)") == "/api/attachments/{attachment_id}")
        #expect(Telemetry.route("/api/sign-ins/shop.example.com") == "/api/sign-ins/{site}")
        #expect(Telemetry.route("/api/sign-ins/a%2Fb/c") == "/api/sign-ins/{site}")
        #expect(Telemetry.route("/api/integrations") == "/api/integrations")
        #expect(Telemetry.route("/api/integrations/apps") == "/api/integrations/apps")
        #expect(Telemetry.route("/api/integrations/apps/linear/connect") == "/api/integrations/apps/{app}/connect")
        #expect(Telemetry.route("/api/integrations/apps/accounts/ca_OmfoGFIzpmEu") == "/api/integrations/apps/accounts/{account_id}")
        #expect(Telemetry.route("/api/integrations/servers/\(thread)/sign-in") == "/api/integrations/servers/{server_id}/sign-in")
    }

    @Test func noQueriesFragmentsOrTokens() {
        #expect(Telemetry.route("/api/threads?cursor=abc#top") == "/api/threads")
        #expect(Telemetry.route(URL(string: "https://monty.test/api/threads/\(thread)?email=a@b.c")!) == "/api/threads/{thread_id}")
        #expect(Telemetry.route("/api/other/\(thread)") == "/api/other/{id}")
        #expect(Telemetry.route("/api/other/abcdef0123456789") == "/api/other/{id}")
        #expect(Telemetry.route("/api/other/12345") == "/api/other/{id}")
        #expect(Telemetry.route("/api/other/invoice.pdf") == "/api/other/{id}")
        #expect(Telemetry.route("/api/password/reset/confirm") == "/api/password/reset/confirm")
        #expect(Telemetry.route("/api/telemetry/v1/traces") == "/api/telemetry/v1/traces")
        #expect(Telemetry.route("/") == "/")
    }

    @Test func theLiveViewIsOnlyEverLive() {
        #expect(Telemetry.route("/live/handoff/Zx9_secret") == "/live/*")
        #expect(Telemetry.route(URL(string: "wss://monty.test/live/handoff/Zx9_secret/ws?t=1")!) == "/live/*")
        #expect(Telemetry.route("/live") == "/live/*")
        #expect(Telemetry.scrub("Open http://monty.test/live/handoff/Zx9_secret/ now") == "Open http://monty.test/live/* now")
    }
}

@Suite struct TelemetryTests {
    @Test func offMakesNoExporterAndNoSpans() {
        #expect(Telemetry.exporter(.off, baseURL: URL(string: "http://x.test")!, session: .shared, siteLogin: nil) == nil)
        #expect(Telemetry.exporter(TelemetrySettings(enabled: true), baseURL: URL(string: "http://x.test")!, session: .shared, siteLogin: nil) != nil)
        let telemetry = Telemetry()
        telemetry.configure(.off, exporter: nil, userId: "u")
        #expect(!telemetry.isEnabled)
        #expect(!telemetry.span("anything").isRecording)
    }

    @Test func contentOnlyWhenTheServerIncludesIt() throws {
        for include in [false, true] {
            let (telemetry, spans) = recording(includeContent: include)
            let span = telemetry.span("send message")
            span.content("monty.message", "Order eggs; see /live/handoff/secret")
            span.fail(APIError.server(status: 409, detail: "a private detail"))
            span.end()
            let data = try #require(spans.all.first)
            #expect(data.attributes["monty.message.length"] == .int(36))
            #expect(data.attributes["monty.message"] == (include ? .string("Order eggs; see /live/*") : nil))
            #expect(data.attributes["error.type"] == .string("server"))
            #expect(data.attributes["enduser.id"] == .string("user-1"))
            #expect("\(data.status)".contains("private detail") == include)
        }
    }

    @Test @MainActor func requestsAreSpansWhoseTraceTheServerGets() async throws {
        let stub = Stub(settings: #"{"enabled": true, "include_content": false, "environment": "test", "version": "abc"}"#)
        let (telemetry, spans) = recording(includeContent: false, into: stub.client.telemetry)
        #expect(telemetry === stub.client.telemetry)
        let threads = try await stub.client.telemetry.action("open chat") { _ in try await stub.client.threads() }
        #expect(threads.isEmpty)
        await #expect(throws: APIError.self) { _ = try await stub.client.thread("3f2a8c1e-5b6d-4e7f-8a9b-0c1d2e3f4a5b") }

        let all = spans.all
        let action = try #require(all.first { $0.name == "open chat" })
        let list = try #require(all.first { $0.name == "GET /api/threads" })
        #expect(list.parentSpanId == action.spanId && list.traceId == action.traceId && list.kind == .client)
        #expect(list.attributes["http.response.status_code"] == .int(200))
        #expect(list.attributes["url.template"] == .string("/api/threads"))
        #expect(list.attributes["server.address"] == .string(stub.host))
        let missing = try #require(all.first { $0.name == "GET /api/threads/{thread_id}" })
        #expect(missing.parentSpanId == nil)
        #expect(missing.attributes["error.type"] == .string("server"))
        #expect(missing.attributes["http.response.status_code"] == .int(404))

        let sent = stub.requests.first { $0.url?.path == "/api/threads" }
        #expect(sent?.value(forHTTPHeaderField: "traceparent") == "00-\(list.traceId.hexString)-\(list.spanId.hexString)-01")
    }

    @Test @MainActor func pollingCarriesTheTraceWithoutASpan() async throws {
        let stub = Stub(settings: "{}")
        let (_, spans) = recording(includeContent: false, into: stub.client.telemetry)
        _ = try await stub.client.telemetry.action("watch") { _ in try await stub.client.screen(run: "r") }
        let all = spans.all
        #expect(all.map(\.name) == ["watch"])
        guard let watch = all.first else { return }
        let sent = stub.requests.first { $0.url?.path == "/api/runs/r/screen" }
        #expect(sent?.value(forHTTPHeaderField: "traceparent")?.contains(watch.spanId.hexString) == true)
    }

    @Test func aServerWithoutTelemetryGetsNone() async throws {
        let stub = Stub(settings: #"{"enabled": false, "include_content": true, "environment": "test", "version": null}"#)
        await stub.client.startTelemetry(userId: "user-1")
        #expect(!stub.client.telemetry.isEnabled)
        _ = try await stub.client.threads()
        await stub.client.telemetry.shutdown()
        #expect(stub.requests.allSatisfy { !($0.url?.path.hasPrefix("/api/telemetry/v1") ?? false) })
        #expect(stub.requests.allSatisfy { $0.value(forHTTPHeaderField: "traceparent") == nil })
    }

    /// (A stand-in protocol gets no cookies, which URLSession adds on the network: the session cookie goes with
    /// the export because it is the API's own session.)
    @Test func spansGoToTheServerAsOTLPWithTheSiteLogin() async throws {
        let stub = Stub(settings: #"{"enabled": true, "include_content": false, "environment": "test", "version": "abc"}"#, siteLogin: "Basic c2l0ZTpsb2dpbg==")
        await stub.client.startTelemetry(userId: "user-1")
        #expect(stub.client.telemetry.isEnabled)
        _ = try await stub.client.threads()
        await stub.client.telemetry.shutdown()  // sends what is waiting
        let export = try #require(stub.requests.first { $0.url?.path == "/api/telemetry/v1/traces" })
        #expect(export.httpMethod == "POST")
        #expect(export.value(forHTTPHeaderField: "Content-Type") == "application/x-protobuf")
        #expect(export.value(forHTTPHeaderField: "Authorization") == "Basic c2l0ZTpsb2dpbg==")
        #expect(stub.bodies[export.url!.path].map { !$0.isEmpty } == true)
        #expect(stub.requests.filter { $0.url?.path == "/api/telemetry" }.count == 1)
    }

    @Test func theResourceNamesTheApp() {
        let resource = Telemetry.resource(TelemetrySettings(enabled: true, environment: "prod", version: "abc123"))
        #expect(resource.attributes["service.name"] == .string("montybot-mac"))
        #expect(resource.attributes["deployment.environment.name"] == .string("prod"))
        #expect(resource.attributes["montybot.server.version"] == .string("abc123"))
        #expect(resource.attributes["os.name"] == .string("macOS"))
        #expect(resource.attributes["device.model.identifier"] != nil)
    }
}

/// Spans as they end.
final class Spans: SpanExporter, @unchecked Sendable {
    private let lock = NSLock()
    private var spans: [SpanData] = []
    weak var telemetry: Telemetry?
    /// What has ended so far (the processor exports on its own queue).
    var all: [SpanData] {
        telemetry?.flushBeforeQuitting()
        return lock.withLock { spans }
    }

    func export(spans: [SpanData], explicitTimeout: TimeInterval?) -> SpanExporterResultCode {
        lock.withLock { self.spans += spans }
        return .success
    }

    func flush(explicitTimeout: TimeInterval?) -> SpanExporterResultCode { .success }
    func shutdown(explicitTimeout: TimeInterval?) {}
}

func recording(includeContent: Bool, into telemetry: Telemetry = Telemetry()) -> (Telemetry, Spans) {
    let spans = Spans()
    spans.telemetry = telemetry
    telemetry.configure(TelemetrySettings(enabled: true, includeContent: includeContent, environment: "test"),
                        exporter: spans, userId: "user-1", batch: false)
    return (telemetry, spans)
}

/// A stand-in montybot, one per test (its own host): `/api/telemetry` says `settings`, `/api/threads` is empty, the
/// telemetry endpoints take anything, the rest is 404. Keeps every request it gets.
final class Stub: @unchecked Sendable {
    let host = "stub-\(UUID().uuidString.prefix(8).lowercased()).test"
    let client: APIClient

    init(settings: String, siteLogin: String? = nil) {
        client = APIClient(baseURL: URL(string: "http://\(host)")!,
                           cookies: HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "monty-stub-\(UUID().uuidString)"),
                           siteLogin: siteLogin, protocols: [StubProtocol.self])
        StubProtocol.register(host, settings: settings)
    }

    var requests: [URLRequest] { StubProtocol.recorded(host).map(\.0) }
    var bodies: [String: Data] {
        Dictionary(StubProtocol.recorded(host).compactMap { request, body in body.map { (request.url!.path, $0) } }) { $1 }
    }
}

final class StubProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) private static var settings: [String: String] = [:]
    nonisolated(unsafe) private static var log: [String: [(URLRequest, Data?)]] = [:]
    private static let lock = NSLock()

    static func register(_ host: String, settings: String) { lock.withLock { self.settings[host] = settings } }
    static func recorded(_ host: String) -> [(URLRequest, Data?)] { lock.withLock { log[host] ?? [] } }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let host = request.url?.host() ?? ""
        let path = request.url?.path ?? ""
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
            stream.close()
            body = data
        }
        let settings = Self.lock.withLock {
            Self.log[host, default: []].append((request, body))
            return Self.settings[host] ?? "{}"
        }
        let (status, answer): (Int, String) = switch path {
        case "/api/telemetry": (200, settings)
        case "/api/threads": (200, "[]")
        case _ where path.hasPrefix("/api/telemetry/v1/"): (200, "")
        default: (404, #"{"detail": "not here"}"#)
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(answer.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// Signing out stops telemetry for good, however slow the server, and quickly.
@Suite struct StoppingTests {
    @Test func aStartBeganBeforeSigningOutDoesNotUndoIt() async {
        let telemetry = Telemetry()
        let before = telemetry.currentGeneration  // `startTelemetry` asks the server...
        await telemetry.shutdown()  // ...the user signs out meanwhile...
        telemetry.configure(TelemetrySettings(enabled: true, environment: "test"), exporter: Spans(), userId: "u",
                            batch: false, generation: before)  // ...and the answer comes
        #expect(!telemetry.isEnabled)
        telemetry.configure(TelemetrySettings(enabled: true, environment: "test"), exporter: Spans(), userId: "u",
                            batch: false, generation: telemetry.currentGeneration)  // signing in again
        #expect(telemetry.isEnabled)
    }

    @Test func waitingForAStuckServerHasALimit() async {
        let started = Date()
        await Telemetry.waiting(atMost: 0.2) { Thread.sleep(forTimeInterval: 3) }
        #expect(Date().timeIntervalSince(started) < 1)
    }
}
