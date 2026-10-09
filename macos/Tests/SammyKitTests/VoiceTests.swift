import Foundation
import Testing
@testable import SammyKit

// Voice: the server's three calls (through the stand-in server, `Stub`), replies as words to say, and the setting.

@Suite struct VoiceTests {
    @Test func theServerSaysWhatItsProviderCanDo() async throws {
        let stub = Stub(settings: "{}")
        #expect(try await stub.client.voice() == VoiceSupport(transcribe: true, speak: false))
        #expect(try JSONDecoder().decode(VoiceSupport.self, from: Data(#"{"transcribe": false, "speak": true}"#.utf8)) == VoiceSupport(speak: true))
    }

    @Test func aRecordingIsSentAsItIs() async throws {
        let stub = Stub(settings: "{}")
        let audio = Data([0, 0, 0, 0x18, 0x66, 0x74, 0x79, 0x70])  // the start of an .m4a
        #expect(try await stub.client.transcribe(audio: audio) == "Order eggs")
        let sent = try #require(stub.requests.first { $0.url?.path == "/api/voice/transcriptions" })
        #expect(sent.httpMethod == "POST")
        #expect(sent.value(forHTTPHeaderField: "Content-Type") == "audio/mp4")  // not JSON, not multipart
        #expect(stub.bodies["/api/voice/transcriptions"] == audio)
        let request = stub.client.transcriptionRequest(audio: audio, mediaType: "audio/mp4")
        #expect(request.httpBody == audio && request.timeoutInterval == 60)
    }

    @Test func aReplyComesBackAsAudio() async throws {
        let stub = Stub(settings: "{}")
        #expect(try await stub.client.speech("Your eggs are ordered.") == Data("ID3 not really an MP3".utf8))
        let sent = try #require(stub.requests.first { $0.url?.path == "/api/voice/speech" })
        #expect(sent.httpMethod == "POST")
        #expect(sent.value(forHTTPHeaderField: "Accept") == "audio/mpeg")
        #expect(sent.value(forHTTPHeaderField: "Content-Type") == "application/json")
        let body = try #require(stub.bodies["/api/voice/speech"])
        #expect(try JSONSerialization.jsonObject(with: body) as? [String: String] == ["text": "Your eggs are ordered."])
    }

    @Test func aProviderThatFailsSaysWhy() async {
        let stub = Stub(settings: "{}")
        await #expect(throws: APIError.server(status: 502, detail: "the speech provider is down")) {
            _ = try await stub.client.speech("fail")
        }
    }

    @Test func repliesAreReadAsWordsNotMarkdown() {
        let reply = """
        # Done

        Your order is **confirmed**: [#1](http://shop.test/orders/1). Use `snake_case` and _emphasis_ ~~not this~~

        - eggs
        - milk, *semi-skimmed*

        | Item | Price |
        |---|---|
        | Eggs | €3 |

        ---
        """
        #expect(MarkdownBlock.spoken(reply) == """
        Done.
        Your order is confirmed: #1. Use snake_case and emphasis not this.
        eggs.
        milk, semi-skimmed.
        Item: Eggs, Price: €3.
        """)
        #expect(MarkdownBlock.spoken("Is that all?") == "Is that all?")
        #expect(MarkdownBlock.spoken("---\n\n") == "")
    }

    @Test func dictationGoesAfterWhatIsTyped() {
        #expect(Voice.appended("order eggs", to: "") == "order eggs")
        #expect(Voice.appended("order eggs", to: "Hi Sammy,  ") == "Hi Sammy, order eggs")
        #expect(Voice.appended("  ", to: "Hi") == "Hi")
    }

    @MainActor @Test func readingRepliesAloudIsKept() throws {
        let defaults = try #require(UserDefaults(suiteName: "sammy-test-\(UUID().uuidString)"))
        #expect(!Voice(defaults: defaults).readRepliesAloud)  // off until the user turns it on
        Voice(defaults: defaults).readRepliesAloud = true
        #expect(Voice(defaults: defaults).readRepliesAloud)
    }

    @MainActor @Test func withoutAServerThereIsNoDictation() throws {
        let defaults = try #require(UserDefaults(suiteName: "sammy-test-\(UUID().uuidString)"))
        let voice = Voice(defaults: defaults)
        #expect(!voice.canDictate && voice.support == .none)
        voice.read("")  // nothing to say: nothing plays
        #expect(voice.reading == nil)
    }
}
