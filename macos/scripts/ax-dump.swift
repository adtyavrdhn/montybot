// What VoiceOver sees in a running app's main window: `ax-dump <pid>` prints one element per line: role, text
// (title | description | value | help), frame from the window's top left, and disabled/selected. scripts/tour.sh
// compiles and runs it for each screen of the tour.
import ApplicationServices
import Foundation

enum AXDump {
    static func dump(pid: pid_t) -> String {
        let app = AXUIElementCreateApplication(pid)
        let windows: [AXUIElement] = attribute(app, kAXWindowsAttribute) ?? []
        // Real windows only (the menu bar item has one of its own that reads as the application); the main one, or
        // else the largest, since an app started from a terminal may have no main window.
        let real = windows.filter { (attribute($0, kAXRoleAttribute) as String?) == "AXWindow" }
        let area = { (element: AXUIElement) in frame(element).width * frame(element).height }
        guard let window = real.first(where: { (attribute($0, kAXMainAttribute) as Bool?) == true })
            ?? real.max(by: { area($0) < area($1) }) else {
            return "(no window)"
        }
        let origin = frame(window).origin
        var lines: [String] = []
        func walk(_ element: AXUIElement, depth: Int) {
            guard depth < 40 else { return }
            let role: String = attribute(element, kAXRoleAttribute) ?? "?"
            var texts: [String] = []
            for key in [kAXTitleAttribute, kAXDescriptionAttribute, kAXValueAttribute, kAXHelpAttribute] {
                if let text: String = attribute(element, key), !text.isEmpty, !texts.contains(text) { texts.append(text) }
            }
            let controls = ["AXButton", "AXTextField", "AXTextArea", "AXImage", "AXPopUpButton", "AXMenuButton", "AXCheckBox", "AXLink", "AXRow"]
            let interesting = !texts.isEmpty || controls.contains(role)
            if interesting {
                let box = frame(element)
                let enabled: Bool = attribute(element, kAXEnabledAttribute) ?? true
                let selected: Bool = attribute(element, kAXSelectedAttribute) ?? false
                let shown = texts.joined(separator: " | ").replacingOccurrences(of: "\n", with: "⏎")
                lines.append("\(String(repeating: "  ", count: min(depth, 12)))\(role.replacingOccurrences(of: "AX", with: "")) \"\(shown.prefix(260))\" @\(Int(box.minX - origin.x)),\(Int(box.minY - origin.y)) \(Int(box.width))×\(Int(box.height))\(enabled ? "" : " disabled")\(selected ? " selected" : "")")
            }
            for child in (attribute(element, kAXChildrenAttribute) as [AXUIElement]?) ?? [] {
                walk(child, depth: interesting ? depth + 1 : depth)
            }
        }
        walk(window, depth: 0)
        return lines.joined(separator: "\n")
    }

    private static func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value as? T
    }

    private static func frame(_ element: AXUIElement) -> CGRect {
        var origin = CGPoint.zero, size = CGSize.zero
        if let value: AXValue = attribute(element, kAXPositionAttribute) { AXValueGetValue(value, .cgPoint, &origin) }
        if let value: AXValue = attribute(element, kAXSizeAttribute) { AXValueGetValue(value, .cgSize, &size) }
        return CGRect(origin: origin, size: size)
    }
}

guard CommandLine.arguments.count == 2, let pid = pid_t(CommandLine.arguments[1]) else {
    FileHandle.standardError.write(Data("usage: ax-dump <pid>\n".utf8))
    exit(2)
}
print(AXDump.dump(pid: pid))
