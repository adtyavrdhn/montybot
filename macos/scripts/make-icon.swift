// Draws Monty's app icon: the Pydantic logomark in Pydantic pink on Logfire's near-black, in the macOS icon grid.
// `swift scripts/make-icon.swift Resources/AppIcon.iconset`, then `iconutil -c icns` (build-app.sh does both).
import AppKit

let output = URL(filePath: CommandLine.arguments[1])
try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

func mark(in rect: CGRect) -> NSBezierPath {
    let scale = min(rect.width / 138.4, rect.height / 120)
    let dx = rect.minX + (rect.width - 138.4 * scale) / 2
    let dy = rect.minY + (rect.height - 120 * scale) / 2
    // Flipped: the logomark's y grows downwards.
    func p(_ x: Double, _ y: Double) -> CGPoint { CGPoint(x: dx + x * scale, y: rect.maxY - (dy - rect.minY) - y * scale) }
    let path = NSBezierPath()
    for polygon in [
        [p(69.2, 0.4), p(137.8, 92.6), p(69.2, 119.8), p(0.6, 92.6)],
        [p(69.2, 14.28), p(94.8, 49.785), p(69.2, 41.4), p(43.6, 49.79)],
        [p(33.03, 64.45), p(63.875, 54.35), p(63.875, 107.34), p(13.905, 90.975)],
        [p(74.53, 107.33), p(74.53, 54.35), p(105.375, 64.45), p(124.5, 90.96)],
    ] {
        path.move(to: polygon[0])
        polygon.dropFirst().forEach { path.line(to: $0) }
        path.close()
    }
    path.windingRule = .evenOdd
    path.lineJoinStyle = .round
    return path
}

func icon(_ pixels: Int) -> Data {
    let size = CGFloat(pixels)
    let image = NSImage(size: NSSize(width: size, height: size), flipped: false) { _ in
        // The macOS grid: an 824/1024 rounded square, centred, with a soft shadow.
        let inset = size * 100 / 1024
        let tile = CGRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
        let shape = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.2237, yRadius: tile.width * 0.2237)
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.35)
        shadow.shadowBlurRadius = size * 0.02
        shadow.shadowOffset = NSSize(width: 0, height: -size * 0.008)
        shadow.set()
        NSColor(srgbRed: 0.05, green: 0.05, blue: 0.055, alpha: 1).setFill()
        shape.fill()
        NSGraphicsContext.restoreGraphicsState()
        // Logfire's dark surfaces, lit from above.
        NSGradient(starting: NSColor(srgbRed: 0.13, green: 0.12, blue: 0.14, alpha: 1), ending: NSColor(srgbRed: 0.045, green: 0.045, blue: 0.05, alpha: 1))?
            .draw(in: shape, angle: -90)
        NSColor.white.withAlphaComponent(0.08).setStroke()
        shape.lineWidth = max(1, size / 512)
        shape.stroke()
        let markSize = tile.width * 0.56
        let markRect = CGRect(x: tile.midX - markSize / 2, y: tile.midY - markSize * 0.45, width: markSize, height: markSize * 120 / 138.4)
        NSColor(srgbRed: 0.898, green: 0.125, blue: 0.914, alpha: 1).setFill()
        mark(in: markRect).fill()
        return true
    }
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8, samplesPerPixel: 4,
                               hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    image.draw(in: NSRect(x: 0, y: 0, width: size, height: size))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for points in [16, 32, 128, 256, 512] {
    try icon(points).write(to: output.appending(path: "icon_\(points)x\(points).png"))
    try icon(points * 2).write(to: output.appending(path: "icon_\(points)x\(points)@2x.png"))
}
