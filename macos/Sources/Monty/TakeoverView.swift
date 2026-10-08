import AppKit
import MontyKit
import SwiftUI

/// The user drives Monty's browser: the page fills the window, and everything they do goes to it.
struct TakeoverView: View {
    let chat: ChatModel
    let live: LiveSession
    /// Bumped to give the page the keyboard back, after the address bar.
    @State private var pageFocus = 0
    /// Bumped to edit the address: ⌘L, or a new tab.
    @State private var addressFocus = 0

    var body: some View {
        VStack(spacing: 0) {
            bar
            Divider().overlay(Palette.outline)
            if !live.tabs.isEmpty {
                TabStrip(live: live, newTab: newTab)
                Divider().overlay(Palette.outline)
            }
            ZStack {
                Palette.surface
                if let frame = live.frame {
                    LiveCanvas(
                        frame: frame, live: live, host: live.activeTab.flatMap { URLComponents(string: $0.url)?.host } ?? "",
                        focus: pageFocus
                    )
                        .aspectRatio(frame.width / frame.height, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusMedium))
                        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusMedium).strokeBorder(Palette.outline))
                        .padding(12)
                        .opacity(live.state == .driving && !live.givingBack ? 1 : 0.35)
                        .allowsHitTesting(live.state == .driving && !live.givingBack)
                }
                stateOverlay
            }
            if let notice = live.notice {
                Label(notice, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 12))
                    .foregroundStyle(Palette.onWarningContainer)
                    .padding(.bottom, 8)
            }
        }
        .background(Palette.surface)
        .background(BrowserShortcuts(perform: perform))
        .onChange(of: live.notice) { _, text in if let text { AccessibilityNotification.Announcement(text).post() } }
        .onChange(of: live.state) { _, state in
            if live.signedOut { chat.liveSignedOut(); return }
            guard case .ended(let givenBack) = state else { return }
            if givenBack { chat.notice = .info("Thanks. Monty has the browser again and is carrying on.") }
            Task { await chat.liveViewEnded() }
        }
    }

    private var bar: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text("You're in control of Monty's browser").font(.system(size: 13, weight: .semibold))
                Text(live.reason).font(.system(size: 12)).foregroundStyle(Palette.onSurfaceVariant).lineLimit(1)
            }
            .frame(minWidth: 140, maxWidth: 360, alignment: .leading)
            Spacer(minLength: 12)
            if live.activeTab != nil {
                HStack(spacing: 6) {
                    NavigationButtons(live: live)
                    AddressField(live: live, focus: addressFocus) { pageFocus += 1 }
                }
            }
            Spacer(minLength: 12)
            Button("Not now") { chat.leaveLiveView() }
                .buttonStyle(.monty(.outline, small: true))
                .keyboardShortcut("t", modifiers: [.command, .shift])
                .help("Close this view (⇧⌘T). Monty keeps waiting until you take over again and hand it back.")
                .layoutPriority(1)
            Button {
                live.giveBack()
            } label: {
                HStack(spacing: 6) {
                    if live.givingBack { ProgressView().controlSize(.mini).tint(Palette.onLink) }
                    Text("I'm done")
                }
            }
            .buttonStyle(.monty(.primary, small: true))
            .disabled(live.state != .driving || live.givingBack)
            .keyboardShortcut(.return, modifiers: [.command])
            .layoutPriority(1)
            .help("Hand the browser back; Monty carries on from here (⌘↩). ⌘V pastes from your Mac.")
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Palette.container)
    }

    /// A blank tab, with the address bar ready to type into.
    private func newTab() {
        live.newTab()
        addressFocus += 1
    }

    /// The browser's shortcuts, while the user can drive; otherwise the keys do what they always do.
    private func perform(_ shortcut: BrowserShortcut) -> Bool {
        guard live.canDrive else { return false }
        switch shortcut {
        case .editAddress: addressFocus += 1
        case .newTab: if live.controls { newTab() }
        default: live.perform(shortcut)
        }
        return true
    }

    @ViewBuilder private var stateOverlay: some View {
        switch live.state {
        case .connecting:
            message(spinner: true, "Opening Monty's browser…")
        case .reconnecting:
            message(spinner: true, "Connection lost. Reconnecting…")
        case .elsewhere:
            VStack(spacing: 12) {
                message("You opened this browser somewhere else.")
                Button("Use it here") { live.reconnect() }.buttonStyle(.primary)
            }
        case .ended:
            EmptyView()
        case .failed(let text):
            VStack(spacing: 12) {
                message(icon: "exclamationmark.triangle", text)
                HStack {
                    Button("Try again") { Task { await chat.retryTakeOver() } }.buttonStyle(.primary)
                    Button("Close") { Task { await chat.liveViewEnded() } }.buttonStyle(.outline)
                }
            }
        case .driving:
            if live.frame == nil { message(spinner: true, "Waiting for the page…") }
        }
    }

    private func message(spinner: Bool = false, icon: String? = nil, _ text: String) -> some View {
        HStack(spacing: 10) {
            if spinner { MontyMark(mood: .working, size: 14) }
            if let icon { Image(systemName: icon).foregroundStyle(Palette.onSurfaceVariant).accessibilityHidden(true) }
            Text(text).font(.system(size: 13, weight: .medium))
        }
        .card(padding: 14)
    }
}

/// The remote page, drawn from the live view's frames, taking the mouse and keyboard.
struct LiveCanvas: NSViewRepresentable {
    let frame: LiveFrame
    let live: LiveSession
    let host: String
    /// A new value gives the page the keyboard.
    var focus = 0

    func makeNSView(context: Context) -> CanvasView {
        let view = CanvasView()
        view.live = live
        view.focus = focus
        DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        return view
    }

    func updateNSView(_ view: CanvasView, context: Context) {
        view.live = live
        view.host = host
        if view.focus != focus {
            view.focus = focus
            DispatchQueue.main.async { view.window?.makeFirstResponder(view) }
        }
        view.show(frame)
        view.outlineChanged(live.outline)
    }

    /// Typing goes through macOS text input, so accents, dead keys and input methods work; named keys and shortcuts
    /// go to the page as presses.
    final class CanvasView: NSView, @preconcurrency NSTextInputClient {
        var live: LiveSession?
        var host = ""
        var focus = 0
        private var marked = ""
        private var elements: [PageElement] = []
        private var shownOutline: PageOutline?
        private var size = CGSize(width: 1, height: 1)
        private var shownSeq = -1

        override var acceptsFirstResponder: Bool { true }
        override var isFlipped: Bool { true }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

        override init(frame: NSRect) {
            super.init(frame: frame)
            wantsLayer = true
            layer?.contentsGravity = .resize
            layer?.magnificationFilter = .linear
            layer?.minificationFilter = .trilinear
        }

        required init?(coder: NSCoder) { fatalError() }

        // MARK: the page for VoiceOver

        /// The page's outline as accessibility elements over the picture. Being asked for children means a screen
        /// reader is running, so that is when the outline starts being kept up to date.
        override func isAccessibilityElement() -> Bool { true }
        override func accessibilityRole() -> NSAccessibility.Role? { .group }
        override func accessibilityRoleDescription() -> String? { "web page" }

        override func accessibilityLabel() -> String? {
            let page = host.isEmpty ? "a page" : host
            if let outline = live?.outline, !outline.available {
                return "Monty's browser, showing \(page). VoiceOver can't read pages in this browser. Shift-Command-T closes it."
            }
            return "Monty's browser, showing \(live?.outline?.title.isEmpty == false ? live!.outline!.title : page)"
        }

        override func accessibilityChildren() -> [Any]? {
            if live?.wantsOutline == false { live?.wantsOutline = true }
            return elements
        }

        func outlineChanged(_ outline: PageOutline?) {
            guard outline != shownOutline else { return }
            let focusMoved = outline?.items.firstIndex(where: \.focused) != shownOutline?.items.firstIndex(where: \.focused)
            shownOutline = outline
            elements = (outline?.items ?? []).map { PageElement(item: $0, canvas: self, frame: viewRect($0)) }
            NSAccessibility.post(element: self, notification: .layoutChanged)
            if focusMoved, let focused = elements.first(where: \.item.focused) {
                NSAccessibility.post(element: focused, notification: .focusedUIElementChanged)
            }
        }

        override func layout() {
            super.layout()
            elements = (shownOutline?.items ?? []).map { PageElement(item: $0, canvas: self, frame: viewRect($0)) }
        }

        /// An item's box, from the page's CSS pixels to this view's points.
        func viewRect(_ item: OutlineItem) -> NSRect {
            let sx = bounds.width / max(size.width, 1), sy = bounds.height / max(size.height, 1)
            return NSRect(x: item.x * sx, y: item.y * sy, width: item.width * sx, height: item.height * sy)
        }

        /// What pressing an item does: click its centre on the page, and keep the keyboard on the page for typing.
        func press(_ item: OutlineItem) {
            window?.makeFirstResponder(self)
            let (x, y) = item.centre
            live?.input(.mouseDown(x: x, y: y, button: .left))
            live?.input(.mouseUp(x: x, y: y, button: .left))
        }

        func show(_ frame: LiveFrame) {
            guard frame.seq != shownSeq || layer?.contents == nil else { return }
            shownSeq = frame.seq
            size = CGSize(width: frame.width, height: frame.height)
            if let image = NSImage(data: frame.image)?.cgImage(forProposedRect: nil, context: nil, hints: nil) {
                layer?.contents = image
            }
        }

        override func updateTrackingAreas() {
            super.updateTrackingAreas()
            trackingAreas.forEach(removeTrackingArea)
            addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .activeInKeyWindow, .inVisibleRect, .cursorUpdate], owner: self))
        }

        override func cursorUpdate(with event: NSEvent) { NSCursor.arrow.set() }

        /// The page's CSS pixel under the event.
        private func point(_ event: NSEvent) -> (Double, Double) {
            let location = convert(event.locationInWindow, from: nil)
            let x = max(0, min(size.width, location.x / max(bounds.width, 1) * size.width))
            let y = max(0, min(size.height, location.y / max(bounds.height, 1) * size.height))
            return (x.rounded(), y.rounded())
        }

        private func button(_ event: NSEvent) -> MouseButton {
            switch event.type {
            case .rightMouseDown, .rightMouseUp, .rightMouseDragged: .right
            case .otherMouseDown, .otherMouseUp, .otherMouseDragged: .middle
            default: event.modifierFlags.contains(.control) ? .right : .left
            }
        }

        override func mouseDown(with event: NSEvent) {
            window?.makeFirstResponder(self)
            lastPoint = convert(event.locationInWindow, from: nil)
            let (x, y) = point(event)
            live?.input(.mouseDown(x: x, y: y, button: button(event)))
        }
        override func mouseUp(with event: NSEvent) {
            let (x, y) = point(event)
            live?.input(.mouseUp(x: x, y: y, button: button(event)))
        }
        override func rightMouseDown(with event: NSEvent) { mouseDown(with: event) }
        override func rightMouseUp(with event: NSEvent) { mouseUp(with: event) }
        override func otherMouseDown(with event: NSEvent) { mouseDown(with: event) }
        override func otherMouseUp(with event: NSEvent) { mouseUp(with: event) }
        override func mouseMoved(with event: NSEvent) { move(event) }
        override func mouseDragged(with event: NSEvent) { move(event) }
        override func rightMouseDragged(with event: NSEvent) { move(event) }

        private func move(_ event: NSEvent) {
            let (x, y) = point(event)
            live?.input(.mouseMove(x: x, y: y))
        }

        override func scrollWheel(with event: NSEvent) {
            let (x, y) = point(event)
            // AppKit's deltas are in points, positive when content should move down; the page's are the other way.
            let scale = event.hasPreciseScrollingDeltas ? 1.0 : 16.0
            live?.input(.scroll(x: x, y: y, deltaX: -event.scrollingDeltaX * scale, deltaY: -event.scrollingDeltaY * scale))
        }

        override func keyDown(with event: NSEvent) {
            let flags = event.modifierFlags
            let named = KeyMapping.isNamed(event.keyCode)
            if flags.contains(.command) || flags.contains(.control) || (named && marked.isEmpty) {
                if !handle(event) { super.keyDown(with: event) }
                return
            }
            if inputContext?.handleEvent(event) != true { _ = handle(event) }
        }

        // MARK: NSTextInputClient

        func insertText(_ string: Any, replacementRange: NSRange) {
            let text = (string as? NSAttributedString)?.string ?? (string as? String) ?? ""
            marked = ""
            live?.type(text)
        }

        func setMarkedText(_ string: Any, selectedRange: NSRange, replacementRange: NSRange) {
            marked = (string as? NSAttributedString)?.string ?? (string as? String) ?? ""
        }

        func unmarkText() {
            if !marked.isEmpty { live?.type(marked) }
            marked = ""
        }

        override func doCommand(by selector: Selector) {}
        func hasMarkedText() -> Bool { !marked.isEmpty }
        func markedRange() -> NSRange { marked.isEmpty ? NSRange(location: NSNotFound, length: 0) : NSRange(location: 0, length: (marked as NSString).length) }
        func selectedRange() -> NSRange { NSRange(location: (marked as NSString).length, length: 0) }
        func validAttributesForMarkedText() -> [NSAttributedString.Key] { [] }
        func attributedSubstring(forProposedRange range: NSRange, actualRange: NSRangePointer?) -> NSAttributedString? { nil }
        func characterIndex(for point: NSPoint) -> Int { 0 }
        func firstRect(forCharacterRange range: NSRange, actualRange: NSRangePointer?) -> NSRect {
            // Where the input method's candidates appear: the pointer's last position on the page.
            let local = NSRect(x: lastPoint.x, y: lastPoint.y, width: 1, height: 18)
            return window?.convertToScreen(convert(local, to: nil)) ?? .zero
        }
        private var lastPoint = CGPoint.zero

        override func performKeyEquivalent(with event: NSEvent) -> Bool {
            guard window?.firstResponder === self else { return super.performKeyEquivalent(with: event) }
            // ⌘↩ gives the browser back; other app shortcuts (⌘Q, ⌘N) stay with the app. The browser's own (⌘W, ⌘T,
            // ⌘L and so on) never get here: `BrowserShortcuts` catches them first.
            if event.modifierFlags.contains(.command), event.keyCode == 36 { return super.performKeyEquivalent(with: event) }
            return handle(event) || super.performKeyEquivalent(with: event)
        }

        private func handle(_ event: NSEvent) -> Bool {
            switch KeyMapping.action(for: MacKey(event)) {
            case .send(let inputs):
                inputs.forEach { live?.input($0) }
                return true
            case .pasteFromMac:
                if let text = NSPasteboard.general.string(forType: .string) { live?.type(text) }
                return true
            case .ignore:
                return false
            }
        }
    }
}

/// One item of the remote page, for VoiceOver: what it is, what it says, and where it is over the picture.
final class PageElement: NSAccessibilityElement {
    let item: OutlineItem
    nonisolated(unsafe) private weak var canvas: LiveCanvas.CanvasView?  // only touched on the main thread

    init(item: OutlineItem, canvas: LiveCanvas.CanvasView, frame: NSRect) {
        self.item = item
        self.canvas = canvas
        super.init()
        setAccessibilityParent(canvas)
        setAccessibilityFrameInParentSpace(frame)
        setAccessibilityEnabled(!item.disabled)
    }

    override func accessibilityRole() -> NSAccessibility.Role? {
        switch item.role {
        case "button", "tab", "menuitem", "switch", "option": .button
        case "link": .link
        case "textbox", "searchbox", "spinbutton": .textField
        case "combobox", "listbox": .popUpButton
        case "checkbox": .checkBox
        case "radio": .radioButton
        case "slider": .slider
        case "image": .image
        default: .staticText
        }
    }

    override func accessibilityRoleDescription() -> String? {
        if item.role == "heading" { return "heading level \(item.level ?? 2)" }
        if item.secure { return "secure text field" }
        return nil
    }

    override func accessibilityLabel() -> String? { item.role == "text" || item.role == "heading" ? nil : item.name }

    override func accessibilityValue() -> Any? {
        switch item.role {
        case "text", "heading": return item.name
        case "checkbox", "radio", "switch": return (item.checked ?? false) ? 1 : 0
        default: return item.value.isEmpty ? nil : item.value
        }
    }

    override func accessibilityPlaceholderValue() -> String? { item.value.isEmpty && item.role == "textbox" ? item.name : nil }
    override func isAccessibilityFocused() -> Bool { item.focused }

    override func accessibilityPerformPress() -> Bool {
        let (canvas, item) = (canvas, item)
        MainActor.assumeIsolated { canvas?.press(item) }  // accessibility calls come on the main thread
        return true
    }

    /// VoiceOver focusing a field means the user wants to type there: put the page's caret in it.
    override func setAccessibilityFocused(_ focused: Bool) {
        guard focused, ["textbox", "searchbox", "spinbutton"].contains(item.role), !item.focused else { return }
        let (canvas, item) = (canvas, item)
        MainActor.assumeIsolated { canvas?.press(item) }
    }
}
