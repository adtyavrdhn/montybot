import Foundation
import Testing
@testable import SammyKit

@MainActor
struct ModelPreferencesTests {
    @Test func switchesModelsThinkingAndAdvancedBeforeTheNextMessage() async throws {
        let server = PreferenceServer()
        let client = server.api
        let selection = client.modelSelection
        await selection.load(using: client, user: "u")
        #expect(selection.preferences?.model == "claude")
        #expect(selection.value(for: "thinking") == "medium")
        #expect(selection.preferences?.selected?.simpleThinking == ["low", "medium", "high"])

        selection.selectOption("thinking", value: "high", using: client)
        #expect(selection.saving)
        // No explicit wait here: message creation must wait for the PUT itself.
        _ = try await client.startThread("first")
        #expect(server.runs.last?.settings["thinking"] == .string("high"))
        #expect(!selection.saving)

        selection.selectOption("verbosity", value: "low", using: client)
        try await selection.waitForSave()
        selection.selectModel("gpt", using: client)
        _ = try await client.send("second", to: "t")
        #expect(server.runs.last?.model == "gpt")
        #expect(server.runs.last?.settings == [:])
        #expect(selection.value(for: "thinking") == "low")
        #expect(selection.preferences?.selected?.options["summary"] == ["auto", "detailed"])
        selection.selectOption("thinking", value: "medium", using: client)
        try await selection.waitForSave()
        selection.selectOption("summary", value: "detailed", using: client)
        _ = try await client.send("third", to: "t")
        #expect(server.runs.last?.settings == ["thinking": .string("medium"), "summary": .string("detailed")])

        let reloaded = ModelPreferencesModel()
        await reloaded.load(using: client, user: "u")
        #expect(reloaded.preferences == selection.preferences)
        let puts = server.requests.filter { $0.httpMethod == "PUT" }
        #expect(puts.count == 5)
        #expect(puts.allSatisfy { $0.url?.path == "/api/model-preferences" })
        #expect(puts.allSatisfy { $0.value(forHTTPHeaderField: "Content-Type") == "application/json" })
        #expect(puts.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Basic test" })
        #expect(server.updates[2] == ModelPreferencesUpdate(model: "gpt", settings: [:]))
    }

    @Test func unsupportedChoicesNeverGenerateRequests() async throws {
        let server = PreferenceServer()
        let selection = server.api.modelSelection
        await selection.load(using: server.api, user: "u")
        selection.selectModel("not-allowed", using: server.api)
        selection.selectOption("thinking", value: "xhigh", using: server.api)
        selection.selectOption("summary", value: "auto", using: server.api)
        #expect(!selection.saving)
        #expect(server.updates.isEmpty)
        selection.selectModel("plain", using: server.api)
        try await selection.waitForSave()
        #expect(selection.preferences?.selected?.simpleThinking.isEmpty == true)
        #expect(selection.preferences?.selected?.options.isEmpty == true)
        selection.selectOption("thinking", value: "low", using: server.api)
        #expect(!selection.saving)
    }

    @Test func failedSaveKeepsConfirmedChoiceAndBlocksMessagesUntilReload() async throws {
        let server = PreferenceServer()
        let selection = server.api.modelSelection
        await selection.load(using: server.api, user: "u")
        server.status = 422
        selection.selectModel("gpt", using: server.api)
        await #expect(throws: APIError.self) { _ = try await server.api.startThread("must not start") }
        #expect(selection.preferences?.model == "claude")
        #expect(selection.error?.contains("Couldn't save") == true)
        #expect(!selection.saving)
        #expect(server.runs.isEmpty)
        server.status = 200
        await selection.load(using: server.api, user: "u")
        #expect(selection.error == nil)
        _ = try await server.api.startThread("confirmed choice")
        #expect(server.runs.last?.model == "claude")
    }

    @Test func loadErrorsCanRetryAndResetClearsAccountState() async throws {
        let server = PreferenceServer()
        let selection = server.api.modelSelection
        server.status = 503
        await selection.load(using: server.api, user: "u")
        #expect(selection.preferences == nil)
        #expect(selection.error != nil)
        #expect(!selection.loading)
        server.status = 200
        await selection.load(using: server.api, user: "u")
        #expect(selection.preferences != nil)
        selection.reset()
        #expect(selection.preferences == nil)
        #expect(selection.error == nil)
    }

    @Test func typedSettingsRoundTripWithoutLosingServerDefaults() throws {
        let data = Data(#"{"cache":true,"max_tokens":8192,"temperature":0.2,"nested":{"value":null},"list":["a",1]}"#.utf8)
        let decoded = try JSONDecoder().decode([String: ModelSettingValue].self, from: data)
        #expect(decoded["max_tokens"] == .number(8192))
        #expect(decoded["cache"] == .bool(true))
        let encoded = try JSONEncoder().encode(decoded)
        #expect(try JSONDecoder().decode([String: ModelSettingValue].self, from: encoded) == decoded)
    }
}

/// An offline server that snapshots its saved preference when a message arrives.
private final class PreferenceServer: @unchecked Sendable {
    let host = "preferences-\(UUID().uuidString).test"
    private let lock = NSLock()
    private var saved = ModelPreferencesUpdate(model: "claude", settings: [:])
    private var recordedRuns: [ModelPreferencesUpdate] = []
    private var recordedUpdates: [ModelPreferencesUpdate] = []
    private var recordedRequests: [URLRequest] = []
    private var responseStatus = 200
    var status: Int {
        get { lock.withLock { responseStatus } }
        set { lock.withLock { responseStatus = newValue } }
    }
    var runs: [ModelPreferencesUpdate] { lock.withLock { recordedRuns } }
    var updates: [ModelPreferencesUpdate] { lock.withLock { recordedUpdates } }
    var requests: [URLRequest] { lock.withLock { recordedRequests } }
    let models: [ModelChoice] = [
        ModelChoice(id: "claude", name: "Claude", thinking: ["low", "medium", "high"],
                    options: ["verbosity": ["low", "high"]], defaults: ["thinking": .string("medium")]),
        ModelChoice(id: "gpt", name: "GPT", thinking: ["low", "medium", "high"],
                    options: ["summary": ["auto", "detailed"]], defaults: ["thinking": .string("low")]),
        ModelChoice(id: "plain", name: "Plain", thinking: [], options: [:], defaults: [:]),
    ]
    let api: APIClient

    init() {
        api = APIClient(baseURL: URL(string: "https://\(host)")!,
                           cookies: .sharedCookieStorage(forGroupContainerIdentifier: host),
                           siteLogin: "Basic test", protocols: [PreferenceProtocol.self])
        PreferenceProtocol.register(self)
    }

    func answer(_ request: URLRequest, body: Data) throws -> (Int, Data) {
        try lock.withLock {
            recordedRequests.append(request)
            if responseStatus != 200 { return (responseStatus, Data(#"{"detail":"try again"}"#.utf8)) }
            if request.url?.path == "/api/model-preferences" {
                if request.httpMethod == "PUT" {
                    saved = try JSONDecoder().decode(ModelPreferencesUpdate.self, from: body)
                    recordedUpdates.append(saved)
                }
                return (200, try JSONEncoder().encode(ModelPreferences(model: saved.model, settings: saved.settings, models: models)))
            }
            recordedRuns.append(saved)
            return (200, Data(#"{"thread_id":"t","run_id":"r"}"#.utf8))
        }
    }
}

private final class PreferenceProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) private static var servers: [String: PreferenceServer] = [:]
    private static let lock = NSLock()
    static func register(_ server: PreferenceServer) { lock.withLock { servers[server.host] = server } }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let server = Self.lock.withLock({ Self.servers[request.url?.host() ?? ""] }) else { return }
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                body.append(buffer, count: count)
            }
        }
        do {
            let (status, data) = try server.answer(request, body: body)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
