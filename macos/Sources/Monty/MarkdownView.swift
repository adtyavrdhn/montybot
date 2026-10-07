import MontyKit
import SwiftUI

/// Monty's replies, which are Markdown: paragraphs, headings, lists, quotes and code blocks, with inline emphasis,
/// code and links. Anything else shows as written.
struct MarkdownView: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(MarkdownBlock.parse(text).enumerated()), id: \.offset) { _, block in
                view(for: block)
            }
        }
        .textSelection(.enabled)
        .tint(Palette.link)
    }

    @ViewBuilder private func view(for block: MarkdownBlock) -> some View {
        switch block {
        case .paragraph(let text):
            inline(text).font(.system(size: 14)).lineSpacing(3.5)
        case .heading(let level, let text):
            inline(text).font(.system(size: level == 1 ? 18 : level == 2 ? 16 : 14, weight: .semibold)).padding(.top, 4)
        case .bullet(let items):
            list(items) { _ in Text("•").foregroundStyle(Palette.onSurfaceVariant) }
        case .numbered(let items):
            list(items) { index in Text("\(index + 1).").font(.system(size: 14).monospacedDigit()).foregroundStyle(Palette.onSurfaceVariant) }
        case .quote(let text):
            inline(text)
                .font(.system(size: 14))
                .foregroundStyle(Palette.onSurfaceVariant)
                .padding(.leading, 12)
                .overlay(alignment: .leading) { Rectangle().fill(Palette.outline).frame(width: 2) }
        case .code(let code):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(code).font(.mono(12)).padding(12)
            }
            .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.containerLow))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radiusMedium).strokeBorder(Palette.outlineVariant))
        case .table(let header, let rows):
            table(header: header, rows: rows)
        case .rule:
            Divider().overlay(Palette.outline)
        }
    }

    /// A table as Logfire draws one: a quiet header row, hairline rules, numbers that line up.
    private func table(header: [String], rows: [[String]]) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { _, cell in
                        inline(cell)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(Palette.onSurfaceVariant)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 7)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .background(Palette.containerLow)
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Divider().overlay(Palette.outlineVariant).gridCellUnsizedAxes(.horizontal)
                    GridRow {
                        ForEach(0..<header.count, id: \.self) { column in
                            inline(column < row.count ? row[column] : "")
                                .font(.system(size: 13).monospacedDigit())
                                .padding(.horizontal, 12)
                                .padding(.vertical, 7)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            .fixedSize(horizontal: true, vertical: false)
        }
        .background(RoundedRectangle(cornerRadius: Metrics.radiusMedium).fill(Palette.container))
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusMedium))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusMedium).strokeBorder(Palette.outline))
        .fixedSize(horizontal: false, vertical: true)
    }

    private func list(_ items: [String], marker: @escaping (Int) -> some View) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    marker(index).frame(minWidth: 14, alignment: .trailing)
                    inline(item).font(.system(size: 14)).lineSpacing(3)
                }
            }
        }
    }

    private func inline(_ text: String) -> Text {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        guard var attributed = try? AttributedString(markdown: text, options: options) else { return Text(text) }
        for run in attributed.runs where run.inlinePresentationIntent?.contains(.code) == true {
            attributed[run.range].font = .mono(12.5)
            attributed[run.range].backgroundColor = Palette.containerHigh
        }
        return Text(attributed)
    }

}
