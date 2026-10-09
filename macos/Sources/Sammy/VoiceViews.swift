import SammyKit
import SwiftUI

/// The composer's microphone: dictates into the message box, after what is typed there; tapping again stops. The
/// words land in the box for the user to read and send. Hidden when there is no way to dictate.
struct DictateButton: View {
    @Environment(AppModel.self) private var app
    let chat: ChatModel

    var body: some View {
        let voice = app.voice
        if voice.canDictate {
            let listening = voice.dictation == .listening
            let transcribing = voice.dictation == .transcribing
            Button {
                voice.toggleDictation(base: chat.draft) { [weak chat] text in chat?.draft = text }
            } label: {
                if transcribing {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: listening ? "stop.circle.fill" : "mic")
                        .font(.system(size: 14, weight: .medium))
                        .foregroundStyle(listening ? Palette.action : Palette.onSurfaceVariant)
                        .symbolEffect(.pulse, isActive: listening)
                }
            }
            .buttonStyle(IconButtonStyle(size: Metrics.control))
            .disabled(transcribing)
            .help(transcribing ? "Turning what you said into text" : listening ? "Stop dictating" : "Dictate")
            .accessibilityLabel(transcribing ? "Transcribing" : listening ? "Stop dictating" : "Dictate")
            .transition(.opacity)
        }
    }
}

/// Under a reply, beside Copy: reads it aloud, or stops. One reply is read at a time.
struct ReadAloudButton: View {
    @Environment(AppModel.self) private var app
    let text: String
    /// Shown on hover, as Copy is; always while it reads.
    let visible: Bool

    var body: some View {
        let voice = app.voice
        let reading = voice.reading == text
        Button { voice.toggleReading(text) } label: {
            HStack(spacing: 4) {
                if reading, voice.preparing { ProgressView().controlSize(.mini) }
                Label(reading ? "Stop reading" : "Read aloud", systemImage: reading ? "stop.fill" : "speaker.wave.2")
            }
            .font(.system(size: 11))
        }
        .buttonStyle(.sammy(.ghost, small: true))
        .opacity(visible || reading ? 1 : 0)
        .help(reading ? "Stop reading this reply" : "Read this reply aloud")
        .accessibilityHidden(true)  // the reply's own Read aloud action does this for VoiceOver
    }
}

/// What dictating or reading aloud has to tell the user, shown once above the open chat's composer.
struct VoiceNotices: ViewModifier {
    @Environment(AppModel.self) private var app
    let chat: ChatModel

    func body(content: Content) -> some View {
        content.onChange(of: app.voice.notice) { _, notice in
            guard let notice else { return }
            chat.notice = notice
            app.voice.notice = nil
        }
    }
}
