import AppKit
import SwiftUI

// Logfire's design system (platform: src/services/logfire-frontend/src/styles/design-tokens.css and index.css),
// so Monty looks like it belongs next to Logfire: neutral surfaces, hairline outlines, blue for actions, and
// Pydantic pink only for the brand and for "Monty needs you".

extension Color {
    /// A color from Logfire's HSL tokens, one for the light theme and one for the dark.
    init(light: (Double, Double, Double), dark: (Double, Double, Double)) {
        self.init(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let (h, s, l) = isDark ? dark : light
            return NSColor.hsl(h, s, l)
        })
    }

    init(gray light: Double, _ dark: Double) {
        self.init(light: (0, 0, light), dark: (0, 0, dark))
    }
}

extension NSColor {
    static func hsl(_ hue: Double, _ saturation: Double, _ lightness: Double) -> NSColor {
        let s = saturation / 100, l = lightness / 100
        let chroma = (1 - abs(2 * l - 1)) * s
        let h = hue / 60
        let x = chroma * (1 - abs(h.truncatingRemainder(dividingBy: 2) - 1))
        let (r, g, b): (Double, Double, Double) = switch h {
        case ..<1: (chroma, x, 0)
        case ..<2: (x, chroma, 0)
        case ..<3: (0, chroma, x)
        case ..<4: (0, x, chroma)
        case ..<5: (x, 0, chroma)
        default: (chroma, 0, x)
        }
        let m = l - chroma / 2
        return NSColor(srgbRed: r + m, green: g + m, blue: b + m, alpha: 1)
    }
}

enum Palette {
    // Surfaces
    static let surface = Color(gray: 97.6, 5)                       // --surface: the window
    static let container = Color(gray: 100, 7.5)                    // --surface-container: sidebar, cards, popovers
    static let containerLowest = Color(gray: 96, 11.8)              // --surface-container-lowest: hover, active row
    static let containerLow = Color(gray: 93.3, 14)
    static let containerHigh = Color(gray: 91, 17)                  // --muted, --secondary
    static let containerHighest = Color(gray: 88.6, 22.7)
    static let onSurface = Color(gray: 10.6, 88.2)                  // --foreground
    static let onSurfaceVariant = Color(light: (213.3, 4.5, 39), dark: (0, 0, 71))  // --muted-foreground
    static let outline = Color(light: (220, 8.8, 86.7), dark: (0, 0, 25))           // --border
    static let outlineVariant = Color(light: (216, 12, 92), dark: (0, 0, 18))
    static let outlineHover = Color(light: (220, 8, 77), dark: (0, 0, 37.3))

    // Actions
    static let link = Color(light: (215.3, 68.2, 53.1), dark: (210.5, 76.5, 51.6))  // --primary, --ring
    /// Logfire's blue a step deeper, for filled buttons and link text: white on it, and it on white, pass 4.5:1.
    static let action = Color(light: (215.3, 68.2, 45), dark: (210.5, 70, 45))
    static let actionText = Color(light: (215.3, 68.2, 43), dark: (210.5, 90, 68))
    static let onLink = Color(gray: 100, 100)

    // Pydantic pink (--logfire-600 light, --logfire-500 dark)
    static let logfire = Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            ? NSColor(srgbRed: 0.937, green: 0.290, blue: 0.945, alpha: 1)
            : NSColor(srgbRed: 0.898, green: 0.125, blue: 0.914, alpha: 1)
    })

    // States
    /// --states-error-variant: a filled destructive button, white text at 4.5:1.
    static let destructive = Color(light: (0, 74.6, 41.8), dark: (0, 65, 45))
    static let errorContainer = Color(light: (8.4, 100, 91.6), dark: (8, 28.3, 20.8))
    static let onErrorContainer = Color(light: (359.1, 100, 12.7), dark: (7.6, 100, 73.5))
    static let onWarningContainer = Color(light: (34.6, 100, 20.4), dark: (30.4, 100, 86.1))
}

enum Metrics {
    static let radius: CGFloat = 8          // --radius: 0.5rem
    static let radiusMedium: CGFloat = 6    // rounded-md
    static let control: CGFloat = 32        // buttons, fields (h-8)
    static let controlSmall: CGFloat = 28
    static let readingWidth: CGFloat = 720  // the chat's column
    static let gutter: CGFloat = 28         // the chat column's side padding
    /// The narrowest window: the floating sidebar, and beside it a chat that still reads. With Monty's browser open
    /// beside the chat too, the window makes room for it rather than clip them.
    static let windowMinWidth: CGFloat = 820
    static let windowWithBrowserMinWidth: CGFloat = 1140
}

extension Font {
    /// Logfire's mono, for labels, metadata, URLs and the activity trace.
    static func mono(_ size: CGFloat = 11, weight: Font.Weight = .regular) -> Font {
        .system(size: size, weight: weight, design: .monospaced)
    }
}

// MARK: - buttons, as Logfire's (components/shadcn/ui/button.tsx)

struct MontyButtonStyle: ButtonStyle {
    /// Blue is for acting; pink is never a button.
    enum Kind { case primary, outline, ghost, destructive }
    var kind: Kind = .primary
    var small = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        Styled(configuration: configuration, kind: kind, small: small, isEnabled: isEnabled)
    }

    private struct Styled: View {
        let configuration: Configuration
        let kind: Kind
        let small: Bool
        let isEnabled: Bool
        @State private var hovering = false

        var body: some View {
            configuration.label
                .font(.system(size: 13, weight: .medium))
                .lineLimit(1)
                .padding(.horizontal, small ? 10 : 14)
                .frame(height: small ? Metrics.controlSmall : Metrics.control)
                .foregroundStyle(foreground)
                .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(background))
                .overlay {
                    if kind == .outline {
                        RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                            .strokeBorder(hovering ? Palette.outlineHover : Palette.outline)
                    }
                }
                .shadow(color: .black.opacity(kind == .primary || kind == .outline ? 0.05 : 0), radius: 1, y: 1)
                .scaleEffect(configuration.isPressed && isEnabled ? 0.97 : 1)
                .opacity(isEnabled ? 1 : 0.5)
                .contentShape(RoundedRectangle(cornerRadius: Metrics.radiusMedium))
                .onHover { hovering = $0 }
                .animation(.easeOut(duration: 0.15), value: hovering)
                .animation(.easeOut(duration: 0.1), value: configuration.isPressed)
        }

        var foreground: Color {
            switch kind {
            case .primary: Palette.onLink
            case .outline, .ghost: Palette.onSurface
            case .destructive: .white
            }
        }

        var background: Color {
            let hover = hovering && isEnabled
            switch kind {
            case .primary: return Palette.action.opacity(hover ? 0.9 : 1)
            case .outline: return hover ? Palette.containerLowest : Palette.container
            case .ghost: return hover ? Palette.containerHighest.opacity(0.7) : .clear
            case .destructive: return Palette.destructive.opacity(hover ? 0.9 : 1)
            }
        }
    }
}

extension ButtonStyle where Self == MontyButtonStyle {
    static var primary: MontyButtonStyle { MontyButtonStyle(kind: .primary) }
    static var outline: MontyButtonStyle { MontyButtonStyle(kind: .outline) }
    static var ghost: MontyButtonStyle { MontyButtonStyle(kind: .ghost) }
    static var destructive: MontyButtonStyle { MontyButtonStyle(kind: .destructive) }
    static func monty(_ kind: MontyButtonStyle.Kind, small: Bool = false) -> MontyButtonStyle {
        MontyButtonStyle(kind: kind, small: small)
    }
}

/// A square icon button, as Logfire's `icon-minimal`. Give it an accessibility label: its icon is not one.
struct IconButtonStyle: ButtonStyle {
    var size: CGFloat = Metrics.controlSmall
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .medium))
            .foregroundStyle(Palette.onSurfaceVariant)
            .frame(width: size, height: size)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(hovering || configuration.isPressed ? Palette.containerHigh : .clear))
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
    }
}

// MARK: - badges, as Logfire's neutral badge (components/shadcn/ui/badge.tsx)

struct Badge: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 12, weight: .medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .foregroundStyle(Palette.onSurfaceVariant)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.containerHigh))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radiusMedium).strokeBorder(Palette.outline))
            .fixedSize()
    }
}

// MARK: - surfaces

struct Card: ViewModifier {
    var padding: CGFloat = 16
    var fill = Palette.container
    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.outline))
    }
}

extension View {
    func card(padding: CGFloat = 16, fill: Color = Palette.container) -> some View {
        modifier(Card(padding: padding, fill: fill))
    }

    /// The uppercase mono label Logfire puts over sections.
    func sectionLabel() -> some View {
        font(.mono(11, weight: .medium)).tracking(0.5).textCase(.uppercase).foregroundStyle(Palette.onSurfaceVariant)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A text field in Logfire's input style: a hairline outline that turns blue with the focus.
struct FieldStyle: ViewModifier {
    var focused: Bool
    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .font(.system(size: 13))
            .padding(.horizontal, 10)
            .frame(minHeight: Metrics.control)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.container))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radiusMedium)
                    .strokeBorder(focused ? Palette.link : Palette.outline, lineWidth: focused ? 1.5 : 1)
            )
            .animation(.easeOut(duration: 0.15), value: focused)
    }
}

extension View {
    func field(focused: Bool) -> some View { modifier(FieldStyle(focused: focused)) }
}

// MARK: - the Pydantic logomark (pydantic.dev/public/brand/pydantic-logomark.svg), drawn as vectors

struct PydanticMark: Shape {
    func path(in rect: CGRect) -> Path {
        // The logomark's corner points in its 138.4 × 120 viewBox; the facets are cut out with even-odd filling.
        let scale = min(rect.width / 138.4, rect.height / 120)
        let dx = rect.minX + (rect.width - 138.4 * scale) / 2
        let dy = rect.minY + (rect.height - 120 * scale) / 2
        func p(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: dx + x * scale, y: dy + y * scale) }
        var path = Path()
        path.addLines([p(69.2, 0.4), p(137.8, 92.6), p(69.2, 119.8), p(0.6, 92.6)])
        path.closeSubpath()
        path.addLines([p(69.2, 14.28), p(94.8, 49.785), p(69.2, 41.4), p(43.6, 49.79)])
        path.closeSubpath()
        path.addLines([p(33.03, 64.45), p(63.875, 54.35), p(63.875, 107.34), p(13.905, 90.975)])
        path.closeSubpath()
        path.addLines([p(74.53, 107.33), p(74.53, 54.35), p(105.375, 64.45), p(124.5, 90.96)])
        path.closeSubpath()
        return path
    }
}

struct Logomark: View {
    var size: CGFloat = 20
    var color: Color = Palette.logfire
    var body: some View {
        PydanticMark()
            .fill(color, style: FillStyle(eoFill: true))
            .frame(width: size, height: size * 120 / 138.4)
            .accessibilityHidden(true)
    }
}
