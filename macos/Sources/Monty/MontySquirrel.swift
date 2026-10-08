import ImageIO
import SwiftUI

/// Monty's squirrel: the flying-squirrel mascot rendered in `macos/mascot`, playing a loop for each mood. Getting to work
/// starts with a quick "got it" nod; a finished or failed task plays its reaction once, then settles back into idle. Every
/// loop starts and ends on the same pose, so the hand-offs are seamless. Under Reduce Motion it holds one telling frame
/// of each loop instead. Without the loops in the app's resources (a bare `swift run`) it falls back to the prism.
///
/// The loops leave the top of the frame free for the effects (`?`, the check, the typing dots), so only the lower
/// `layoutHeight` takes up room; the rest overflows upward, over whatever is above, and never takes a click.
struct MontySquirrel: View {
    var mood: MontyMark.Mood = .idle
    var size: CGFloat = 64
    var layoutHeight: CGFloat?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var playback = Playback(clip: .idle)

    var body: some View {
        if SquirrelClip.isBundled {
            TimelineView(.animation(minimumInterval: 1 / SquirrelClip.fps, paused: reduceMotion)) { context in
                if let frame = frame(at: context.date) {
                    Image(decorative: frame, scale: 1)
                        .resizable()
                        .interpolation(.high)
                        .frame(width: size, height: size)
                }
            }
            .frame(width: size, height: size)
            .frame(height: layoutHeight ?? size, alignment: .bottom)
            .allowsHitTesting(false)
            .onAppear { playback = Playback(clip: .steady(for: mood)) }
            .onChange(of: mood) { old, new in playback = Playback(clip: .entering(new, from: old)) }
            .task(id: playback) {
                // A one-shot reaction hands back to the mood's loop once it has played through.
                guard playback.clip.isOneShot else { return }
                try? await Task.sleep(for: playback.clip.duration)
                guard !Task.isCancelled else { return }
                playback = Playback(clip: .steady(for: mood))
            }
            .accessibilityHidden(true)
        } else {
            MontyMark(mood: mood, size: size * 0.45).frame(width: size, height: layoutHeight ?? size)
        }
    }

    private func frame(at date: Date) -> CGImage? {
        let frames = playback.clip.frames
        guard !frames.isEmpty else { return nil }
        if reduceMotion { return frames[frames.count / 3] }  // past the wind-up, where the pose says the mood
        // Never before the start: the timeline's date can be a moment earlier than a reaction that just began, and a
        // negative index (Swift's % keeps the sign) would be out of range.
        let index = max(0, Int(date.timeIntervalSince(playback.started) * SquirrelClip.fps))
        return frames[playback.clip.isOneShot ? min(index, frames.count - 1) : index % frames.count]
    }
}

private struct Playback: Equatable {
    var clip: SquirrelClip
    var started = Date.now
}

/// One pre-rendered loop: `Resources/Squirrel/montysquirrel_<clip>.png`, an APNG with full alpha.
enum SquirrelClip: String, CaseIterable {
    case idle, ack, thinking, question, success, failed

    /// The rate `macos/mascot` renders at.
    static let fps: Double = 24

    /// Reactions that play once; the rest loop for as long as the mood lasts.
    var isOneShot: Bool { self == .ack || self == .success || self == .failed }

    @MainActor var duration: Duration { .seconds(Double(frames.count) / Self.fps) }

    /// The loop for a mood that has settled.
    static func steady(for mood: MontyMark.Mood) -> SquirrelClip {
        switch mood {
        case .working: .thinking
        case .waiting: .question
        case .idle, .done, .failed: .idle
        }
    }

    /// What to play the moment the mood changes: a reaction if there is one, else the mood's loop.
    static func entering(_ mood: MontyMark.Mood, from old: MontyMark.Mood) -> SquirrelClip {
        switch mood {
        case .working: old == .working ? .thinking : .ack
        case .done: .success
        case .failed: .failed
        case .idle, .waiting: steady(for: mood)
        }
    }

    @MainActor static var isBundled: Bool { !SquirrelClip.idle.frames.isEmpty }

    /// Decoded once per clip and shared by every squirrel on screen.
    @MainActor var frames: [CGImage] {
        if let cached = Self.cache[self] { return cached }
        let frames = Self.decode(self)
        Self.cache[self] = frames
        return frames
    }

    @MainActor private static var cache: [SquirrelClip: [CGImage]] = [:]

    private static func decode(_ clip: SquirrelClip) -> [CGImage] {
        guard let url = Bundle.main.url(forResource: "montysquirrel_\(clip.rawValue)", withExtension: "png", subdirectory: "Squirrel"),
              let source = CGImageSourceCreateWithURL(url as CFURL, nil)
        else { return [] }
        return (0 ..< CGImageSourceGetCount(source)).compactMap { CGImageSourceCreateImageAtIndex(source, $0, nil) }
    }
}
