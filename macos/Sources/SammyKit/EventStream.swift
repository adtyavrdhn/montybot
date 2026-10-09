/// One server-sent event: its `event:` name ("message" when unnamed) and its data lines joined.
public struct ServerSentEvent: Equatable, Sendable {
    public let name: String
    public let data: String
}

/// Server-sent events, a line at a time (without the line break). An event is complete at the blank line after it;
/// comments (`: keep-alive`) and unknown fields are skipped, as the HTML spec says.
public struct EventStreamParser: Sendable {
    private var name = ""
    private var data: [String] = []

    public init() {}

    public mutating func feed(_ line: String) -> ServerSentEvent? {
        if line.isEmpty {
            defer { name = ""; data = [] }
            guard !data.isEmpty else { return nil }
            return ServerSentEvent(name: name.isEmpty ? "message" : name, data: data.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let field: Substring
        var value: Substring
        if let colon = line.firstIndex(of: ":") {
            field = line[..<colon]
            value = line[line.index(after: colon)...]
            if value.first == " " { value = value.dropFirst() }
        } else {
            field = Substring(line)
            value = ""
        }
        switch field {
        case "event": name = String(value)
        case "data": data.append(String(value))
        default: break
        }
        return nil
    }
}
