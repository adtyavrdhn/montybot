/// Sammy's replies are Markdown. These are the blocks the app draws: paragraphs, headings, lists, quotes, code,
/// tables and rules; inline marks (emphasis, code, links) stay in the text for the view to style.
public enum MarkdownBlock: Equatable, Sendable {
    case paragraph(String)
    case heading(Int, String)
    case bullet([String])
    case numbered([String])
    case quote(String)
    case code(String)
    case table(header: [String], rows: [[String]])
    case rule

    public static func parse(_ text: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var items: [String] = []
        var numbered = false
        var code: [String]?
        var table: [String] = []

        func flushTable() {
            defer { table = [] }
            guard !table.isEmpty else { return }
            let isRule = { (line: String) in line.range(of: #"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$"#, options: .regularExpression) != nil }
            if table.count >= 2, isRule(table[1]) {
                blocks.append(.table(header: cells(table[0]), rows: table.dropFirst(2).map(cells)))
            } else {
                blocks.append(.paragraph(table.joined(separator: "\n")))
            }
        }

        func flush() {
            flushTable()
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] }
            if !items.isEmpty { blocks.append(numbered ? .numbered(items) : .bullet(items)); items = [] }
        }

        for raw in text.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("```") {
                if let lines = code { blocks.append(.code(lines.joined(separator: "\n"))); code = nil } else { flush(); code = [] }
                continue
            }
            if code != nil { code!.append(raw); continue }
            if line.hasPrefix("|") {
                if table.isEmpty { flush() }
                table.append(line)
                continue
            }
            flushTable()
            if line.isEmpty { flush(); continue }
            if let match = line.firstMatch(of: /^(#{1,6})\s+(.*)$/) {
                flush(); blocks.append(.heading(match.1.count, String(match.2))); continue
            }
            if line == "---" || line == "***" { flush(); blocks.append(.rule); continue }
            if let match = line.firstMatch(of: /^[-*+]\s+(.*)$/) {
                if !items.isEmpty, numbered { flush() }
                if !paragraph.isEmpty { flush() }
                numbered = false; items.append(String(match.1)); continue
            }
            if let match = line.firstMatch(of: /^\d+[.)]\s+(.*)$/) {
                if !items.isEmpty, !numbered { flush() }
                if !paragraph.isEmpty { flush() }
                numbered = true; items.append(String(match.1)); continue
            }
            if let match = line.firstMatch(of: /^>\s?(.*)$/) {
                flush(); blocks.append(.quote(String(match.1))); continue
            }
            if !items.isEmpty { items[items.count - 1] += " " + line; continue }  // a wrapped list item
            paragraph.append(line)
        }
        if let lines = code { blocks.append(.code(lines.joined(separator: "\n"))) }
        flush()
        return blocks
    }

    static func cells(_ line: String) -> [String] {
        var trimmed = line.trimmingCharacters(in: .whitespaces)
        if trimmed.hasPrefix("|") { trimmed.removeFirst() }
        if trimmed.hasSuffix("|") { trimmed.removeLast() }
        return trimmed.components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
    }
}
