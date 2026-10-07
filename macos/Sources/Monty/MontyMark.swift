import SwiftUI

/// Monty's mascot: the Pydantic prism in Pydantic pink, alive. It breathes while idle, a light sweeps its facets while
/// Monty works, a soft halo calls while Monty needs you, it hops once when a task is done and shakes once when one
/// fails. Under Reduce Motion it holds still and only its colour and halo say the state.
struct MontyMark: View {
    enum Mood: Equatable { case idle, working, waiting, done, failed }

    var mood: Mood = .idle
    var size: CGFloat = 20
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hop = 0
    @State private var shake = 0

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: reduceMotion || mood == .done || mood == .failed)) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            ZStack {
                if mood == .waiting {
                    Circle()
                        .fill(Palette.logfire.opacity(0.18))
                        .frame(width: size * 1.7, height: size * 1.7)
                        .scaleEffect(reduceMotion ? 1 : 0.85 + 0.15 * wave(t, period: 1.6))
                        .opacity(reduceMotion ? 0.8 : 0.4 + 0.6 * wave(t, period: 1.6))
                }
                mark
                    .overlay {
                        if mood == .working, !reduceMotion {
                            sweep(t).mask(mark)  // light passing over the facets
                        }
                    }
                    .scaleEffect(mood == .idle && !reduceMotion ? 1 + 0.035 * wave(t, period: 3.2) : 1)
                    .offset(y: mood == .working && !reduceMotion ? -size * 0.04 * wave(t, period: 0.9) : 0)
            }
            .frame(width: size * 1.7, height: size * 1.7)
        }
        .keyframeAnimator(initialValue: CGFloat(0), trigger: hop) { view, lift in
            view.offset(y: -lift)
        } keyframes: { _ in
            SpringKeyframe(size * 0.28, duration: 0.18, spring: .snappy)
            SpringKeyframe(0, duration: 0.45, spring: .bouncy)
        }
        .keyframeAnimator(initialValue: CGFloat(0), trigger: shake) { view, x in
            view.offset(x: x)
        } keyframes: { _ in
            KeyframeTrack {
                LinearKeyframe(size * 0.12, duration: 0.06)
                LinearKeyframe(-size * 0.12, duration: 0.1)
                LinearKeyframe(size * 0.06, duration: 0.08)
                LinearKeyframe(0, duration: 0.08)
            }
        }
        .onChange(of: mood) { _, mood in
            guard !reduceMotion else { return }
            if mood == .done { hop += 1 }
            if mood == .failed { shake += 1 }
        }
        .accessibilityHidden(true)
    }

    private var mark: some View {
        PydanticMark()
            .fill(mood == .failed ? Palette.onSurfaceVariant : Palette.logfire, style: FillStyle(eoFill: true))
            .frame(width: size, height: size * 120 / 138.4)
    }

    private func sweep(_ t: TimeInterval) -> some View {
        let phase = (t.truncatingRemainder(dividingBy: 1.4)) / 1.4  // 0...1 every 1.4 s
        return LinearGradient(
            stops: [.init(color: .clear, location: 0), .init(color: .white.opacity(0.75), location: 0.5), .init(color: .clear, location: 1)],
            startPoint: .leading, endPoint: .trailing
        )
        .frame(width: size * 0.6)
        .rotationEffect(.degrees(20))
        .offset(x: size * (phase * 2.2 - 1.1))
    }

    /// 0...1 and back, smoothly, every `period` seconds.
    private func wave(_ t: TimeInterval, period: Double) -> Double { 0.5 - 0.5 * cos(t / period * 2 * .pi) }
}

extension View {
    /// An animation, unless the user asked for less motion.
    func motion<V: Equatable>(_ animation: Animation, value: V) -> some View {
        modifier(MotionModifier(animation: animation, value: value))
    }
}

private struct MotionModifier<V: Equatable>: ViewModifier {
    let animation: Animation
    let value: V
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func body(content: Content) -> some View { content.animation(reduceMotion ? nil : animation, value: value) }
}

extension AnyTransition {
    /// How things arrive in the chat: up a little, fading in, on a soft spring.
    static var arrive: AnyTransition {
        .asymmetric(insertion: .opacity.combined(with: .offset(y: 10)).combined(with: .scale(scale: 0.985, anchor: .bottom)),
                    removal: .opacity)
    }
}
