import AVFoundation
import Foundation
import Observation
@preconcurrency import OpenTelemetryApi
@preconcurrency import Speech

// Voice: the user dictates a message, and hears Sammy's replies. Dictation is Apple's speech recognition on this Mac
// when it can run here, otherwise the server's provider (`POST /api/voice/transcriptions`) with a recording that is
// deleted as soon as it is sent; replies are read by the server's provider (`POST /api/voice/speech`, MP3) when it
// has one, otherwise by the Mac's own voice. Provider keys stay on the server, and no audio is kept or traced: spans
// carry what was heard or read only as `content` (its length, and the text only when the server allows content).

/// What the server's speech provider can do, `GET /api/voice`.
public struct VoiceSupport: Decodable, Equatable, Sendable {
    /// It turns a recording into text.
    public let transcribe: Bool
    /// It reads text aloud.
    public let speak: Bool

    public init(transcribe: Bool = false, speak: Bool = false) {
        self.transcribe = transcribe
        self.speak = speak
    }

    /// An older server, or one without a provider.
    public static let none = VoiceSupport()
}

extension MarkdownBlock {
    /// A reply as words to say: no Markdown marks, links as their text, list items and table rows as sentences.
    public static func spoken(_ markdown: String) -> String {
        parse(markdown).compactMap { block -> String? in
            switch block {
            case .paragraph(let text), .quote(let text): return sentence(inline(text))
            case .heading(_, let text): return sentence(inline(text))
            case .bullet(let items), .numbered(let items): return items.map { sentence(inline($0)) }.joined(separator: "\n")
            case .code(let text): return text.trimmingCharacters(in: .whitespacesAndNewlines)
            case .table(let header, let rows):
                return rows.map { row in
                    sentence(zip(header + Array(repeating: "", count: max(row.count - header.count, 0)), row).map { name, cell in
                        let name = inline(name), cell = inline(cell)
                        return name.isEmpty ? cell : "\(name): \(cell)"
                    }.joined(separator: ", "))
                }.joined(separator: "\n")
            case .rule: return nil
            }
        }
        .filter { !$0.isEmpty }
        .joined(separator: "\n")
    }

    /// Inline marks off: links and pictures as their text, emphasis, strike-through and code marks gone.
    static func inline(_ text: String) -> String {
        text
            .replacingOccurrences(of: #"!?\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"\*+|~~|`+"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"(?<![\p{L}\p{N}])_+|_+(?![\p{L}\p{N}])"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    /// Ends with a full stop unless it ends with punctuation already, so a voice pauses between items.
    static func sentence(_ text: String) -> String {
        guard let last = text.last, last.isLetter || last.isNumber else { return text }
        return text + "."
    }
}

/// Dictation and reading replies aloud, for the whole app: one reply is read at a time, and one dictation runs at a time.
@MainActor
@Observable
public final class Voice {
    public enum Dictation: Equatable, Sendable {
        case idle
        /// The microphone is on.
        case listening
        /// The recording is with the server, being turned into text.
        case transcribing
    }

    /// What the server can do; nothing until it says.
    public private(set) var support = VoiceSupport.none
    public private(set) var dictation = Dictation.idle
    /// The reply being read aloud, as written (its Markdown), so its button says Stop reading.
    public private(set) var reading: String?
    /// The reply's audio is still on its way from the server.
    public private(set) var preparing = false
    /// Something the user should know (the microphone is off, the server couldn't transcribe): the chat shows it once.
    public var notice: ChatNotice?
    /// A reply to a task the user is watching is read aloud when the task is done. Kept on this Mac.
    public var readRepliesAloud: Bool {
        didSet { defaults.set(readRepliesAloud, forKey: "readRepliesAloud") }
    }

    /// The most the server takes for a recording.
    public static let maxRecordingBytes = 10 * 1024 * 1024
    /// The most text the server reads aloud at once.
    public static let maxSpokenCharacters = 20_000

    @ObservationIgnored private let defaults: UserDefaults
    @ObservationIgnored private var client: APIClient?
    @ObservationIgnored private var connections = 0
    /// Counts dictations started and stopped, so a late result from one that is over is ignored.
    @ObservationIgnored private var dictations = 0
    @ObservationIgnored private var dictated: (base: String, update: @MainActor (String) -> Void)?
    @ObservationIgnored private var heard = ""
    @ObservationIgnored private var dictationStarted = Date()
    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var recognition: (request: SFSpeechAudioBufferRecognitionRequest, task: SFSpeechRecognitionTask)?
    @ObservationIgnored private var recorder: AVAudioRecorder?
    /// Counts readings started and stopped, so a reading that is over never plays.
    @ObservationIgnored private var readings = 0
    @ObservationIgnored private var player: AVAudioPlayer?
    @ObservationIgnored private var synthesizer: AVSpeechSynthesizer?
    @ObservationIgnored private var observer: PlaybackObserver?

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        readRepliesAloud = defaults.bool(forKey: "readRepliesAloud")
    }

    private var telemetry: Telemetry? { client?.telemetry }

    // MARK: the server

    /// Signed in to `client`'s server: ask it what its provider can do.
    func connect(_ client: APIClient) {
        cancel()
        connections += 1
        let connection = connections
        self.client = client
        support = .none
        Task {
            let support = (try? await client.voice()) ?? .none
            guard connection == connections else { return }
            self.support = support
        }
    }

    /// Signed out, or moved to another server.
    func disconnect() {
        cancel()
        connections += 1
        client = nil
        support = .none
        notice = nil
    }

    /// Stops listening and reading, for good: the chat closed. A recording not sent yet is deleted, not sent.
    public func cancel() {
        dictations += 1
        endListening()
        discardRecording()
        dictation = .idle
        stopReading()
    }

    // MARK: dictation

    /// Microphone and speech prompts need the app's Info.plist, which `swift run` has none of.
    private var isBundled: Bool { Bundle.main.bundleIdentifier != nil }

    /// Apple's recognizer for the user's language, when there is one and the user hasn't turned it off.
    private var recognizer: SFSpeechRecognizer? {
        guard isBundled, ![.denied, .restricted].contains(SFSpeechRecognizer.authorizationStatus()) else { return nil }
        if languageRecognizer == nil { languageRecognizer = .some(SFSpeechRecognizer()) }  // made once: the view asks often
        guard let recognizer = languageRecognizer ?? nil, recognizer.isAvailable else { return nil }
        return recognizer
    }
    @ObservationIgnored private var languageRecognizer: SFSpeechRecognizer??

    /// Whether there is a way to dictate: on this Mac, or through the server.
    public var canDictate: Bool { isBundled && client != nil && (support.transcribe || recognizer != nil) }

    /// Starts dictating into the composer, or stops. `update` gets the composer's new text: `base` (what was typed
    /// before) and what was heard, live while Apple's recognizer listens, or once the server has transcribed it.
    public func toggleDictation(base: String, update: @escaping @MainActor (String) -> Void) {
        switch dictation {
        case .listening: finishDictation()
        case .transcribing: return
        case .idle: Task { await startDictation(base: base, update: update) }
        }
    }

    private func startDictation(base: String, update: @escaping @MainActor (String) -> Void) async {
        guard dictation == .idle, client != nil else { return }
        stopReading()  // it would be dictated too
        dictations += 1
        let attempt = dictations
        dictation = .listening
        dictated = (base, update)
        heard = ""
        dictationStarted = Date()
        guard await Self.microphoneAllowed() else {
            return failDictation(attempt, "Sammy can't hear you. Allow the microphone for Sammy in System Settings > Privacy & Security > Microphone.")
        }
        // On this Mac when it can recognize speech without the network; otherwise the server; otherwise Apple's service.
        let recognizer = self.recognizer
        let onDevice = recognizer?.supportsOnDeviceRecognition == true
        let useRecognizer = recognizer != nil && (onDevice || !support.transcribe)
        if useRecognizer, let recognizer, await Self.speechRecognitionAllowed() {
            guard attempt == dictations else { return }
            do {
                try listen(with: recognizer, onDevice: onDevice, attempt: attempt)
                return
            } catch {
                endListening()
                if !support.transcribe { return failDictation(attempt, "Couldn't start dictation. Check your microphone and try again.") }
            }
        }
        guard attempt == dictations else { return }
        guard support.transcribe else {
            return failDictation(attempt, "Dictation is off. Allow Speech Recognition for Sammy in System Settings > Privacy & Security.")
        }
        do {
            try record()
        } catch {
            discardRecording()
            failDictation(attempt, "Couldn't start recording. Check your microphone and try again.")
        }
    }

    private func failDictation(_ attempt: Int, _ text: String) {
        guard attempt == dictations else { return }
        dictation = .idle
        dictated = nil
        notice = .error(text)
    }

    /// The user is done talking: Apple's recognizer gives its last words, or the recording goes to the server.
    private func finishDictation() {
        if let recognition {
            recognition.request.endAudio()  // the final result follows, then `heard(...)` ends it
            engine?.stop()
            engine?.inputNode.removeTap(onBus: 0)
            engine = nil
            if heard.isEmpty { endDictation(dictations) }
        } else if recorder != nil {
            Task { await transcribe(attempt: dictations) }
        } else {
            dictations += 1
            dictation = .idle
        }
    }

    // On this Mac, with Apple's speech recognition.

    private func listen(with recognizer: SFSpeechRecognizer, onDevice: Bool, attempt: Int) throws {
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        request.requiresOnDeviceRecognition = onDevice
        request.addsPunctuation = true
        let engine = AVAudioEngine()
        try Self.start(engine, feeding: request)
        self.engine = engine
        let task = Self.recognize(request, with: recognizer) { [weak self] text, final, failed in
            Task { @MainActor in self?.heard(text, final: final, failed: failed, attempt: attempt) }
        }
        recognition = (request, task)
    }

    private func heard(_ text: String?, final: Bool, failed: Bool, attempt: Int) {
        guard attempt == dictations, let dictated else { return }
        if let text, !text.isEmpty {
            heard = text
            dictated.update(Self.appended(text, to: dictated.base))
        }
        if final || failed {
            if failed, heard.isEmpty, dictation == .listening, engine != nil {
                notice = .error("Couldn't make out any words. Try again, a little closer to the microphone.")
            }
            endDictation(attempt)
        }
    }

    private func endDictation(_ attempt: Int) {
        guard attempt == dictations else { return }
        let words = heard
        telemetry?.log("dictate", ["sammy.voice.engine": .string("device")], start: dictationStarted) {
            $0.content("sammy.transcript", words)
        }
        dictations += 1
        endListening()
        dictation = .idle
        dictated = nil
    }

    private func endListening() {
        engine?.stop()
        engine?.inputNode.removeTap(onBus: 0)
        engine = nil
        recognition?.task.cancel()
        recognition = nil
    }

    /// The tap runs on the audio thread: it is made here, outside the main actor, so it is not tied to it.
    nonisolated private static func start(_ engine: AVAudioEngine, feeding request: SFSpeechAudioBufferRecognitionRequest) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw APIError.unexpected("no microphone") }
        nonisolated(unsafe) let request = request
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in request.append(buffer) }
        engine.prepare()
        try engine.start()
    }

    nonisolated private static func recognize(_ request: SFSpeechAudioBufferRecognitionRequest, with recognizer: SFSpeechRecognizer,
                                              _ heard: @escaping @Sendable (String?, Bool, Bool) -> Void) -> SFSpeechRecognitionTask {
        recognizer.recognitionTask(with: request) { result, error in
            heard(result?.bestTranscription.formattedString, result?.isFinal ?? false, error != nil)
        }
    }

    // Through the server: a recording (AAC in .m4a), deleted as soon as it is read to send.

    private func record() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "sammy-dictation-\(UUID().uuidString).m4a")
        let recorder = try AVAudioRecorder(url: url, settings: [
            AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32_000,
        ])
        self.recorder = recorder
        // 15 minutes is about 4 MB: well under what the server takes.
        guard recorder.record(forDuration: 15 * 60) else { throw APIError.unexpected("the recorder didn't start") }
    }

    private func transcribe(attempt: Int) async {
        guard let recorder, let client, let dictated else { return }
        recorder.stop()
        self.recorder = nil
        dictation = .transcribing
        let audio = try? Data(contentsOf: recorder.url)
        try? FileManager.default.removeItem(at: recorder.url)  // the recording goes nowhere but the request
        guard let audio, !audio.isEmpty else { return failDictation(attempt, "Nothing was recorded. Check your microphone and try again.") }
        guard audio.count <= Self.maxRecordingBytes else { return failDictation(attempt, "That recording is too long. Try a shorter one.") }
        let attributes: [String: AttributeValue?] = ["sammy.voice.engine": .string("server"), "sammy.audio.size": .int(audio.count)]
        let result: Result<String, Error> = await client.telemetry.action("dictate", attributes) { span in
            do {
                let text = try await client.transcribe(audio: audio)
                span.content("sammy.transcript", text)
                return .success(text)
            } catch {
                span.fail(error)
                return .failure(error)
            }
        }
        guard attempt == dictations else { return }
        switch result {
        case .success(let text):
            dictation = .idle
            self.dictated = nil
            let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if words.isEmpty { notice = .info("Couldn't make out any words. Try again.") } else { dictated.update(Self.appended(words, to: dictated.base)) }
        case .failure(let error as APIError) where error.status == 404:
            failDictation(attempt, "Sammy's server can't transcribe speech right now.")
        case .failure(let error):
            failDictation(attempt, "Couldn't transcribe that: \(error.localizedDescription)")
        }
    }

    private func discardRecording() {
        guard let recorder else { return }
        recorder.stop()
        try? FileManager.default.removeItem(at: recorder.url)
        self.recorder = nil
    }

    /// What the composer says after dictating: what was there, then what was heard.
    nonisolated static func appended(_ heard: String, to base: String) -> String {
        let base = base.trimmingCharacters(in: .whitespacesAndNewlines)
        let heard = heard.trimmingCharacters(in: .whitespacesAndNewlines)
        if base.isEmpty || heard.isEmpty { return base.isEmpty ? heard : base }
        return base + (base.last?.isNewline == true ? "" : " ") + heard
    }

    nonisolated private static func microphoneAllowed() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
    }

    nonisolated private static func speechRecognitionAllowed() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .notDetermined:
            return await withCheckedContinuation { done in
                SFSpeechRecognizer.requestAuthorization { done.resume(returning: $0 == .authorized) }
            }
        default: return false
        }
    }

    // MARK: reading aloud

    /// Reads the reply aloud, or stops if it is the one being read.
    public func toggleReading(_ reply: String) {
        if reading == reply { stopReading() } else { read(reply) }
    }

    /// Reads the reply aloud, as plain words, instead of anything being read now.
    public func read(_ reply: String) {
        stopReading()
        guard dictation == .idle else { return }  // the microphone would hear it
        let text = String(MarkdownBlock.spoken(reply).prefix(Self.maxSpokenCharacters))
        guard !text.isEmpty else { return }
        readings += 1
        let reading = readings
        self.reading = reply
        guard support.speak, let client else { return speakHere(text, reading) }
        preparing = true
        Task { await speakFromServer(text, reading, client) }
    }

    public func stopReading() {
        readings += 1
        player?.stop()
        player = nil
        synthesizer?.stopSpeaking(at: .immediate)
        observer = nil
        reading = nil
        preparing = false
    }

    private func speakFromServer(_ text: String, _ reading: Int, _ client: APIClient) async {
        let audio: Data? = await client.telemetry.action("read aloud", ["sammy.voice.engine": .string("server")]) { span in
            span.content("sammy.reply", text)
            do {
                return try await client.speech(text)
            } catch {
                span.fail(error)
                return nil
            }
        }
        guard reading == readings else { return }
        preparing = false
        // The server's voice, or this Mac's when the server couldn't.
        guard let audio, let player = try? AVAudioPlayer(data: audio) else { return speakHere(text, reading, logged: false) }
        let observer = observer(for: reading)
        player.delegate = observer
        self.observer = observer
        self.player = player
        if !player.play() { speakHere(text, reading, logged: false) }
    }

    private func speakHere(_ text: String, _ reading: Int, logged: Bool = true) {
        player = nil
        if logged {
            telemetry?.log("read aloud", ["sammy.voice.engine": .string("device")]) { $0.content("sammy.reply", text) }
        }
        let synthesizer = synthesizer ?? AVSpeechSynthesizer()
        self.synthesizer = synthesizer
        let observer = observer(for: reading)
        synthesizer.delegate = observer
        self.observer = observer
        synthesizer.speak(AVSpeechUtterance(string: text))
    }

    private func observer(for reading: Int) -> PlaybackObserver {
        PlaybackObserver { [weak self] in
            Task { @MainActor in
                guard let self, reading == self.readings else { return }
                self.player = nil
                self.observer = nil
                self.reading = nil
            }
        }
    }

    // MARK: replies

    /// A task the user is watching finished with `reply`: read it aloud if they asked for that.
    func replyFinished(_ reply: String) {
        guard readRepliesAloud, dictation == .idle else { return }
        read(reply)
    }
}

/// Says when playing or speaking ends. Players and synthesizers call it on threads of their own.
private final class PlaybackObserver: NSObject, AVAudioPlayerDelegate, AVSpeechSynthesizerDelegate, @unchecked Sendable {
    let ended: @Sendable () -> Void

    init(_ ended: @escaping @Sendable () -> Void) { self.ended = ended }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) { ended() }
    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) { ended() }
    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) { ended() }
}

extension AppModel {
    /// The open chat's task finished `done` while the user watched it (the window on screen): its reply may be read.
    func replyFinished(in chat: ChatModel, _ reply: String) {
        guard self.chat === chat, isWindowVisible else { return }
        voice.replyFinished(reply)
    }
}
