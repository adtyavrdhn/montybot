import Foundation
import Testing
@testable import MontyKit

// The live view's protocol and the run's event stream, checked against what `montybot/liveview/wire.py` and
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
        #expect(object(.giveBack) == ["kind": "give_back"])
        #expect(object(.outline) == ["kind": "outline"])
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
    let login = APIClient.basicAuthorization(user: "montybot", password: "pa:ss")
    var client: APIClient { APIClient(baseURL: URL(string: "https://monty.test")!, siteLogin: login) }

    @Test func everyRequestCarriesIt() throws {
        #expect(login == "Basic " + Data("montybot:pa:ss".utf8).base64EncodedString())
        #expect(client.request("GET", "/api/me").value(forHTTPHeaderField: "Authorization") == login)
        let link = LiveLink(url: "/live/h1/", reason: "Sign in")
        let socket = try #require(client.liveSocketRequest(link))
        #expect(socket.value(forHTTPHeaderField: "Authorization") == login)
        #expect(APIClient(baseURL: URL(string: "https://monty.test")!).request("GET", "/").value(forHTTPHeaderField: "Authorization") == nil)
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
    }

    @Test func threadListStatus() throws {
        let json = #"[{"id": "a", "title": "Eggs", "status": "waiting"}, {"id": "b", "title": "Hi", "status": null}]"#
        let threads = try JSONDecoder().decode([ThreadSummary].self, from: Data(json.utf8))
        #expect(threads == [ThreadSummary(id: "a", title: "Eggs", status: .waiting), ThreadSummary(id: "b", title: "Hi")])
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
