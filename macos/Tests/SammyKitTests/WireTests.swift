import Foundation
import Testing
@testable import SammyKit

// The live view's protocol and the run's event stream, checked against what `sammy/liveview/wire.py` and
// `api.run_events` send and accept.

@Suite struct LiveWireTests {
    func object(_ input: LiveInput) -> [String: AnyHashable] {
        (try? JSONSerialization.jsonObject(with: Data(input.json.utf8))) as? [String: AnyHashable] ?? [:]
    }

    @Test func inputIsTheJsonWirePyDecodes() {
        #expect(object(.mouseDown(x: 10, y: 20.5, button: .left)) == ["kind": "mouse_down", "x": 10, "y": 20.5, "button": "left"])
        #expect(object(.mouseMove(x: 1, y: 2)) == ["kind": "mouse_move", "x": 1, "y": 2])
        #expect(object(.mouseUp(x: 1, y: 2, button: .right)) == ["kind": "mouse_up", "x": 1, "y": 2, "button": "right"])
        #expect(object(.scroll(x: 5, y: 6, deltaX: 0, deltaY: -40)) == ["kind": "scroll", "x": 5, "y": 6, "delta_x": 0, "delta_y": -40])
        #expect(object(.type("héllo \"x\"")) == ["kind": "type", "text": "héllo \"x\""])
        #expect(object(.press(key: "a", modifiers: ["Control", "Shift"])) == ["kind": "press", "key": "a", "modifiers": ["Control", "Shift"]])
        #expect(object(.switchTab("t2")) == ["kind": "switch_tab", "tab_id": "t2"])
        #expect(object(.navigate("https://x.test/a?b=1")) == ["kind": "navigate", "url": "https://x.test/a?b=1"])
        #expect(object(.giveBack) == ["kind": "give_back"])
        #expect(object(.outline) == ["kind": "outline"])
        #expect(object(.page(.back)) == ["kind": "back"])
        #expect(object(.page(.forward)) == ["kind": "forward"])
        #expect(object(.page(.reload)) == ["kind": "reload"])
        #expect(object(.page(.stop)) == ["kind": "stop"])
        #expect(object(.newTab) == ["kind": "new_tab"])
        #expect(object(.closeTab("t2")) == ["kind": "close_tab", "tab_id": "t2"])
    }

    @Test func theBrowsersButtonsComeFromHelloAndTabs() {
        #expect(LiveServerMessage(json: #"{"kind": "hello", "handoff_id": "h1", "reason": "Sign in", "controls": true}"#)
            == .hello(handoffId: "h1", reason: "Sign in", controls: true))
        #expect(LiveServerMessage(json: #"{"kind": "tabs", "tabs": [{"tab_id": "a", "url": "https://x.test/", "title": "X", "active": true, "closable": true, "loading": true, "can_go_back": true, "can_go_forward": false}]}"#)
            == .tabs([LiveTab(id: "a", url: "https://x.test/", title: "X", active: true, closable: true, loading: true, canGoBack: true, canGoForward: false)]))
    }

    @Test func tabsAreNamedByTitleThenHost() {
        #expect(LiveTab(id: "a", url: "https://x.test/a", title: "X shop", active: true).name == "X shop")
        #expect(LiveTab(id: "a", url: "https://x.test/a", title: "", active: true).name == "x.test")
        #expect(LiveTab(id: "a", url: "about:blank", title: "", active: true).name == "New Tab")
        #expect(LiveTab(id: "a", url: "about:blank", title: "", active: true).isBlank)
    }

    @Test func theAddressBarOpensAddressesHostsAndSearches() {
        #expect(LiveSession.address(for: "  https://www.walmart.ca/en  ") == "https://www.walmart.ca/en")
        #expect(LiveSession.address(for: "HTTP://shop.test") == "HTTP://shop.test")
        #expect(LiveSession.address(for: "walmart.com") == "https://walmart.com")
        #expect(LiveSession.address(for: "walmart.ca/en/cart?x=1") == "https://walmart.ca/en/cart?x=1")
        #expect(LiveSession.address(for: "localhost:8000/x") == "https://localhost:8000/x")
        #expect(LiveSession.address(for: "cheap eggs") == "https://www.google.com/search?q=cheap%20eggs")
        #expect(LiveSession.address(for: "eggs") == "https://www.google.com/search?q=eggs")
        #expect(LiveSession.address(for: "   ") == nil)
    }

    @Test func serverMessages() {
        #expect(LiveServerMessage(json: #"{"kind": "hello", "handoff_id": "h1", "reason": "Sign in"}"#) == .hello(handoffId: "h1", reason: "Sign in"))
        #expect(LiveServerMessage(json: #"{"kind": "tabs", "tabs": [{"tab_id": "a", "url": "https://x.test/", "title": "X", "active": true}, {"tab_id": "b", "url": "about:blank", "title": "", "active": false}]}"#)
            == .tabs([LiveTab(id: "a", url: "https://x.test/", title: "X", active: true), LiveTab(id: "b", url: "about:blank", title: "", active: false)]))
        #expect(LiveServerMessage(json: #"{"kind": "error", "message": "Unknown key"}"#) == .error("Unknown key"))
        #expect(LiveServerMessage(json: #"{"kind": "ended", "given_back": true}"#) == .ended(givenBack: true))
        #expect(LiveServerMessage(json: #"{"kind": "ended"}"#) == .ended(givenBack: false))
        #expect(LiveServerMessage(json: #"{"kind": "outline", "title": "Sign in", "available": true, "items": [{"role": "heading", "name": "Sign in", "x": 40, "y": 40, "width": 640, "height": 38, "level": 1}, {"role": "textbox", "name": "password", "x": 40, "y": 136, "width": 153, "height": 21, "value": "5 characters", "secure": true, "focused": true}, {"role": "checkbox", "name": "Remember me", "x": 0, "y": 0, "width": 13, "height": 13, "checked": false}, {"role": "broken"}]}"#)
            == .outline(PageOutline(title: "Sign in", items: [
                OutlineItem(role: "heading", name: "Sign in", x: 40, y: 40, width: 640, height: 38, level: 1),
                OutlineItem(role: "textbox", name: "password", x: 40, y: 136, width: 153, height: 21, value: "5 characters", focused: true, secure: true),
                OutlineItem(role: "checkbox", name: "Remember me", x: 0, y: 0, width: 13, height: 13, checked: false),
            ])))
        #expect(LiveServerMessage(json: #"{"kind": "outline", "available": false, "items": []}"#) == .outline(PageOutline(title: "", items: [], available: false)))
        #expect(OutlineItem(role: "button", name: "Go", x: 10, y: 20, width: 30, height: 10).centre == (25, 25))
        #expect(LiveServerMessage(json: #"{"kind": "nope"}"#) == nil)
        #expect(LiveServerMessage(json: "not json") == nil)
    }

    @Test func framesRoundTrip() throws {
        let image = Data([0xFF, 0xD8, 0xFF, 0x00, 0x01])
        let frame = try #require(LiveFrame(data: LiveFrame.encode(seq: 7, width: 1280, height: 720, mime: "image/jpeg", image: image)))
        #expect(frame.seq == 7)
        #expect(frame.width == 1280 && frame.height == 720)
        #expect(frame.mime == "image/jpeg")
        #expect(frame.image == image)
    }

    @Test func badFramesAreDropped() {
        #expect(LiveFrame(data: Data([0, 0])) == nil)
        #expect(LiveFrame(data: Data([0, 0, 0, 50]) + Data("{}".utf8)) == nil)  // header longer than the data
        #expect(LiveFrame(data: LiveFrame.encode(seq: 1, width: 10, height: 10, mime: "image/gif", image: Data())) == nil)
        #expect(LiveFrame(data: LiveFrame.encode(seq: 1, width: 0, height: 10, mime: "image/png", image: Data())) == nil)
    }
}

@Suite struct EventStreamTests {
    func events(_ text: String) -> [ServerSentEvent] {
        var parser = EventStreamParser()
        return text.components(separatedBy: "\n").compactMap { parser.feed($0) }
    }

    @Test func namedEventsEndAtTheBlankLine() {
        let parsed = events("event: status\ndata: {\"a\": 1}\n\n: keep-alive\n\nevent: preview\ndata: {\"b\": 2}\n\n")
        #expect(parsed == [ServerSentEvent(name: "status", data: #"{"a": 1}"#), ServerSentEvent(name: "preview", data: #"{"b": 2}"#)])
    }

    @Test func multipleDataLinesJoin() {
        #expect(events("data: one\ndata:two\n\n") == [ServerSentEvent(name: "message", data: "one\ntwo")])
    }

    @Test func runEventsDecode() {
        let status = ServerSentEvent(name: "status", data: #"{"id": "r", "thread_id": "t", "status": "waiting", "output": null, "activity": ["Opening x.test"], "ask": {"id": "a", "kind": "handoff", "prompt": "Sign in"}}"#)
        #expect(RunEvent(status) == .status(Run(id: "r", threadId: "t", status: .waiting, activity: ["Opening x.test"], ask: Ask(id: "a", kind: .handoff, prompt: "Sign in"))))
        let preview = ServerSentEvent(name: "preview", data: #"{"revision": 3, "text": "Hel", "activity": "Writing"}"#)
        #expect(RunEvent(preview) == .preview(Preview(revision: 3, text: "Hel", activity: "Writing")))
        #expect(RunEvent(ServerSentEvent(name: "status", data: "{}")) == nil)
    }
}

/// A private server's site login goes with every request by hand, so URLSession never asks the keychain for it.
@Suite struct SiteLoginTests {
    let login = APIClient.basicAuthorization(user: "sammy", password: "pa:ss")
    var client: APIClient { APIClient(baseURL: URL(string: "https://sammy.test")!, siteLogin: login) }

    @Test func everyRequestCarriesIt() throws {
        #expect(login == "Basic " + Data("sammy:pa:ss".utf8).base64EncodedString())
        #expect(client.request("GET", "/api/me").value(forHTTPHeaderField: "Authorization") == login)
        let link = LiveLink(url: "/live/h1/", reason: "Sign in")
        let socket = try #require(client.liveSocketRequest(link))
        #expect(socket.value(forHTTPHeaderField: "Authorization") == login)
        #expect(APIClient(baseURL: URL(string: "https://sammy.test")!).request("GET", "/").value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test func theKeychainIsNeverAsked() {
        #expect(client.session.configuration.urlCredentialStorage == nil)
    }
}

@Suite struct KeyMappingTests {
    func action(_ code: UInt16, _ chars: String, command: Bool = false, option: Bool = false, control: Bool = false, shift: Bool = false) -> KeyAction {
        KeyMapping.action(for: MacKey(keyCode: code, characters: chars, command: command, option: option, control: control, shift: shift))
    }

    @Test func textIsTyped() {
        #expect(action(0, "a") == .send([.type("a")]))
        #expect(action(0, "A", shift: true) == .send([.type("A")]))
        #expect(action(14, "é", option: true) == .send([.type("é")]))
    }

    @Test func namedKeysArePressed() {
        #expect(action(36, "\r") == .send([.press(key: "Enter", modifiers: [])]))
        #expect(action(48, "\t", shift: true) == .send([.press(key: "Tab", modifiers: ["Shift"])]))
        #expect(action(51, "\u{7f}") == .send([.press(key: "Backspace", modifiers: [])]))
        #expect(action(53, "\u{1b}") == .send([.press(key: "Escape", modifiers: [])]))
    }

    @Test func macEditingShortcutsBecomeLinuxOnes() {
        #expect(action(0, "a", command: true) == .send([.press(key: "a", modifiers: ["Control"])]))
        #expect(action(6, "z", command: true, shift: true) == .send([.press(key: "z", modifiers: ["Control", "Shift"])]))
        #expect(action(9, "v", command: true) == .pasteFromMac)
        #expect(action(123, "", option: true) == .send([.press(key: "ArrowLeft", modifiers: ["Control"])]))
        #expect(action(51, "", option: true) == .send([.press(key: "Backspace", modifiers: ["Control"])]))
        #expect(action(123, "", command: true, shift: true) == .send([.press(key: "Home", modifiers: ["Shift"])]))
        #expect(action(51, "", command: true) == .send([.press(key: "Home", modifiers: ["Shift"]), .press(key: "Backspace", modifiers: [])]))
    }

    func shortcut(_ code: UInt16, _ chars: String, command: Bool = false, control: Bool = false, shift: Bool = false) -> BrowserShortcut? {
        BrowserShortcut.shortcut(for: MacKey(keyCode: code, characters: chars, command: command, control: control, shift: shift))
    }

    @Test func browserShortcuts() {
        #expect(shortcut(33, "[", command: true) == .page(.back))
        #expect(shortcut(30, "]", command: true) == .page(.forward))
        #expect(shortcut(15, "r", command: true) == .page(.reload))
        #expect(shortcut(17, "t", command: true) == .newTab)
        #expect(shortcut(13, "w", command: true) == .closeTab)
        #expect(shortcut(37, "l", command: true) == .editAddress)
        #expect(shortcut(18, "1", command: true) == .tab(1))
        #expect(shortcut(25, "9", command: true) == .tab(9))
        #expect(shortcut(48, "\t", control: true) == .nextTab)
        #expect(shortcut(48, "\t", control: true, shift: true) == .previousTab)
        #expect(shortcut(17, "T", command: true, shift: true) == nil)  // ⇧⌘T is "Not now"
        #expect(shortcut(0, "a", command: true) == nil)  // select all, on the page
        #expect(shortcut(29, "0", command: true) == nil)
        #expect(shortcut(48, "\t") == nil)
    }

    @Test func tabShortcutsPickATab() {
        let tabs = ["a", "b", "c"].map { LiveTab(id: $0, url: "", title: "", active: $0 == "c") }
        #expect(BrowserShortcut.target(of: .nextTab, in: tabs)?.id == "a")  // wraps around
        #expect(BrowserShortcut.target(of: .previousTab, in: tabs)?.id == "b")
        #expect(BrowserShortcut.target(of: .tab(1), in: tabs)?.id == "a")
        #expect(BrowserShortcut.target(of: .tab(9), in: tabs)?.id == "c")  // the last, as in a browser
        #expect(BrowserShortcut.target(of: .tab(5), in: tabs) == nil)
        #expect(BrowserShortcut.target(of: .nextTab, in: []) == nil)
    }

    @Test func appShortcutsStayWithTheApp() {
        #expect(action(13, "w", command: true) == .ignore)
        #expect(action(12, "q", command: true) == .ignore)
        #expect(action(45, "n", command: true) == .ignore)
    }
}

@Suite struct ModelTests {
    @Test func scheduleInWords() throws {
        let json = #"{"id": "s", "name": "Groceries", "when": "Mondays at 09:00 (0 9 * * 1, Europe/London)", "paused": false, "watch": false, "thread_id": "t"}"#
        let schedule = try JSONDecoder().decode(Schedule.self, from: Data(json.utf8))
        #expect(schedule.plainWhen == "Mondays at 09:00")
        #expect(schedule.timeZone == "Europe/London")
        let now = #"{"id": "s", "name": "Slots", "when": "every 30 minutes (America/Toronto)", "paused": false, "watch": true, "thread_id": "t"}"#
        let current = try JSONDecoder().decode(Schedule.self, from: Data(now.utf8))
        #expect(current.plainWhen == "every 30 minutes" && current.timeZone == "America/Toronto")  // as the server says it now
    }

    @Test func threadListStatus() throws {
        let json = #"[{"id": "a", "title": "Eggs", "status": "waiting"}, {"id": "b", "title": "Hi", "status": null}]"#
        let threads = try JSONDecoder().decode([ThreadSummary].self, from: Data(json.utf8))
        #expect(threads == [ThreadSummary(id: "a", title: "Eggs", status: .waiting), ThreadSummary(id: "b", title: "Hi")])
    }

    @Test func aChatWithEventsAndRolesNotKnownYetReads() throws {
        let json = #"""
        {"id": "t", "title": "Eggs", "messages": [
            {"role": "user", "text": "Order eggs"}, {"role": "event", "text": "You approved: Buy eggs"},
            {"role": "something-new", "text": "?"}, {"role": "assistant", "text": "Done"}],
         "run": {"id": "r", "thread_id": "t", "status": "done", "output": "Done", "activity": [], "ask": null}}
        """#
        let thread = try JSONDecoder().decode(ThreadDetail.self, from: Data(json.utf8))
        #expect(thread.messages.map(\.role) == [.user, .event, .event, .assistant])
        #expect(thread.run?.prompt == nil)  // an older server, without it
    }

    @Test func chatsAreGroupedByWhenTheyWereLastActive() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Europe/London")!
        let now = try #require(ThreadSummary.date("2026-10-07T15:00:00+00:00"))
        func age(_ text: String) throws -> ChatAge { ChatAge.of(try #require(ThreadSummary.date(text)), now: now, calendar: calendar) }
        #expect(try age("2026-10-07T00:30:00.123456+00:00") == .today)
        #expect(try age("2026-10-06T08:00:00+00:00") == .yesterday)
        #expect(try age("2026-10-01T08:00:00+00:00") == .week)
        #expect(try age("2026-09-15T08:00:00+00:00") == .month)
        #expect(try age("2025-01-01T08:00:00+00:00") == .earlier)
        #expect(ChatAge.of(nil, now: now, calendar: calendar) == .today)  // just made, not read back yet
    }

    @Test func aChatListFromAnOlderServerHasNoTimes() throws {
        let json = #"[{"id": "a", "title": "Eggs", "status": null, "outcome": "done", "updated_at": "2026-10-07T15:58:18.5+00:00"}, {"id": "b", "title": "Hi"}]"#
        let threads = try JSONDecoder().decode([ThreadSummary].self, from: Data(json.utf8))
        #expect(threads[0].updatedAt == ThreadSummary.date("2026-10-07T15:58:18.5+00:00") && threads[0].updatedAt != nil)
        #expect(threads[1].updatedAt == nil && threads[1].outcome == nil)
    }

    @Test func aScheduleSaysWhenItRunsAndHowItWent() throws {
        let json = #"{"id": "s", "name": "Slots", "when": "every 30 minutes (*/30 * * * *, UTC)", "paused": false, "watch": true, "thread_id": "t", "next_run_at": "2026-10-12T09:00:00+01:00", "last_run_at": "2026-10-12T07:00:00+00:00", "last_status": "failed"}"#
        let schedule = try JSONDecoder().decode(Schedule.self, from: Data(json.utf8))
        let last = try #require(schedule.lastRun)
        let times = try #require(schedule.times(now: last.addingTimeInterval(7200)))
        #expect(times.failed && times.text.hasPrefix("Next: ") && times.text.contains("couldn't finish"))
        let old = #"{"id": "s", "name": "Slots", "when": "x", "paused": false, "watch": true, "thread_id": "t"}"#
        #expect(try JSONDecoder().decode(Schedule.self, from: Data(old.utf8)).nextRun == nil)  // an older server
    }

    @Test func usageReadsAsTheServerSendsIt() throws {
        // `api.read_usage`: floats from Decimals, a null cap when unset, a null id and name for chats since deleted.
        let json = #"""
        {"today": 0.0036, "month": 7.5, "daily_cap": null, "monthly_cap": 20.0,
         "tokens": {"input": 4000, "output": 200, "cache_read": 3000},
         "threads": [{"id": "t", "name": "Weekly groceries", "cost": 6.0}, {"id": null, "name": null, "cost": 1.5}],
         "schedules": [{"id": "s", "name": "Weekly groceries", "cost": 5.25}]}
        """#
        let usage = try JSONDecoder().decode(Usage.self, from: Data(json.utf8))
        #expect(usage.dailyCap == nil && usage.monthlyCap == 20)
        #expect(usage.threads.map(\.id) == ["t", nil] && usage.schedules.map(\.cost) == [5.25])
        #expect(Usage.spent(usage.today, of: usage.dailyCap) == "under $0.01")
        #expect(Usage.spent(usage.month, of: usage.monthlyCap) == "$7.50 of $20.00")
        #expect(Usage.dollars(0) == "$0.00")
    }

    @Test func whatAWaitingChatWaitsFor() throws {
        let json = #"[{"id": "a", "title": "Eggs", "status": "waiting", "waiting_for": "approval"}, {"id": "b", "title": "Hi", "status": "waiting", "waiting_for": "something-new"}]"#
        let threads = try JSONDecoder().decode([ThreadSummary].self, from: Data(json.utf8))
        #expect(threads.map(\.waitingFor) == [.approval, nil])  // a kind not known yet is left unsaid, not an error
    }

    @Test func aChatWaitingForAnAppToBeConnected() throws {
        let json = #"""
        {"id": "t", "title": "Linear", "messages": [{"role": "user", "text": "yo what's on my linear"}],
         "run": {"id": "r", "thread_id": "t", "status": "waiting", "activity": [],
                 "ask": {"id": "a", "kind": "connect", "prompt": "Connect Linear so I can look up your issues.",
                         "integration": {"provider": "composio", "key": "linear", "name": "Linear", "logo": "https://logos.composio.dev/api/linear"}}}}
        """#
        let ask = try #require(try JSONDecoder().decode(ThreadDetail.self, from: Data(json.utf8)).run?.ask)
        #expect(ask.kind == .connect)
        #expect(ask.integration == Offer(provider: "composio", key: "linear", name: "Linear", logo: "https://logos.composio.dev/api/linear"))
        #expect(ask.integration?.isApp == true)
        let server = #"{"id": "a", "kind": "connect", "prompt": "Sign in again", "integration": {"provider": "mcp", "key": "mcp:wiki", "name": "Wiki", "logo": "", "server_id": "s1"}}"#
        #expect(try JSONDecoder().decode(Ask.self, from: Data(server.utf8)).integration?.serverId == "s1")
        let list = #"[{"id": "a", "title": "Linear", "status": "waiting", "waiting_for": "connect"}]"#
        #expect(try JSONDecoder().decode([ThreadSummary].self, from: Data(list.utf8)).map(\.waitingFor) == [.connect])
    }

    @Test func anAskOfAKindNotKnownYetDoesNotBreakTheChat() throws {
        let json = #"{"id": "r", "thread_id": "t", "status": "waiting", "activity": [], "ask": {"id": "a", "kind": "something-new", "prompt": "?"}}"#
        let run = try JSONDecoder().decode(Run.self, from: Data(json.utf8))
        #expect(run.ask?.kind == .other && run.ask?.integration == nil)
    }

    @Test func integrations() throws {
        let json = #"""
        {"apps_available": true, "connections": [
            {"id": "ca_1", "key": "linear", "provider": "composio", "name": "Linear", "detail": "Issue tracking", "logo": "", "state": "connected"},
            {"id": "s1", "key": "mcp:wiki", "provider": "mcp", "name": "Wiki", "detail": "wiki.example.com", "logo": "", "state": "needs_sign_in"}]}
        """#
        let found = try JSONDecoder().decode(Integrations.self, from: Data(json.utf8))
        #expect(found.appsAvailable)
        #expect(found.connections.map { [$0.isApp, $0.isConnected] } == [[true, true], [false, false]])
        let apps = try JSONDecoder().decode([CatalogApp].self, from: Data(#"[{"slug": "gmail", "name": "Gmail", "logo": "", "description": "Email from Google", "categories": ["email"]}]"#.utf8))
        #expect(apps[0].matches("") && apps[0].matches("GMAIL") && apps[0].matches("google") && apps[0].matches("email"))
        #expect(!apps[0].matches("linear"))
        let added = try JSONDecoder().decode(AddedServer.self, from: Data(#"{"connection": {"id": "s2", "key": "mcp:notes", "provider": "mcp", "name": "Notes", "detail": "notes.example.com", "logo": "", "state": "needs_sign_in"}, "sign_in_url": "https://auth.example.com/authorize?x=1"}"#.utf8))
        #expect(added.signInUrl == "https://auth.example.com/authorize?x=1" && added.connection.key == "mcp:notes")
        // Answering a connect ask sends only what the server reads for it.
        let body = try JSONSerialization.jsonObject(with: JSONEncoder().encode(AnswerBody.connected(false))) as? [String: Bool]
        #expect(body == ["connected": false])
    }

    @Test func anUploadSaysWhatItIs() throws {
        let json = #"{"id": "a1", "name": "photo.png", "media_type": "image/png", "size": 12345, "kind": "image"}"#
        let file = try JSONDecoder().decode(Attachment.self, from: Data(json.utf8))
        #expect(file == Attachment(id: "a1", name: "photo.png", mediaType: "image/png", size: 12345, kind: .image))
        #expect(file.isImage && !file.isPDF)
        let later = #"{"id": "a2", "name": "x.bin", "media_type": "application/x-new", "size": 1, "kind": "something-new"}"#
        #expect(try JSONDecoder().decode(Attachment.self, from: Data(later.utf8)).kind == .file)  // not an error
        // Named and typed a PNG, but the server found its bytes are not a picture: a file to save, not to show.
        #expect(!Attachment(id: "a3", name: "fake.png", mediaType: "image/png", size: 4, kind: .file).isImage)
        #expect(Attachment(id: "a4", name: "old.png", mediaType: "image/png", size: 4).isImage)  // a server that doesn't say
    }

    @Test func messagesCarryTheirFiles() throws {
        let json = #"""
        {"id": "t", "title": "Receipt", "messages": [
            {"role": "user", "text": "", "files": [{"id": "a1", "name": "receipt.pdf", "media_type": "application/pdf", "size": 2048}]},
            {"role": "assistant", "text": "Here's the summary.", "files": [{"id": "a2", "name": "summary.csv", "media_type": "text/csv", "size": 99}]},
            {"role": "event", "text": "You approved: Pay"}],
         "run": null}
        """#
        let messages = try JSONDecoder().decode(ThreadDetail.self, from: Data(json.utf8)).messages
        #expect(messages[0].text.isEmpty)  // files alone
        #expect(messages[0].files == [Attachment(id: "a1", name: "receipt.pdf", mediaType: "application/pdf", size: 2048)])
        #expect(messages[0].files.first?.isPDF == true && messages[0].files.first?.kind == nil)
        #expect(messages[1].files.map(\.name) == ["summary.csv"])
        #expect(messages[2].files.isEmpty)  // no key: no files, as from older servers
        #expect(messages[2] == ChatMessage(role: .event, text: "You approved: Pay"))
    }

    @Test func anUploadIsTheFileItself() throws {
        let client = APIClient(baseURL: URL(string: "https://sammy.test")!)
        let bytes = Data([0x89, 0x50, 0x4E, 0x47, 0, 1, 2])
        let request = client.uploadRequest(data: bytes, name: "Façade plan #2.png", mediaType: "image/png")
        #expect(request.httpMethod == "POST" && request.url?.path == "/api/attachments")
        #expect(request.httpBody == bytes)  // not JSON, not multipart
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "image/png")
        #expect(request.value(forHTTPHeaderField: "X-Filename") == "Fa%C3%A7ade%20plan%20%232.png")
        let unknown = client.uploadRequest(data: Data(), name: "notes", mediaType: "")
        #expect(unknown.value(forHTTPHeaderField: "Content-Type") == "application/octet-stream")
    }

    @Test func messagesNameTheirFilesByIdOnlyWhenThereAreSome() throws {
        func body(_ attachments: [String]) throws -> [String: AnyHashable] {
            let data = try JSONEncoder().encode(APIClient.MessageBody("", attachments, nil))
            return try #require(try JSONSerialization.jsonObject(with: data) as? [String: AnyHashable])
        }
        #expect(try body(["a1", "a2"])["attachments"] == AnyHashable(["a1", "a2"]))
        #expect(try body(["a1"])["text"] == AnyHashable(""))
        #expect(try body([])["attachments"] == nil)  // as before, for older servers
    }

    @Test func theFilesPageIsGoneAndOpensANewTaskInstead() {
        #expect(Route(stored: "files") == nil)  // kept from before: the app falls back to a new task
        for route in [Route.chat(nil), .chat("t"), .schedules, .signIns, .integrations, .memory] {
            #expect(Route(stored: route.stored) == route)
        }
    }

    @MainActor @Test func aFileTooLargeIsNotUploaded() {
        let id = "sammy-test-\(UUID().uuidString)"
        let app = AppModel(serverURL: URL(string: "http://127.0.0.1:9")!, cookies: .sharedCookieStorage(forGroupContainerIdentifier: id),
                           defaults: UserDefaults(suiteName: id)!)
        app.open(.chat(nil))
        let chat = app.chat!
        chat.attach(Data(count: ChatModel.maxAttachmentBytes + 1), name: "huge.mov", mediaType: "video/quicktime")
        #expect(chat.attachments.isEmpty)
        #expect(chat.notice?.isError == true && chat.notice?.text.contains("too large") == true)
        #expect(!chat.canSend)
    }

    @Test func durationsReadAtAGlance() {
        #expect(spoken(0) == "0s" && spoken(12.9) == "12s" && spoken(63) == "1m 3s" && spoken(7500) == "2h 5m")
        #expect(spoken(-3) == "0s")  // a server clock a little ahead
    }

    @Test func pagesOfOneSiteAreOneStep() {
        let steps = ["Opening walmart.com", "Opening walmart.com", "Opening walmart.com", "Comparing prices", "Comparing prices",
                     "Opening target.com", "Opening walmart.com"]
        #expect(ChatModel.grouped(steps) == [
            "Browsing walmart.com · 3 pages", "Comparing prices", "Opening target.com", "Opening walmart.com",
        ])
    }

    @Test func notificationTextIsPlain() {
        #expect(AppModel.plain("**Done.** Your order is [#1](http://x). `ok`") == "Done. Your order is #1. ok")
        #expect(AppModel.plain(String(repeating: "a", count: 300)).count == 200)
    }

    @Test func titlesShowSitesNotLinks() {
        #expect("Order eggs from http://127.0.0.1:65090".readableTitle == "Order eggs from 127.0.0.1:65090")
        #expect("Check https://www.walmart.com/cart?x=1 for eggs".readableTitle == "Check walmart.com for eggs")
        #expect("   ".readableTitle == "Untitled")
    }

    @Test func serverErrorsReadAsSentences() {
        #expect(APIError.server(status: 409, detail: "that was answered already").localizedDescription == "That was answered already.")
        #expect(APIError.server(status: 500, detail: nil).localizedDescription.contains("server had a problem"))
    }

    @Test func featuredIntegrationsAndKnownMcpServers() throws {
        let json = #"""
        [{"key": "posthog", "slug": "posthog", "name": "PostHog", "logo": "https://posthog.com/logo.png", "description": "Product analytics",
          "categories": ["analytics"], "kind": "analytics", "kind_label": "Analytics", "featured": true, "provider": "mcp",
          "url": "https://mcp.posthog.com/mcp", "host": "mcp.posthog.com"},
         {"key": "linear", "slug": "linear", "name": "Linear", "logo": "", "description": "Issue tracking", "categories": [],
          "kind": "issues", "kind_label": "Issues & projects", "featured": true, "provider": "composio", "url": null, "host": null}]
        """#
        let apps = try JSONDecoder().decode([CatalogApp].self, from: Data(json.utf8))
        let posthog = apps[0], linear = apps[1]
        #expect(posthog.key == "posthog" && posthog.featured && !posthog.isApp && posthog.kindLabel == "Analytics")
        #expect(posthog.url == "https://mcp.posthog.com/mcp" && posthog.host == "mcp.posthog.com")
        #expect(linear.isApp && linear.kind == "issues" && linear.url == nil && linear.matches("projects"))
        // A connection is an entry's when it is the same app, or a server at the preset's host.
        func connection(_ key: String, _ provider: String, _ detail: String) throws -> Connection {
            let json = #"{"id": "x", "key": "\#(key)", "provider": "\#(provider)", "name": "N", "detail": "\#(detail)", "logo": "", "state": "connected"}"#
            return try JSONDecoder().decode(Connection.self, from: Data(json.utf8))
        }
        #expect(posthog.matches(try connection("mcp:posthog", "mcp", "mcp.posthog.com")))
        #expect(!posthog.matches(try connection("mcp:other", "mcp", "other.example.com")))
        #expect(linear.matches(try connection("linear", "composio", "Issue tracking")))
        #expect(!linear.matches(try connection("mcp:linear", "mcp", "mcp.linear.app")))
        // A chat offering the known server carries its address, to add it in one click.
        let ask = #"{"id": "a", "kind": "connect", "prompt": "Connect PostHog", "integration": {"provider": "mcp", "key": "posthog", "name": "PostHog", "logo": "", "url": "https://mcp.posthog.com/mcp"}}"#
        let offer = try #require(try JSONDecoder().decode(Ask.self, from: Data(ask.utf8)).integration)
        #expect(offer == Offer(provider: "mcp", key: "posthog", name: "PostHog", url: "https://mcp.posthog.com/mcp"))
        #expect(offer.isPreset && !offer.isApp && !offer.needsToken)
    }

    @Test func harnessIntegrationsSayHowTheySignIn() throws {
        let json = #"""
        [{"key": "github", "slug": "github", "name": "GitHub", "logo": "", "description": "Code", "categories": [], "kind": "code",
          "kind_label": "Code", "featured": true, "provider": "mcp", "url": "https://api.githubcopilot.com/mcp/",
          "host": "api.githubcopilot.com", "auth": "token", "token_hint": "A GitHub personal access token", "token_header": "Authorization"},
         {"key": "linear", "slug": "linear", "name": "Linear", "logo": "", "description": "Issues", "categories": [], "kind": "issues",
          "featured": true, "provider": "mcp", "url": "https://mcp.linear.app/mcp", "host": "mcp.linear.app",
          "auth": "oauth", "token_hint": null, "token_header": null},
         {"key": "linear", "slug": "linear", "name": "Linear via Composio", "logo": "", "description": "Issues", "categories": [],
          "featured": false, "provider": "composio", "url": null, "host": null, "auth": null},
         {"slug": "gmail", "name": "Gmail", "logo": "", "description": "Email", "categories": ["email"]}]
        """#
        let apps = try JSONDecoder().decode([CatalogApp].self, from: Data(json.utf8))
        let github = apps[0], linear = apps[1], composioLinear = apps[2], gmail = apps[3]
        #expect(github.needsToken && github.auth == "token")
        #expect(github.tokenHint == "A GitHub personal access token" && github.tokenHeader == "Authorization")
        #expect(!linear.needsToken && linear.auth == "oauth" && linear.tokenHint == nil && linear.tokenHeader == nil)
        #expect(!composioLinear.needsToken && composioLinear.auth == nil && composioLinear.isApp)
        #expect(gmail.auth == nil && gmail.tokenHint == nil && !gmail.needsToken)
        // The same key from two providers is two entries.
        #expect(linear.id != composioLinear.id)
        #expect(Set(apps.map(\.id)).count == apps.count)
        // A pasted token goes in the named header as a bearer token; none without one.
        #expect(AppModel.tokenHeaders("Authorization", " tok \n") == ["Authorization": "Bearer tok"])
        #expect(AppModel.tokenHeaders(nil, "tok") == ["Authorization": "Bearer tok"])
        #expect(AppModel.tokenHeaders("Authorization", "  ").isEmpty && AppModel.tokenHeaders("Authorization", nil).isEmpty)
        // A chat offering it says so too.
        let ask = #"""
        {"id": "a", "kind": "connect", "prompt": "Connect GitHub", "integration": {"provider": "mcp", "key": "github", "name": "GitHub",
         "logo": "", "url": "https://api.githubcopilot.com/mcp/", "auth": "token", "token_hint": "A GitHub personal access token",
         "token_header": "Authorization"}}
        """#
        let offer = try #require(try JSONDecoder().decode(Ask.self, from: Data(ask.utf8)).integration)
        #expect(offer == Offer(provider: "mcp", key: "github", name: "GitHub", url: "https://api.githubcopilot.com/mcp/", auth: "token",
                               tokenHint: "A GitHub personal access token", tokenHeader: "Authorization"))
        #expect(offer.isPreset && offer.needsToken)
    }
}

@Suite struct MarkdownTests {
    @Test func theFlightsReplyIsATable() {
        let reply = "The three cheapest flights to Lisbon next Friday:\n\n| Flight | Departs | Price |\n|---|---|---|\n| FR 8341 | 10:15 | €64 |\n| U2 8169 | 12:30 | €97 |"
        #expect(MarkdownBlock.parse(reply) == [
            .paragraph("The three cheapest flights to Lisbon next Friday:"),
            .table(header: ["Flight", "Departs", "Price"], rows: [["FR 8341", "10:15", "€64"], ["U2 8169", "12:30", "€97"]]),
        ])
    }

    @Test func tableWithoutBlankLineOrAlignment() {
        #expect(MarkdownBlock.parse("Prices:\n| a | b |\n| :-- | --: |\n| 1 | 2 |\nThat's all.") == [
            .paragraph("Prices:"), .table(header: ["a", "b"], rows: [["1", "2"]]), .paragraph("That's all."),
        ])
    }

    @Test func pipesThatAreNotATableStayText() {
        #expect(MarkdownBlock.parse("| just | a line |") == [.paragraph("| just | a line |")])
    }

    @Test func blocks() {
        let text = "# Done\n\nI found:\n- eggs\n- milk,\n  semi-skimmed\n\n1. one\n2. two\n\n> note\n\n```\ncode()\n```\n---"
        #expect(MarkdownBlock.parse(text) == [
            .heading(1, "Done"), .paragraph("I found:"), .bullet(["eggs", "milk, semi-skimmed"]),
            .numbered(["one", "two"]), .quote("note"), .code("code()"), .rule,
        ])
    }
}
