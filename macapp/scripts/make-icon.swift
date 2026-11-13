// Draws Marketing Studio's app icon (navy rounded square, brass "MS" + a small
// rising chart line) into an .iconset and turns it into Resources/AppIcon.icns.
//   swift scripts/make-icon.swift     (run from macapp/)
import Cocoa

let out = "Resources/AppIcon.icns"
let tmp = NSTemporaryDirectory() + "MarketingStudio-\(getpid()).iconset"
try? FileManager.default.removeItem(atPath: tmp)
try! FileManager.default.createDirectory(atPath: tmp, withIntermediateDirectories: true)

func draw(_ px: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    rep.size = NSSize(width: px, height: px)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let s = CGFloat(px)
    // macOS icon grid: ~10% margin, corner radius ~22.5% of the tile.
    let inset = s * 0.1, tile = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let path = NSBezierPath(roundedRect: tile, xRadius: tile.width * 0.225, yRadius: tile.width * 0.225)
    NSGradient(starting: NSColor(calibratedRed: 0.16, green: 0.29, blue: 0.47, alpha: 1),
               ending: NSColor(calibratedRed: 0.09, green: 0.17, blue: 0.29, alpha: 1))!.draw(in: path, angle: -90)
    let brass = NSColor(calibratedRed: 0.91, green: 0.75, blue: 0.48, alpha: 1)
    // rising line
    let line = NSBezierPath()
    line.move(to: NSPoint(x: tile.minX + tile.width * 0.2, y: tile.minY + tile.height * 0.22))
    line.line(to: NSPoint(x: tile.minX + tile.width * 0.42, y: tile.minY + tile.height * 0.34))
    line.line(to: NSPoint(x: tile.minX + tile.width * 0.58, y: tile.minY + tile.height * 0.27))
    line.line(to: NSPoint(x: tile.minX + tile.width * 0.8, y: tile.minY + tile.height * 0.42))
    line.lineWidth = tile.width * 0.045
    line.lineCapStyle = .round; line.lineJoinStyle = .round
    brass.setStroke(); line.stroke()
    // "MS"
    let font = NSFont.systemFont(ofSize: tile.width * 0.36, weight: .bold)
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
    let text = NSAttributedString(string: "MS", attributes: attrs)
    let size = text.size()
    text.draw(at: NSPoint(x: tile.midX - size.width / 2, y: tile.minY + tile.height * 0.44))
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64),
                   ("128x128", 128), ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512),
                   ("512x512", 512), ("512x512@2x", 1024)] {
    try! draw(px).write(to: URL(fileURLWithPath: "\(tmp)/icon_\(name).png"))
}
let p = Process()
p.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
p.arguments = ["-c", "icns", tmp, "-o", out]
try! p.run(); p.waitUntilExit()
try? FileManager.default.removeItem(atPath: tmp)
print(p.terminationStatus == 0 ? "wrote \(out)" : "iconutil failed")
