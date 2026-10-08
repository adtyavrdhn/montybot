import Foundation

// The live view's WebSocket protocol, as `montybot/liveview/wire.py` defines it: JSON text messages both ways, and
// binary frames from the server (a 4-byte big-endian header length, a JSON header, then the image).

public enum MouseButton: String, Sendable { case left, middle, right }

/// A browser's own buttons for the active tab; the raw value is the message's `kind`.
public enum PageCommand: String, Sendable { case back, forward, reload, stop }

/// What the user does in the live view, in the page's CSS pixels.
public enum LiveInput: Equatable, Sendable {
    case mouseDown(x: Double, y: Double, button: MouseButton)
    case mouseMove(x: Double, y: Double)
    case mouseUp(x: Double, y: Double, button: MouseButton)
    case scroll(x: Double, y: Double, deltaX: Double, deltaY: Double)
    /// Text at the caret.
    case type(String)
    /// A named key ("Enter", "ArrowLeft") or a character with modifiers ("a" with Control).
    case press(key: String, modifiers: [String])
    case switchTab(String)
    /// An address the user typed into the address bar, for the active tab.
    case navigate(String)
    case page(PageCommand)
    /// A blank tab, ready for an address.
    case newTab
    case closeTab(String)
    case giveBack
    /// Asks what is on the page, for a screen reader (answered with an outline).
    case outline

    public var json: String {
        var object: [String: Any]
        switch self {
        case .mouseDown(let x, let y, let button): object = ["kind": "mouse_down", "x": x, "y": y, "button": button.rawValue]
        case .mouseMove(let x, let y): object = ["kind": "mouse_move", "x": x, "y": y]
        case .mouseUp(let x, let y, let button): object = ["kind": "mouse_up", "x": x, "y": y, "button": button.rawValue]
        case .scroll(let x, let y, let deltaX, let deltaY):
            object = ["kind": "scroll", "x": x, "y": y, "delta_x": deltaX, "delta_y": deltaY]
        case .type(let text): object = ["kind": "type", "text": text]
        case .press(let key, let modifiers): object = ["kind": "press", "key": key, "modifiers": modifiers]
        case .switchTab(let tab): object = ["kind": "switch_tab", "tab_id": tab]
        case .navigate(let url): object = ["kind": "navigate", "url": url]
        case .page(let command): object = ["kind": command.rawValue]
        case .newTab: object = ["kind": "new_tab"]
        case .closeTab(let tab): object = ["kind": "close_tab", "tab_id": tab]
        case .giveBack: object = ["kind": "give_back"]
        case .outline: object = ["kind": "outline"]
        }
        let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data()
        return String(decoding: data, as: UTF8.self)
    }
}

public struct LiveTab: Equatable, Identifiable, Sendable {
    public let id: String
    public let url: String
    public let title: String
    public let active: Bool
    /// Any tab but the run's own, which Monty carries on in.
    public var closable = false
    /// The active tab is loading a page.
    public var loading = false
    /// Whether the active tab has history that way; nil when the browser cannot tell (Servo), so the button stays on.
    public var canGoBack: Bool?
    public var canGoForward: Bool?

    public init(id: String, url: String, title: String, active: Bool, closable: Bool = false, loading: Bool = false,
                canGoBack: Bool? = nil, canGoForward: Bool? = nil) {
        self.id = id
        self.url = url
        self.title = title
        self.active = active
        self.closable = closable
        self.loading = loading
        self.canGoBack = canGoBack
        self.canGoForward = canGoForward
    }

    /// A new tab, before anything was opened in it: the address bar shows its placeholder.
    public var isBlank: Bool { Self.isBlank(url) }
    public static func isBlank(_ url: String) -> Bool { url.isEmpty || url == "about:blank" }

    /// What the tab strip calls it: the page's title, else its host, as a browser without favicons would.
    public var name: String {
        if !title.isEmpty { return title }
        if isBlank { return "New Tab" }
        return URLComponents(string: url)?.host ?? url
    }
}

/// One thing on the page, in reading order, as a screen reader says it, with its box in the page's CSS pixels.
public struct OutlineItem: Equatable, Sendable {
    public let role: String
    public let name: String
    public let x, y, width, height: Double
    public var value = ""
    public var level: Int?
    public var checked: Bool?
    public var disabled = false
    public var focused = false
    public var secure = false

    public init(role: String, name: String, x: Double, y: Double, width: Double, height: Double, value: String = "",
                level: Int? = nil, checked: Bool? = nil, disabled: Bool = false, focused: Bool = false, secure: Bool = false) {
        self.role = role
        self.name = name
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.value = value
        self.level = level
        self.checked = checked
        self.disabled = disabled
        self.focused = focused
        self.secure = secure
    }

    init?(json: [String: Any]) {
        guard let role = json["role"] as? String,
              let x = (json["x"] as? NSNumber)?.doubleValue, let y = (json["y"] as? NSNumber)?.doubleValue,
              let width = (json["width"] as? NSNumber)?.doubleValue, let height = (json["height"] as? NSNumber)?.doubleValue
        else { return nil }
        self.init(role: role, name: json["name"] as? String ?? "", x: x, y: y, width: width, height: height,
                  value: json["value"] as? String ?? "", level: (json["level"] as? NSNumber)?.intValue,
                  checked: json["checked"] as? Bool, disabled: json["disabled"] as? Bool == true,
                  focused: json["focused"] as? Bool == true, secure: json["secure"] as? Bool == true)
    }

    /// The centre of its box: where pressing it clicks.
    public var centre: (x: Double, y: Double) { (x + width / 2, y + height / 2) }
}

/// What is on the page for a screen reader; `available` is false for an engine that cannot read pages.
public struct PageOutline: Equatable, Sendable {
    public let title: String
    public let items: [OutlineItem]
    public let available: Bool

    public init(title: String, items: [OutlineItem], available: Bool = true) {
        self.title = title
        self.items = items
        self.available = available
    }
}

public enum LiveServerMessage: Equatable, Sendable {
    /// `controls`: the browser has back, forward, reload, stop and opening and closing tabs.
    case hello(handoffId: String, reason: String, controls: Bool = false)
    case tabs([LiveTab])
    /// An input failed; safe to show (never contains typed text).
    case error(String)
    /// The hand-off is over; `givenBack` when this connection gave it back.
    case ended(givenBack: Bool)
    case outline(PageOutline)

    public init?(json text: String) {
        guard let object = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] else { return nil }
        switch object["kind"] as? String {
        case "hello":
            guard let id = object["handoff_id"] as? String, let reason = object["reason"] as? String else { return nil }
            self = .hello(handoffId: id, reason: reason, controls: object["controls"] as? Bool == true)
        case "tabs":
            guard let tabs = object["tabs"] as? [[String: Any]] else { return nil }
            self = .tabs(tabs.compactMap { tab in
                guard let id = tab["tab_id"] as? String, let url = tab["url"] as? String,
                      let title = tab["title"] as? String else { return nil }
                return LiveTab(id: id, url: url, title: title, active: tab["active"] as? Bool == true,
                               closable: tab["closable"] as? Bool == true, loading: tab["loading"] as? Bool == true,
                               canGoBack: tab["can_go_back"] as? Bool, canGoForward: tab["can_go_forward"] as? Bool)
            })
        case "error":
            guard let message = object["message"] as? String else { return nil }
            self = .error(message)
        case "ended":
            self = .ended(givenBack: object["given_back"] as? Bool == true)
        case "outline":
            let items = (object["items"] as? [[String: Any]] ?? []).compactMap(OutlineItem.init(json:))
            self = .outline(PageOutline(title: object["title"] as? String ?? "", items: items, available: object["available"] as? Bool != false))
        default:
            return nil
        }
    }
}

/// One picture of the active tab. `width` and `height` are the viewport's CSS pixels, the space input uses; the image
/// may be larger (a Retina-like screencast) or smaller.
public struct LiveFrame: Equatable, Sendable {
    public let seq: Int
    public let width: Double
    public let height: Double
    public let mime: String
    public let image: Data

    public init?(data: Data) {
        guard data.count >= 4 else { return nil }
        let bytes = [UInt8](data.prefix(4))
        let length = Int(bytes[0]) << 24 | Int(bytes[1]) << 16 | Int(bytes[2]) << 8 | Int(bytes[3])
        guard data.count >= 4 + length else { return nil }
        let start = data.startIndex + 4
        let header = data[start..<(start + length)]
        guard let object = (try? JSONSerialization.jsonObject(with: header)) as? [String: Any],
              let seq = (object["seq"] as? NSNumber)?.intValue,
              let width = (object["width"] as? NSNumber)?.doubleValue,
              let height = (object["height"] as? NSNumber)?.doubleValue,
              let mime = object["mime"] as? String, mime == "image/jpeg" || mime == "image/png",
              width > 0, height > 0
        else { return nil }
        self.seq = seq
        self.width = width
        self.height = height
        self.mime = mime
        image = Data(data[(start + length)...])
    }

    /// For tests: the bytes the server would send.
    public static func encode(seq: Int, width: Int, height: Int, mime: String, image: Data) -> Data {
        let header = Data(#"{"seq": \#(seq), "width": \#(width), "height": \#(height), "mime": "\#(mime)"}"#.utf8)
        var data = Data()
        let length = UInt32(header.count).bigEndian
        withUnsafeBytes(of: length) { data.append(contentsOf: $0) }
        return data + header + image
    }
}

/// How the server closes the live view's socket (`montybot/liveview/app.py`).
public enum LiveCloseCode {
    public static let signedOut = 4401
    public static let notFound = 4404
    public static let replaced = 4409
    public static let ended = 4410
}

// MARK: - keys

/// A browser's own shortcut while the user drives, as in Safari and Chrome. These never reach the page.
public enum BrowserShortcut: Equatable, Sendable {
    case page(PageCommand)
    case newTab
    case closeTab
    case editAddress
    case nextTab
    case previousTab
    /// ⌘1 to ⌘8: the tab at that place, from 1. ⌘9 is the last tab, wherever it is.
    case tab(Int)

    /// ⌘[ ⌘] ⌘R ⌘T ⌘W ⌘L ⌘1-9, ⌃⇥ and ⌃⇧⇥. ⇧⌘T stays the takeover's "Not now".
    public static func shortcut(for key: MacKey) -> BrowserShortcut? {
        if key.control, !key.command, !key.option, key.keyCode == 48 { return key.shift ? .previousTab : .nextTab }
        guard key.command, !key.control, !key.option, !key.shift else { return nil }
        switch key.bare.lowercased() {
        case "[": return .page(.back)
        case "]": return .page(.forward)
        case "r": return .page(.reload)
        case "t": return .newTab
        case "w": return .closeTab
        case "l": return .editAddress
        case let digit where digit.count == 1 && ("1"..."9").contains(digit): return .tab(Int(digit)!)
        default: return nil
        }
    }

    /// The tab a tab shortcut goes to, from the active one. Next and previous wrap around, as in a browser.
    public static func target(of shortcut: BrowserShortcut, in tabs: [LiveTab]) -> LiveTab? {
        guard !tabs.isEmpty else { return nil }
        let active = tabs.firstIndex(where: \.active) ?? 0
        switch shortcut {
        case .nextTab: return tabs[(active + 1) % tabs.count]
        case .previousTab: return tabs[(active - 1 + tabs.count) % tabs.count]
        case .tab(9): return tabs.last
        case .tab(let place): return tabs.indices.contains(place - 1) ? tabs[place - 1] : nil
        default: return nil
        }
    }
}

/// A key press on the Mac, as AppKit reports it, without AppKit: `keyCode` is the hardware key, `characters` what it
/// types with the modifiers applied, `bare` what it types without them.
public struct MacKey: Equatable, Sendable {
    public var keyCode: UInt16
    public var characters: String
    public var bare: String
    public var command = false
    public var option = false
    public var control = false
    public var shift = false

    public init(keyCode: UInt16, characters: String, bare: String? = nil, command: Bool = false, option: Bool = false,
                control: Bool = false, shift: Bool = false) {
        self.keyCode = keyCode
        self.characters = characters
        self.bare = bare ?? characters
        self.command = command
        self.option = option
        self.control = control
        self.shift = shift
    }
}

/// What a Mac key press means in the bot's browser, which runs on Linux: Mac editing shortcuts become their Linux
/// equivalents, so ⌘A selects all and ⌥⌫ deletes a word there as it does here.
public enum KeyAction: Equatable, Sendable {
    case send([LiveInput])
    /// ⌘V: paste the Mac's clipboard as typed text (the remote browser has its own, empty clipboard).
    case pasteFromMac
    /// Not for the browser: a Mac shortcut (⌘W, ⌘Q, ...) the app handles.
    case ignore
}

public enum KeyMapping {
    static let named: [UInt16: String] = [
        36: "Enter", 76: "Enter", 48: "Tab", 51: "Backspace", 53: "Escape", 117: "Delete",
        123: "ArrowLeft", 124: "ArrowRight", 125: "ArrowDown", 126: "ArrowUp",
        115: "Home", 119: "End", 116: "PageUp", 121: "PageDown",
        122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
        98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
    ]

    /// A key the page gets as a press, not as text: Return, Tab, the arrows and so on.
    public static func isNamed(_ keyCode: UInt16) -> Bool { named[keyCode] != nil }

    /// ⌘ shortcuts that edit text, sent on as Control: select all, copy, cut, undo, redo, find.
    static let commandEdits: Set<String> = ["a", "c", "x", "z", "y", "f"]

    public static func action(for key: MacKey) -> KeyAction {
        let lower = key.bare.lowercased()
        var modifiers: [String] = []
        if key.shift { modifiers.append("Shift") }

        if key.command {
            if lower == "v" { return .pasteFromMac }
            if let name = named[key.keyCode] {
                switch name {
                case "ArrowLeft": return .send([.press(key: "Home", modifiers: modifiers)])
                case "ArrowRight": return .send([.press(key: "End", modifiers: modifiers)])
                case "ArrowUp": return .send([.press(key: "Home", modifiers: ["Control"] + modifiers)])
                case "ArrowDown": return .send([.press(key: "End", modifiers: ["Control"] + modifiers)])
                case "Backspace":  // delete to the start of the line
                    return .send([.press(key: "Home", modifiers: ["Shift"]), .press(key: "Backspace", modifiers: [])])
                case "Enter": return .send([.press(key: "Enter", modifiers: ["Control"] + modifiers)])
                default: return .ignore
                }
            }
            guard commandEdits.contains(lower) else { return .ignore }
            return .send([.press(key: lower, modifiers: ["Control"] + modifiers)])
        }

        if let name = named[key.keyCode] {
            if key.option, ["ArrowLeft", "ArrowRight", "Backspace", "Delete"].contains(name) {
                return .send([.press(key: name, modifiers: ["Control"] + modifiers)])  // a word at a time
            }
            if key.control { modifiers.insert("Control", at: 0) }
            if key.option { modifiers.insert("Alt", at: 0) }
            return .send([.press(key: name, modifiers: modifiers)])
        }
        if key.control, !lower.isEmpty {
            return .send([.press(key: lower, modifiers: ["Control"] + modifiers)])
        }
        // Option types characters on a Mac (⌥E then E is é); the characters already carry what it typed.
        let text = key.characters.filter { !$0.isASCII || ($0.asciiValue ?? 0) >= 32 }
        return text.isEmpty ? .ignore : .send([.type(text)])
    }
}
