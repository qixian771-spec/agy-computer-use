import Cocoa
import QuartzCore
import CoreGraphics
import Darwin

// MARK: - Spring Physics & Data Models

struct SpringValue {
    var current: CGFloat
    var velocity: CGFloat = 0
    var target: CGFloat
    var stiffness: CGFloat = 290.0
    var damping: CGFloat = 24.0

    init(_ initial: CGFloat, stiffness: CGFloat = 290.0, damping: CGFloat = 24.0) {
        self.current = initial
        self.target = initial
        self.stiffness = stiffness
        self.damping = damping
    }

    mutating func step(dt: CGFloat) -> Bool {
        let clampedDt = min(dt, 1.0 / 20.0)
        let displacement = current - target
        let force = -stiffness * displacement - damping * velocity
        velocity += force * clampedDt
        current += velocity * clampedDt
        if abs(displacement) < 0.25 && abs(velocity) < 1.5 {
            current = target
            velocity = 0
            return false
        }
        return true
    }
}

struct ScreenIslandMetrics {
    let screenFrame: NSRect
    let hasHardwareNotch: Bool
    let notchWidth: CGFloat
    let notchHeight: CGFloat
    let centerX: CGFloat

    static func current() -> ScreenIslandMetrics {
        let screen = NSScreen.main ?? NSScreen.screens.first
        let sf = screen?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var topInset: CGFloat = 0
        if let s = screen {
            topInset = s.safeAreaInsets.top
        }
        if topInset > 0, let s = screen {
            let nh = max(30.0, topInset)
            var nw: CGFloat = 188.0
            var cx: CGFloat = sf.midX
            if let leftArea = s.auxiliaryTopLeftArea,
               let rightArea = s.auxiliaryTopRightArea,
               rightArea.minX > leftArea.maxX {
                nw = max(156.0, min(260.0, rightArea.minX - leftArea.maxX))
                cx = (leftArea.maxX + rightArea.minX) / 2.0
            }
            return ScreenIslandMetrics(
                screenFrame: sf,
                hasHardwareNotch: true,
                notchWidth: nw,
                notchHeight: nh,
                centerX: cx
            )
        }
        return ScreenIslandMetrics(
            screenFrame: sf,
            hasHardwareNotch: false,
            notchWidth: 150.0,
            notchHeight: 0.0,
            centerX: sf.midX
        )
    }
}

enum IslandMode: Equatable {
    case hidden
    case working
    case authRequired
    case permissionRequired
    case done
    case stopped
}

struct IslandButtonSpec {
    let id: String
    let title: String
    let style: Style
    var rect: NSRect

    enum Style {
        case neutral
        case primaryEmerald
        case activeCyan
        case amber
        case danger
    }
}

struct RippleEffect {
    var x: CGFloat
    var y: CGFloat
    var startTime: TimeInterval
    var duration: TimeInterval = 0.46
    var isRightClick: Bool = false
}

final class SharedHUDState {
    var watching: Bool = false
    var stopTimestamp: Double = 0
    var authResolvedTimestamp: Double = 0
    var authActive: Bool = false

    var targetPid: pid_t = 0
    var targetWindowId: CGWindowID = 0
    var targetAppName: String = ""
    var explicitBounds: CGRect? = nil
    var contentInsetTop: CGFloat = 0
    var contentSize: CGSize? = nil
    var permissionSettingURL: String = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"

    let uid = getuid()
    var stateFilePath: String { "/tmp/agy_hud_state_\(uid).json" }
    var pidFilePath: String { "/tmp/agy_hud_\(uid).pid" }

    func writeStateToDisk() {
        let payload: [String: Any] = [
            "watching": watching,
            "stop_ts": stopTimestamp,
            "auth_resolved_ts": authResolvedTimestamp,
            "auth_active": authActive,
            "updated_ts": Date().timeIntervalSince1970,
            "pid": ProcessInfo.processInfo.processIdentifier
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: payload, options: []) else { return }
        let tmpPath = stateFilePath + ".tmp"
        try? data.write(to: URL(fileURLWithPath: tmpPath), options: .atomic)
        rename(tmpPath, stateFilePath)
    }
}

// MARK: - Unconstrained Top-Bezel Notch Panel

final class NotchPanel: NSPanel {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect {
        // Critical: allow placing the Dynamic Island flush with screen.frame.maxY inside the MacBook notch band!
        return frameRect
    }
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

// MARK: - Dynamic Island Hardware-Fused Animated Canvas

final class DynamicIslandView: NSView {
    override var isFlipped: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    var metrics: ScreenIslandMetrics = ScreenIslandMetrics.current()
    var mode: IslandMode = .hidden

    // Spring-animated geometry
    var widthSpring = SpringValue(188, stiffness: 280, damping: 23)
    var heightSpring = SpringValue(34, stiffness: 280, damping: 23)
    var radiusSpring = SpringValue(18, stiffness: 300, damping: 26)
    var contentAlphaSpring = SpringValue(0, stiffness: 260, damping: 24)
    var expandAlphaSpring = SpringValue(0, stiffness: 260, damping: 24)

    // Telemetry & copy
    var phaseTag: String = "work"   // open | click | type | scroll | scan | auth | done
    var titleText: String = "正在后台执行..."
    var prevTitleText: String = ""
    var textSlideProgress: CGFloat = 1.0
    var subtitleText: String = ""
    var appBadgeText: String = ""

    // Animation clock
    var wavePhase: CGFloat = 0
    var glowPhase: CGFloat = 0
    var isHovered: Bool = false

    // Interactive buttons rendered inside the island
    var buttons: [IslandButtonSpec] = []
    var hoveredButtonId: String? = nil
    var pressedButtonId: String? = nil
    var onButtonTap: ((String) -> Void)?

    func setStatusText(_ newText: String) {
        guard newText != titleText, !newText.isEmpty else { return }
        prevTitleText = titleText
        titleText = newText
        textSlideProgress = 0.0
    }

    func currentIslandBodyRect() -> NSRect {
        let w = max(120, widthSpring.current)
        let h = max(24, heightSpring.current)
        let topY: CGFloat = metrics.hasHardwareNotch ? 0.0 : 7.0
        return NSRect(x: (bounds.width - w) / 2.0, y: topY, width: w, height: max(22, h - topY))
    }

    private func buildIslandPath(bodyRect: NSRect, bottomRadius: CGFloat) -> NSBezierPath {
        let br = min(bottomRadius, bodyRect.height / 2.0, bodyRect.width / 2.0)
        if !metrics.hasHardwareNotch {
            return NSBezierPath(roundedRect: bodyRect, xRadius: br, yRadius: br)
        }

        // True MacBook Notch silhouette with concave top bezel ears!
        let earR: CGFloat = 11.0
        let lx = bodyRect.minX
        let rx = bodyRect.maxX
        let h = bodyRect.maxY

        let p = NSBezierPath()
        p.move(to: NSPoint(x: lx - earR, y: 0))
        p.curve(
            to: NSPoint(x: lx, y: earR),
            controlPoint1: NSPoint(x: lx - earR * 0.15, y: 0),
            controlPoint2: NSPoint(x: lx, y: earR * 0.15)
        )
        p.line(to: NSPoint(x: lx, y: max(earR, h - br)))
        p.curve(
            to: NSPoint(x: lx + br, y: h),
            controlPoint1: NSPoint(x: lx, y: h - br * 0.44),
            controlPoint2: NSPoint(x: lx + br * 0.44, y: h)
        )
        p.line(to: NSPoint(x: rx - br, y: h))
        p.curve(
            to: NSPoint(x: rx, y: max(earR, h - br)),
            controlPoint1: NSPoint(x: rx - br * 0.44, y: h),
            controlPoint2: NSPoint(x: rx, y: h - br * 0.44)
        )
        p.line(to: NSPoint(x: rx, y: earR))
        p.curve(
            to: NSPoint(x: rx + earR, y: 0),
            controlPoint1: NSPoint(x: rx, y: earR * 0.15),
            controlPoint2: NSPoint(x: rx + earR * 0.15, y: 0)
        )
        p.close()
        return p
    }

    private func accentColors() -> (NSColor, NSColor) {
        switch mode {
        case .authRequired, .permissionRequired:
            return (
                NSColor(calibratedRed: 0.98, green: 0.75, blue: 0.15, alpha: 1.0),
                NSColor(calibratedRed: 0.98, green: 0.45, blue: 0.18, alpha: 1.0)
            )
        case .done:
            return (
                NSColor(calibratedRed: 0.16, green: 0.88, blue: 0.60, alpha: 1.0),
                NSColor(calibratedRed: 0.13, green: 0.83, blue: 0.93, alpha: 1.0)
            )
        case .stopped:
            return (
                NSColor(calibratedRed: 0.96, green: 0.32, blue: 0.32, alpha: 1.0),
                NSColor(calibratedRed: 0.92, green: 0.18, blue: 0.42, alpha: 1.0)
            )
        default:
            return (
                NSColor(calibratedRed: 0.13, green: 0.83, blue: 0.95, alpha: 1.0),
                NSColor(calibratedRed: 0.51, green: 0.55, blue: 0.97, alpha: 1.0)
            )
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        let alpha = max(0.0, min(1.0, contentAlphaSpring.current))
        guard mode != .hidden || alpha > 0.02 else { return }

        let bodyRect = currentIslandBodyRect()
        let islandPath = buildIslandPath(bodyRect: bodyRect, bottomRadius: radiusSpring.current)
        let (accentA, accentB) = accentColors()

        // 1. Soft Ambient Aurora Drop Glow + Pure Pitch-Black Notch Body
        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        let glowPulse = 0.5 + 0.5 * sin(glowPhase)
        let shadowAlpha: CGFloat = (mode == .authRequired || mode == .permissionRequired)
            ? (0.42 + 0.20 * glowPulse)
            : (0.28 + 0.10 * glowPulse)
        shadow.shadowColor = accentA.withAlphaComponent(shadowAlpha * alpha)
        shadow.shadowBlurRadius = (mode == .authRequired || mode == .permissionRequired) ? 18.0 : 12.0
        shadow.shadowOffset = NSSize(width: 0, height: 3)
        shadow.set()

        NSColor(calibratedWhite: 0.0, alpha: 0.985 * max(0.35, alpha)).setFill()
        islandPath.fill()
        NSGraphicsContext.restoreGraphicsState()

        // 2. Subtle Shimmering Rim Border (masked away from y=0 on notched Macs so bezel seam is invisible)
        NSGraphicsContext.saveGraphicsState()
        let clipTop: CGFloat = metrics.hasHardwareNotch ? 2.0 : 0.0
        NSBezierPath(rect: NSRect(x: 0, y: clipTop, width: bounds.width, height: bounds.height - clipTop)).addClip()
        let borderAlpha: CGFloat = (mode == .authRequired || mode == .permissionRequired)
            ? (0.62 + 0.25 * glowPulse)
            : (0.26 + 0.12 * glowPulse)
        accentA.withAlphaComponent(borderAlpha * alpha).setStroke()
        islandPath.lineWidth = (mode == .authRequired || mode == .permissionRequired) ? 1.6 : 1.05
        islandPath.stroke()
        NSGraphicsContext.restoreGraphicsState()

        guard alpha > 0.08 else { return }

        // 3. Wing Animations (Left Equalizer Waveform + Right Live Badge)
        drawTopWings(bodyRect: bodyRect, alpha: alpha, accentA: accentA, accentB: accentB)

        // 4. Unobstructed Content Area Below Hardware Camera Cutout
        drawMainContent(bodyRect: bodyRect, alpha: alpha, accentA: accentA, accentB: accentB)
    }

    private func drawTopWings(bodyRect: NSRect, alpha: CGFloat, accentA: NSColor, accentB: NSColor) {
        let wingCenterY: CGFloat = metrics.hasHardwareNotch
            ? min(metrics.notchHeight * 0.52, bodyRect.minY + 17.0)
            : (bodyRect.minY + 21.0)

        // Left Wing: 5-bar fluid neural equalizer or state glyph
        let eqOriginX = bodyRect.minX + 15.0
        if mode == .done {
            // Crisp emerald checkmark
            let check = NSBezierPath()
            check.move(to: NSPoint(x: eqOriginX + 2, y: wingCenterY + 0.5))
            check.line(to: NSPoint(x: eqOriginX + 6.5, y: wingCenterY + 5.0))
            check.line(to: NSPoint(x: eqOriginX + 15.0, y: wingCenterY - 4.5))
            accentA.withAlphaComponent(alpha).setStroke()
            check.lineWidth = 2.4
            check.lineCapStyle = .round
            check.lineJoinStyle = .round
            check.stroke()
        } else if mode == .authRequired || mode == .permissionRequired {
            // Pulsing golden lock icon
            let pulse = 1.0 + 0.08 * sin(glowPhase * 1.8)
            let cx = eqOriginX + 8.0
            let cy = wingCenterY
            let bodyW: CGFloat = 11.0 * pulse
            let bodyH: CGFloat = 8.5 * pulse
            let lockBody = NSBezierPath(
                roundedRect: NSRect(x: cx - bodyW / 2, y: cy - 1.0, width: bodyW, height: bodyH),
                xRadius: 2.5,
                yRadius: 2.5
            )
            accentA.withAlphaComponent(alpha).setFill()
            lockBody.fill()

            let shackle = NSBezierPath()
            shackle.appendArc(
                withCenter: NSPoint(x: cx, y: cy - 1.0),
                radius: 3.4 * pulse,
                startAngle: 180,
                endAngle: 360,
                clockwise: false
            )
            accentA.withAlphaComponent(alpha).setStroke()
            shackle.lineWidth = 1.8
            shackle.stroke()
        } else {
            // 5 dancing gradient equalizer bars!
            let barCount = 5
            let barW: CGFloat = 3.0
            let gap: CGFloat = 2.6
            for i in 0..<barCount {
                let t = CGFloat(i) / CGFloat(barCount - 1)
                let wave1 = sin(wavePhase + CGFloat(i) * 0.95)
                let wave2 = cos(wavePhase * 1.45 - CGFloat(i) * 0.6)
                let amp = (mode == .stopped) ? 0.18 : (0.35 + 0.65 * abs(wave1 * 0.7 + wave2 * 0.3))
                let barH = max(4.0, 14.5 * amp)
                let bx = eqOriginX + CGFloat(i) * (barW + gap)
                let by = wingCenterY - barH / 2.0
                let barColor = accentA.blended(withFraction: t, of: accentB) ?? accentA
                barColor.withAlphaComponent(alpha * 0.96).setFill()
                NSBezierPath(roundedRect: NSRect(x: bx, y: by, width: barW, height: barH), xRadius: 1.5, yRadius: 1.5).fill()
            }
        }

        // Right Wing: Compact state badge / orbital indicator (only on notched Macs or Auth/Expanded states so non-notch single row doesn't overlap)
        if metrics.hasHardwareNotch || mode == .authRequired || mode == .permissionRequired || expandAlphaSpring.current > 0.25 {
            let rightBadge: String
            switch mode {
            case .authRequired:       rightBadge = "待你授权"
            case .permissionRequired: rightBadge = "系统权限"
            case .done:               rightBadge = "已完成"
            case .stopped:            rightBadge = "已停止"
            default:                  rightBadge = appBadgeText.isEmpty ? "灵动静默" : appBadgeText
            }
            let badgeFont = NSFont.systemFont(ofSize: 10.5, weight: .semibold)
            let badgeAttrs: [NSAttributedString.Key: Any] = [
                .font: badgeFont,
                .foregroundColor: accentA.withAlphaComponent(0.92 * alpha)
            ]
            let bStr = rightBadge as NSString
            let bSz = bStr.size(withAttributes: badgeAttrs)
            let maxWingRightW: CGFloat = metrics.hasHardwareNotch
                ? max(42, (bodyRect.width - metrics.notchWidth) / 2.0 - 18.0)
                : 90.0
            if bSz.width <= maxWingRightW {
                bStr.draw(
                    at: NSPoint(x: bodyRect.maxX - bSz.width - 15.0, y: wingCenterY - bSz.height / 2.0),
                    withAttributes: badgeAttrs
                )
            }
        }
    }

    private func drawMainContent(bodyRect: NSRect, alpha: CGFloat, accentA: NSColor, accentB: NSColor) {
        buttons.removeAll(keepingCapacity: true)

        let contentTopY: CGFloat = metrics.hasHardwareNotch
            ? (metrics.notchHeight + 3.0)
            : (bodyRect.minY + 11.0)
        let leftPad: CGFloat = metrics.hasHardwareNotch ? 18.0 : 48.0
        let rightPad: CGFloat = 16.0
        let availW = max(80.0, bodyRect.width - leftPad - rightPad)

        // 1. Primary Live Action Ticker (with smooth vertical slide-fade transition)
        let titleFont = NSFont.systemFont(
            ofSize: (mode == .authRequired || mode == .permissionRequired) ? 13.2 : 12.5,
            weight: (mode == .authRequired || mode == .permissionRequired) ? .semibold : .medium
        )
        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail

        let p = max(0.0, min(1.0, textSlideProgress))
        if p < 1.0 && !prevTitleText.isEmpty {
            let oldAttrs: [NSAttributedString.Key: Any] = [
                .font: titleFont,
                .foregroundColor: NSColor(calibratedWhite: 0.95, alpha: (1.0 - p) * 0.7 * alpha),
                .paragraphStyle: paragraph
            ]
            (prevTitleText as NSString).draw(
                in: NSRect(x: bodyRect.minX + leftPad, y: contentTopY - p * 7.0, width: availW, height: 20),
                withAttributes: oldAttrs
            )
        }

        // Reserve space on the right if non-notched compact pill shows a quick hint
        let newAttrs: [NSAttributedString.Key: Any] = [
            .font: titleFont,
            .foregroundColor: NSColor(calibratedWhite: 0.98, alpha: p * alpha),
            .paragraphStyle: paragraph
        ]
        (titleText as NSString).draw(
            in: NSRect(x: bodyRect.minX + leftPad, y: contentTopY + (1.0 - p) * 7.0, width: availW, height: 20),
            withAttributes: newAttrs
        )

        // 2. Expanded Section (Hover Drawer OR Auth / Permission Takeover Card)
        let expAlpha = max(0.0, min(1.0, expandAlphaSpring.current)) * alpha
        guard expAlpha > 0.06 else { return }

        var rowY = contentTopY + 23.0

        if (mode == .authRequired || mode == .permissionRequired) && !subtitleText.isEmpty {
            let subFont = NSFont.systemFont(ofSize: 11.4, weight: .regular)
            let subStyle = NSMutableParagraphStyle()
            subStyle.lineBreakMode = .byTruncatingTail
            let subAttrs: [NSAttributedString.Key: Any] = [
                .font: subFont,
                .foregroundColor: NSColor(calibratedWhite: 0.78, alpha: expAlpha),
                .paragraphStyle: subStyle
            ]
            (subtitleText as NSString).draw(
                in: NSRect(x: bodyRect.minX + 18.0, y: rowY, width: bodyRect.width - 36.0, height: 18.0),
                withAttributes: subAttrs
            )
            rowY += 25.0
        } else {
            rowY += 5.0
        }

        // Build action button specs depending on current mode
        var specs: [(String, String, IslandButtonSpec.Style)] = []
        switch mode {
        case .authRequired:
            specs = [
                ("auth_done", "✓ 我已完成，继续后台执行", .primaryEmerald),
                ("pop_win", "弹出登录窗口", .amber),
                ("stop", "停止", .danger)
            ]
        case .permissionRequired:
            specs = [
                ("open_prefs", "打开系统隐私设置", .amber),
                ("auth_done", "已开启，继续", .primaryEmerald),
                ("hide", "关闭", .neutral)
            ]
        case .working:
            let watchTitle = (appBadgeText == "前台查看中") ? "切回后台隐身" : "查看真实窗口"
            let watchStyle: IslandButtonSpec.Style = (appBadgeText == "前台查看中") ? .activeCyan : .neutral
            specs = [
                ("toggle_watch", watchTitle, watchStyle),
                ("manual_auth", "我来接管/登录", .amber),
                ("stop", "停止", .danger)
            ]
        default:
            break
        }

        guard !specs.isEmpty else { return }

        let btnFont = NSFont.systemFont(ofSize: 11.6, weight: .medium)
        let btnH: CGFloat = 26.0
        let spacing: CGFloat = 8.0
        var totalW: CGFloat = 0
        var widths: [CGFloat] = []
        for (_, title, _) in specs {
            let tw = (title as NSString).size(withAttributes: [.font: btnFont]).width + 22.0
            widths.append(ceil(tw))
            totalW += ceil(tw)
        }
        totalW += CGFloat(max(0, specs.count - 1)) * spacing

        var curX = bodyRect.midX - totalW / 2.0
        for (idx, item) in specs.enumerated() {
            let rect = NSRect(x: curX, y: rowY, width: widths[idx], height: btnH)
            let spec = IslandButtonSpec(id: item.0, title: item.1, style: item.2, rect: rect)
            buttons.append(spec)
            drawIslandButton(spec, alpha: expAlpha, font: btnFont)
            curX += widths[idx] + spacing
        }
    }

    private func drawIslandButton(_ btn: IslandButtonSpec, alpha: CGFloat, font: NSFont) {
        let hovered = (hoveredButtonId == btn.id)
        let pressed = (pressedButtonId == btn.id)
        let drawRect = pressed ? btn.rect.insetBy(dx: 1.0, dy: 0.8) : btn.rect
        let path = NSBezierPath(roundedRect: drawRect, xRadius: 13, yRadius: 13)

        let bg: NSColor
        let border: NSColor
        let fg: NSColor

        switch btn.style {
        case .primaryEmerald:
            bg = NSColor(calibratedRed: 0.10, green: 0.78, blue: 0.52, alpha: (pressed ? 0.52 : (hovered ? 0.40 : 0.28)) * alpha)
            border = NSColor(calibratedRed: 0.20, green: 0.92, blue: 0.65, alpha: 0.68 * alpha)
            fg = NSColor(calibratedRed: 0.90, green: 1.0, blue: 0.95, alpha: alpha)
        case .activeCyan:
            bg = NSColor(calibratedRed: 0.08, green: 0.72, blue: 0.88, alpha: (pressed ? 0.48 : (hovered ? 0.38 : 0.26)) * alpha)
            border = NSColor(calibratedRed: 0.18, green: 0.86, blue: 0.96, alpha: 0.65 * alpha)
            fg = NSColor(calibratedWhite: 1.0, alpha: alpha)
        case .amber:
            bg = NSColor(calibratedRed: 0.96, green: 0.65, blue: 0.14, alpha: (pressed ? 0.44 : (hovered ? 0.34 : 0.22)) * alpha)
            border = NSColor(calibratedRed: 0.98, green: 0.78, blue: 0.28, alpha: 0.60 * alpha)
            fg = NSColor(calibratedRed: 1.0, green: 0.94, blue: 0.80, alpha: alpha)
        case .danger:
            bg = NSColor(calibratedRed: 0.94, green: 0.26, blue: 0.26, alpha: (pressed ? 0.44 : (hovered ? 0.34 : 0.20)) * alpha)
            border = NSColor(calibratedRed: 0.98, green: 0.45, blue: 0.45, alpha: 0.45 * alpha)
            fg = NSColor(calibratedRed: 1.0, green: 0.86, blue: 0.86, alpha: alpha)
        case .neutral:
            bg = NSColor(calibratedWhite: 1.0, alpha: (pressed ? 0.26 : (hovered ? 0.19 : 0.12)) * alpha)
            border = NSColor(calibratedWhite: 1.0, alpha: 0.20 * alpha)
            fg = NSColor(calibratedWhite: 0.96, alpha: alpha)
        }

        bg.setFill()
        path.fill()
        border.setStroke()
        path.lineWidth = 1.0
        path.stroke()

        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: fg]
        let ns = btn.title as NSString
        let sz = ns.size(withAttributes: attrs)
        ns.draw(
            at: NSPoint(x: drawRect.midX - sz.width / 2.0, y: drawRect.midY - sz.height / 2.0),
            withAttributes: attrs
        )
    }

    // MARK: - Direct Non-Activating Mouse Interaction

    override func mouseMoved(with event: NSEvent) {
        let pt = convert(event.locationInWindow, from: nil)
        let hit = buttons.first { $0.rect.contains(pt) }?.id
        if hit != hoveredButtonId {
            hoveredButtonId = hit
            needsDisplay = true
        }
    }

    override func mouseDown(with event: NSEvent) {
        let pt = convert(event.locationInWindow, from: nil)
        if let hit = buttons.first(where: { $0.rect.contains(pt) })?.id {
            pressedButtonId = hit
            needsDisplay = true
        } else if mode == .working {
            // Clicking anywhere on the compact Dynamic Island toggles hover/watch drawer or pops window!
            onButtonTap?("island_body")
        }
    }

    override func mouseUp(with event: NSEvent) {
        let pt = convert(event.locationInWindow, from: nil)
        let released = buttons.first(where: { $0.rect.contains(pt) })?.id
        let wasPressed = pressedButtonId
        pressedButtonId = nil
        needsDisplay = true
        if let id = wasPressed, id == released {
            onButtonTap?(id)
        }
    }
}

// MARK: - Transparent Click-Through AI Cursor & Ripple Canvas (For Real Window Watch Mode)

final class AICursorCanvasView: NSView {
    override var isFlipped: Bool { true }

    var cursorStart: CGPoint = CGPoint(x: 320, y: 240)
    var cursorCurrent: CGPoint = CGPoint(x: 320, y: 240)
    var cursorTarget: CGPoint = CGPoint(x: 320, y: 240)
    var moveStartTime: TimeInterval = 0
    var moveDuration: TimeInterval = 0.20
    var cursorBadge: String = "AI"
    var ripples: [RippleEffect] = []
    var cursorVisible: Bool = false

    func moveTo(point: CGPoint, badge: String, spawnClickRipple: Bool, isRightClick: Bool = false) {
        let now = CACurrentMediaTime()
        if !cursorVisible {
            cursorCurrent = point
            cursorStart = point
            cursorTarget = point
            cursorVisible = true
        } else {
            cursorStart = cursorCurrent
            cursorTarget = point
            moveStartTime = now
        }
        cursorBadge = badge
        if spawnClickRipple {
            ripples.append(RippleEffect(x: point.x, y: point.y, startTime: now + 0.08, isRightClick: isRightClick))
        }
        needsDisplay = true
    }

    func stepAnimation(now: TimeInterval) -> Bool {
        var animating = false
        if cursorVisible {
            let elapsed = now - moveStartTime
            if elapsed < moveDuration && moveDuration > 0 {
                let t = max(0.0, min(1.0, elapsed / moveDuration))
                let ease = 1.0 - pow(1.0 - t, 3.0)
                cursorCurrent = CGPoint(
                    x: cursorStart.x + (cursorTarget.x - cursorStart.x) * CGFloat(ease),
                    y: cursorStart.y + (cursorTarget.y - cursorStart.y) * CGFloat(ease)
                )
                animating = true
            } else {
                cursorCurrent = cursorTarget
            }
        }
        ripples.removeAll { now - $0.startTime > $0.duration }
        if !ripples.isEmpty { animating = true }
        if animating { needsDisplay = true }
        return animating
    }

    override func draw(_ dirtyRect: NSRect) {
        guard bounds.width > 20 && bounds.height > 20 else { return }

        let frameRect = bounds.insetBy(dx: 1.5, dy: 1.5)
        let borderPath = NSBezierPath(roundedRect: frameRect, xRadius: 11, yRadius: 11)
        NSColor(calibratedRed: 0.13, green: 0.83, blue: 0.93, alpha: 0.52).setStroke()
        borderPath.lineWidth = 2.0
        borderPath.stroke()

        let now = CACurrentMediaTime()
        for r in ripples {
            let dt = now - r.startTime
            if dt < 0 { continue }
            let p = CGFloat(max(0.0, min(1.0, dt / r.duration)))
            let ease = 1.0 - pow(1.0 - p, 2.5)
            let radius: CGFloat = 5.0 + ease * 28.0
            let alpha: CGFloat = (1.0 - p) * 0.90
            let color = r.isRightClick
                ? NSColor(calibratedRed: 0.66, green: 0.33, blue: 0.97, alpha: alpha)
                : NSColor(calibratedRed: 0.06, green: 0.82, blue: 0.95, alpha: alpha)

            let ring = NSBezierPath(ovalIn: NSRect(x: r.x - radius, y: r.y - radius, width: radius * 2, height: radius * 2))
            color.setStroke()
            ring.lineWidth = max(1.0, 2.8 * (1.0 - p * 0.6))
            ring.stroke()

            if p < 0.45 {
                let dotR: CGFloat = 4.5 * (1.0 - p)
                let dot = NSBezierPath(ovalIn: NSRect(x: r.x - dotR, y: r.y - dotR, width: dotR * 2, height: dotR * 2))
                color.withAlphaComponent(alpha * 0.65).setFill()
                dot.fill()
            }
        }

        guard cursorVisible else { return }
        let cx = max(4, min(bounds.width - 6, cursorCurrent.x))
        let cy = max(4, min(bounds.height - 6, cursorCurrent.y))

        NSGraphicsContext.saveGraphicsState()
        let shadow = NSShadow()
        shadow.shadowColor = NSColor(calibratedWhite: 0, alpha: 0.45)
        shadow.shadowBlurRadius = 4.0
        shadow.shadowOffset = NSSize(width: 0, height: -1.5)
        shadow.set()

        let arrow = NSBezierPath()
        arrow.move(to: NSPoint(x: cx, y: cy))
        arrow.line(to: NSPoint(x: cx, y: cy + 19.5))
        arrow.line(to: NSPoint(x: cx + 5.0, y: cy + 15.2))
        arrow.line(to: NSPoint(x: cx + 8.8, y: cy + 22.4))
        arrow.line(to: NSPoint(x: cx + 11.7, y: cy + 21.0))
        arrow.line(to: NSPoint(x: cx + 7.9, y: cy + 13.9))
        arrow.line(to: NSPoint(x: cx + 14.3, y: cy + 13.9))
        arrow.close()

        NSColor(calibratedRed: 0.03, green: 0.71, blue: 0.85, alpha: 0.98).setFill()
        arrow.fill()
        NSColor.white.setStroke()
        arrow.lineWidth = 1.7
        arrow.lineJoinStyle = .round
        arrow.stroke()

        let font = NSFont.systemFont(ofSize: 10.5, weight: .semibold)
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
        let labelStr = cursorBadge as NSString
        let textSz = labelStr.size(withAttributes: attrs)
        let pillW = textSz.width + 12.0
        let pillH: CGFloat = 18.0
        var pillX = cx + 14.0
        var pillY = cy + 15.0
        if pillX + pillW > bounds.width - 6 { pillX = cx - pillW - 6.0 }
        if pillY + pillH > bounds.height - 6 { pillY = cy - pillH - 4.0 }

        let pillRect = NSRect(x: pillX, y: pillY, width: pillW, height: pillH)
        let pillPath = NSBezierPath(roundedRect: pillRect, xRadius: 9, yRadius: 9)
        NSColor(calibratedRed: 0.05, green: 0.45, blue: 0.58, alpha: 0.94).setFill()
        pillPath.fill()
        NSColor(calibratedWhite: 1.0, alpha: 0.35).setStroke()
        pillPath.lineWidth = 0.8
        pillPath.stroke()
        NSGraphicsContext.restoreGraphicsState()

        labelStr.draw(
            at: NSPoint(x: pillX + 6.0, y: pillY + (pillH - textSz.height) / 2.0),
            withAttributes: attrs
        )
    }
}

// MARK: - Dynamic Island HUD Controller

final class HUDController: NSObject {
    let state = SharedHUDState()

    private let canvasWidth: CGFloat = 560.0
    private let canvasHeight: CGFloat = 210.0

    private var islandPanel: NotchPanel!
    private var islandView: DynamicIslandView!

    private var overlayPanel: NSPanel!
    private var cursorCanvas: AICursorCanvasView!

    private var previousFrontApp: NSRunningApplication?
    private var autoHideWorkItem: DispatchWorkItem?
    private var frameTimer: Timer?
    private var lastFrameTime: TimeInterval = CACurrentMediaTime()
    private var windowTrackTimer: Timer?

    func setupUI() {
        let metrics = ScreenIslandMetrics.current()

        // 1. Top-Center Hardware-Fused Dynamic Island Panel
        islandPanel = NotchPanel(
            contentRect: NSRect(
                x: round(metrics.centerX - canvasWidth / 2.0),
                y: round(metrics.screenFrame.maxY - canvasHeight),
                width: canvasWidth,
                height: canvasHeight
            ),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        islandPanel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.mainMenuWindow)) + 3)
        islandPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
        islandPanel.isFloatingPanel = true
        islandPanel.hidesOnDeactivate = false
        islandPanel.isOpaque = false
        islandPanel.backgroundColor = .clear
        islandPanel.hasShadow = false
        islandPanel.acceptsMouseMovedEvents = true
        islandPanel.ignoresMouseEvents = true

        islandView = DynamicIslandView(frame: NSRect(x: 0, y: 0, width: canvasWidth, height: canvasHeight))
        islandView.metrics = metrics
        islandView.widthSpring = SpringValue(metrics.notchWidth)
        islandView.heightSpring = SpringValue(max(24, metrics.notchHeight))
        islandView.onButtonTap = { [weak self] btnId in
            self?.handleIslandButton(btnId)
        }
        islandPanel.contentView = islandView

        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.reanchorToActiveScreen()
        }

        // 2. Transparent Click-Through AI Cursor Overlay Window
        overlayPanel = NSPanel(
            contentRect: NSRect(x: 100, y: 100, width: 600, height: 400),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        overlayPanel.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.floatingWindow)))
        overlayPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        overlayPanel.ignoresMouseEvents = true
        overlayPanel.isOpaque = false
        overlayPanel.backgroundColor = .clear
        overlayPanel.hasShadow = false
        overlayPanel.hidesOnDeactivate = false

        cursorCanvas = AICursorCanvasView(frame: overlayPanel.contentView!.bounds)
        cursorCanvas.autoresizingMask = [.width, .height]
        overlayPanel.contentView?.addSubview(cursorCanvas)

        state.writeStateToDisk()
    }

    private func reanchorToActiveScreen() {
        let m = ScreenIslandMetrics.current()
        islandView.metrics = m
        islandPanel.setFrame(
            NSRect(
                x: round(m.centerX - canvasWidth / 2.0),
                y: round(m.screenFrame.maxY - canvasHeight),
                width: canvasWidth,
                height: canvasHeight
            ),
            display: true
        )
        updateSpringTargets()
    }

    // MARK: - Spring Morphing & Hover Hit-Testing Loop (60fps when visible, 0% CPU when hidden)

    private func updateSpringTargets() {
        let m = islandView.metrics
        let nh = m.hasHardwareNotch ? m.notchHeight : 7.0

        switch islandView.mode {
        case .hidden:
            islandView.widthSpring.target = m.notchWidth
            islandView.heightSpring.target = m.hasHardwareNotch ? m.notchHeight : 26.0
            islandView.radiusSpring.target = 16.0
            islandView.contentAlphaSpring.target = 0.0
            islandView.expandAlphaSpring.target = 0.0

        case .authRequired, .permissionRequired:
            islandView.widthSpring.target = 478.0
            islandView.heightSpring.target = nh + 104.0
            islandView.radiusSpring.target = 24.0
            islandView.contentAlphaSpring.target = 1.0
            islandView.expandAlphaSpring.target = 1.0

        case .done, .stopped:
            islandView.widthSpring.target = 356.0
            islandView.heightSpring.target = nh + 36.0
            islandView.radiusSpring.target = 19.0
            islandView.contentAlphaSpring.target = 1.0
            islandView.expandAlphaSpring.target = 0.0

        case .working:
            if islandView.isHovered {
                islandView.widthSpring.target = 456.0
                islandView.heightSpring.target = nh + 74.0
                islandView.radiusSpring.target = 22.0
                islandView.contentAlphaSpring.target = 1.0
                islandView.expandAlphaSpring.target = 1.0
            } else {
                islandView.widthSpring.target = 376.0
                islandView.heightSpring.target = nh + 36.0
                islandView.radiusSpring.target = 19.0
                islandView.contentAlphaSpring.target = 1.0
                islandView.expandAlphaSpring.target = 0.0
            }
        }
    }

    private func ensureFrameLoop() {
        if !islandPanel.isVisible {
            reanchorToActiveScreen()
            islandPanel.orderFrontRegardless()
        }
        guard frameTimer == nil else { return }
        lastFrameTime = CACurrentMediaTime()
        frameTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            self.tickFrame()
        }
    }

    private func tickFrame() {
        let now = CACurrentMediaTime()
        let dt = CGFloat(max(1.0 / 240.0, min(0.05, now - lastFrameTime)))
        lastFrameTime = now

        // 1. Live non-activating mouse proximity & click-through hit-test
        if islandView.mode != .hidden {
            let mouseScreen = NSEvent.mouseLocation
            let winFrame = islandPanel.frame
            if winFrame.insetBy(dx: -20, dy: -20).contains(mouseScreen) {
                let localX = mouseScreen.x - winFrame.minX
                let localFlippedY = winFrame.maxY - mouseScreen.y
                let hitRect = islandView.currentIslandBodyRect().insetBy(dx: -10, dy: -6)
                let inside = hitRect.contains(NSPoint(x: localX, y: localFlippedY))
                if inside != islandView.isHovered {
                    islandView.isHovered = inside
                    islandPanel.ignoresMouseEvents = !inside
                    if !inside {
                        islandView.hoveredButtonId = nil
                        islandView.pressedButtonId = nil
                    }
                    updateSpringTargets()
                }
            } else if islandView.isHovered || !islandPanel.ignoresMouseEvents {
                islandView.isHovered = false
                islandView.hoveredButtonId = nil
                islandView.pressedButtonId = nil
                islandPanel.ignoresMouseEvents = true
                updateSpringTargets()
            }
        } else {
            islandPanel.ignoresMouseEvents = true
        }

        // 2. Step springs & waveform animations
        var moving = false
        if islandView.widthSpring.step(dt: dt) { moving = true }
        if islandView.heightSpring.step(dt: dt) { moving = true }
        if islandView.radiusSpring.step(dt: dt) { moving = true }
        if islandView.contentAlphaSpring.step(dt: dt) { moving = true }
        if islandView.expandAlphaSpring.step(dt: dt) { moving = true }

        if islandView.textSlideProgress < 1.0 {
            islandView.textSlideProgress = min(1.0, islandView.textSlideProgress + dt * 6.5)
            moving = true
        }

        if islandView.mode != .hidden {
            islandView.wavePhase += dt * 7.2
            islandView.glowPhase += dt * 3.4
            moving = true
        }

        let cursorAnimating = state.watching ? cursorCanvas.stepAnimation(now: now) : false
        islandView.needsDisplay = true

        // 3. Shut down 60fps loop cleanly once sucked back into the MacBook notch!
        if islandView.mode == .hidden && !moving && !cursorAnimating && islandView.contentAlphaSpring.current <= 0.02 {
            frameTimer?.invalidate()
            frameTimer = nil
            islandPanel.orderOut(nil)
        }
    }

    private func scheduleAutoHide(seconds: Double) {
        autoHideWorkItem?.cancel()
        guard seconds > 0 else { return }
        let item = DispatchWorkItem { [weak self] in
            guard let self = self else { return }
            if self.state.authActive || self.islandView.isHovered {
                self.scheduleAutoHide(seconds: 4.0)
                return
            }
            if self.state.watching {
                self.scheduleAutoHide(seconds: 8.0)
                return
            }
            self.suckIntoNotchAndHide()
        }
        autoHideWorkItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: item)
    }

    private func suckIntoNotchAndHide() {
        autoHideWorkItem?.cancel()
        stopWindowTracking()
        islandView.isHovered = false
        islandPanel.ignoresMouseEvents = true
        islandView.mode = .hidden
        updateSpringTargets()
        ensureFrameLoop()
    }

    // MARK: - Target Window Tracking & Pop-Out / Tuck-Back

    private func queryTargetQuartzBounds() -> CGRect? {
        let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let infoList = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else {
            return state.explicitBounds
        }

        var bestMatch: [String: Any]? = nil
        if state.targetWindowId != 0 {
            bestMatch = infoList.first {
                ($0[kCGWindowNumber as String] as? NSNumber)?.uint32Value == state.targetWindowId
            }
        }
        if bestMatch == nil && state.targetPid > 0 {
            let myPid = ProcessInfo.processInfo.processIdentifier
            let candidates = infoList.filter { item in
                guard let pidNum = item[kCGWindowOwnerPID as String] as? NSNumber,
                      let layerNum = item[kCGWindowLayer as String] as? NSNumber,
                      pid_t(pidNum.int32Value) == state.targetPid,
                      pid_t(pidNum.int32Value) != myPid,
                      layerNum.intValue == 0,
                      let b = item[kCGWindowBounds as String] as? [String: Any],
                      let w = (b["Width"] as? NSNumber)?.doubleValue,
                      let h = (b["Height"] as? NSNumber)?.doubleValue,
                      w >= 180, h >= 120 else { return false }
                return true
            }
            bestMatch = candidates.first
            if let num = (bestMatch?[kCGWindowNumber as String] as? NSNumber)?.uint32Value {
                state.targetWindowId = num
            }
        }

        if let m = bestMatch,
           let b = m[kCGWindowBounds as String] as? [String: Any],
           let x = (b["X"] as? NSNumber)?.doubleValue,
           let y = (b["Y"] as? NSNumber)?.doubleValue,
           let w = (b["Width"] as? NSNumber)?.doubleValue,
           let h = (b["Height"] as? NSNumber)?.doubleValue {
            return CGRect(x: x, y: y, width: w, height: h)
        }
        return state.explicitBounds
    }

    private func quartzToAppKit(_ qRect: CGRect) -> NSRect {
        let primaryH = NSScreen.screens.first?.frame.height ?? 900.0
        return NSRect(
            x: qRect.origin.x,
            y: primaryH - qRect.origin.y - qRect.size.height,
            width: qRect.size.width,
            height: qRect.size.height
        )
    }

    private func syncOverlayToTargetWindow() {
        guard state.watching, !state.authActive, let qRect = queryTargetQuartzBounds() else {
            overlayPanel.orderOut(nil)
            return
        }
        let nsRect = quartzToAppKit(qRect)
        if overlayPanel.frame != nsRect {
            overlayPanel.setFrame(nsRect, display: true)
            cursorCanvas.needsDisplay = true
        }
        if !overlayPanel.isVisible {
            overlayPanel.orderFrontRegardless()
        }
    }

    private func startWindowTracking() {
        syncOverlayToTargetWindow()
        guard windowTrackTimer == nil else { return }
        windowTrackTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            self?.syncOverlayToTargetWindow()
        }
    }

    private func stopWindowTracking() {
        windowTrackTimer?.invalidate()
        windowTrackTimer = nil
        overlayPanel.orderOut(nil)
    }

    private func capturePreviousAppIfNeeded() {
        let myPid = ProcessInfo.processInfo.processIdentifier
        if let front = NSWorkspace.shared.frontmostApplication,
           front.processIdentifier != myPid,
           front.processIdentifier != state.targetPid {
            previousFrontApp = front
        }
    }

    private func bringTargetAppToFront() {
        capturePreviousAppIfNeeded()
        if state.targetPid > 0, let app = NSRunningApplication(processIdentifier: state.targetPid) {
            app.unhide()
            app.activate(options: [.activateIgnoringOtherApps])
        } else if !state.targetAppName.isEmpty {
            let match = NSWorkspace.shared.runningApplications.first {
                ($0.localizedName ?? "").localizedCaseInsensitiveContains(state.targetAppName)
            }
            if let m = match {
                state.targetPid = m.processIdentifier
                m.unhide()
                m.activate(options: [.activateIgnoringOtherApps])
            }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
            self?.islandPanel.orderFrontRegardless()
        }
    }

    private func returnFocusToAntigravity() {
        if let prev = previousFrontApp, !prev.isTerminated {
            prev.activate(options: [.activateIgnoringOtherApps])
        } else {
            let agy = NSWorkspace.shared.runningApplications.first { app in
                let n = (app.localizedName ?? "").lowercased()
                let b = (app.bundleIdentifier ?? "").lowercased()
                return n.contains("antigravity") || b.contains("antigravity") || b.contains("jetski")
            }
            agy?.activate(options: [.activateIgnoringOtherApps])
        }
    }

    private func applyWatchTransition(watching: Bool) {
        if watching {
            state.watching = true
            state.writeStateToDisk()
            islandView.appBadgeText = "前台查看中"
            bringTargetAppToFront()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) { [weak self] in
                self?.startWindowTracking()
            }
        } else {
            state.watching = false
            state.writeStateToDisk()
            islandView.appBadgeText = state.targetAppName
            stopWindowTracking()
            returnFocusToAntigravity()
            scheduleAutoHide(seconds: 4.5)
        }
        updateSpringTargets()
        ensureFrameLoop()
    }

    // MARK: - Dynamic Island Button Actions

    private func handleIslandButton(_ id: String) {
        switch id {
        case "toggle_watch", "island_body":
            applyWatchTransition(watching: !state.watching)

        case "pop_win":
            bringTargetAppToFront()

        case "manual_auth":
            enterAuthTakeover(
                title: "已暂停交给你手动登录 / 操作",
                subtitle: "在弹出的窗口完成扫码或授权后，点下方「我已完成」切回后台继续",
                popWindow: true,
                playChime: false
            )

        case "open_prefs":
            if let url = URL(string: state.permissionSettingURL) {
                NSWorkspace.shared.open(url)
            }

        case "auth_done":
            completeAuthTakeover(text: "授权已完成 · 已切回后台继续执行")

        case "hide":
            state.authActive = false
            state.writeStateToDisk()
            suckIntoNotchAndHide()

        case "stop":
            state.stopTimestamp = Date().timeIntervalSince1970
            state.authActive = false
            state.watching = false
            state.writeStateToDisk()
            stopWindowTracking()
            islandView.mode = .stopped
            islandView.setStatusText("已紧急中断 AI 后台操作")
            updateSpringTargets()
            ensureFrameLoop()
            scheduleAutoHide(seconds: 1.3)

        default:
            break
        }
    }

    private func enterAuthTakeover(title: String, subtitle: String, popWindow: Bool, playChime: Bool) {
        autoHideWorkItem?.cancel()
        state.authActive = true
        state.writeStateToDisk()
        stopWindowTracking() // Never draw AI cursor overlay on top of user's login window!

        islandView.mode = .authRequired
        islandView.setStatusText(title)
        islandView.subtitleText = subtitle
        updateSpringTargets()
        ensureFrameLoop()

        if playChime {
            NSSound(named: NSSound.Name("Glass"))?.play()
        }
        if popWindow {
            bringTargetAppToFront()
        }
    }

    private func completeAuthTakeover(text: String) {
        let shouldRestoreFocus = (previousFrontApp != nil) || state.watching
        state.authActive = false
        state.watching = false
        state.authResolvedTimestamp = Date().timeIntervalSince1970
        state.writeStateToDisk()
        stopWindowTracking()
        if shouldRestoreFocus {
            returnFocusToAntigravity()
            previousFrontApp = nil
        }

        islandView.mode = .working
        islandView.appBadgeText = state.targetAppName
        islandView.subtitleText = ""
        islandView.setStatusText(text)
        updateSpringTargets()
        ensureFrameLoop()
        scheduleAutoHide(seconds: 5.5)
    }

    // MARK: - Incoming UDP IPC Command Handler

    private func num(_ dict: [String: Any], _ key: String) -> Double? {
        return (dict[key] as? NSNumber)?.doubleValue
    }

    func handleCommand(_ msg: [String: Any]) {
        let cmd = (msg["cmd"] as? String) ?? "status"

        if let pid = num(msg, "pid"), pid > 0 { state.targetPid = pid_t(pid) }
        if let wid = num(msg, "window_id"), wid > 0 { state.targetWindowId = CGWindowID(wid) }
        if let app = msg["app"] as? String, !app.isEmpty {
            state.targetAppName = app
            if !state.watching { islandView.appBadgeText = app }
        }
        if let bArr = msg["bounds"] as? [NSNumber], bArr.count == 4 {
            let b = bArr.map { $0.doubleValue }
            if b[2] > 20 && b[3] > 20 {
                state.explicitBounds = CGRect(x: b[0], y: b[1], width: b[2], height: b[3])
            }
        }
        if let insetTop = num(msg, "inset_top") { state.contentInsetTop = CGFloat(max(0, insetTop)) }
        if let csArr = msg["content_size"] as? [NSNumber], csArr.count == 2 {
            let cs = csArr.map { $0.doubleValue }
            if cs[0] > 20 && cs[1] > 20 {
                state.contentSize = CGSize(width: cs[0], height: cs[1])
            }
        }
        if let phase = msg["phase"] as? String, !phase.isEmpty {
            islandView.phaseTag = phase
        }

        switch cmd {
        case "hide":
            state.authActive = false
            state.watching = false
            state.writeStateToDisk()
            suckIntoNotchAndHide()

        case "auth":
            let title = (msg["title"] as? String) ?? "需要你完成登录 / 授权"
            let sub = (msg["text"] as? String) ?? "完成后点「我已完成」自动切回后台继续执行"
            let popWin = (msg["pop_window"] as? Bool) ?? true
            enterAuthTakeover(title: title, subtitle: sub, popWindow: popWin, playChime: true)

        case "auth_done":
            let txt = (msg["text"] as? String) ?? "检测到登录已完成 · 继续后台执行"
            completeAuthTakeover(text: txt)

        case "permission":
            autoHideWorkItem?.cancel()
            state.authActive = true
            if let prefUrl = msg["pref_url"] as? String, !prefUrl.isEmpty {
                state.permissionSettingURL = prefUrl
            }
            state.writeStateToDisk()
            islandView.mode = .permissionRequired
            islandView.setStatusText((msg["title"] as? String) ?? "需要开启 macOS 屏幕录制 / 辅助权限")
            islandView.subtitleText = (msg["text"] as? String) ?? "点击下方按钮打开系统设置勾选 Antigravity 后点「已开启」"
            NSSound(named: NSSound.Name("Glass"))?.play()
            updateSpringTargets()
            ensureFrameLoop()

        case "watch":
            let wantWatch = (msg["watching"] as? Bool) ?? ((num(msg, "watching") ?? 1) != 0)
            if let text = msg["text"] as? String, !text.isEmpty {
                islandView.setStatusText(text)
            }
            if islandView.mode == .hidden || islandView.mode == .done {
                islandView.mode = .working
            }
            applyWatchTransition(watching: wantWatch)
            scheduleAutoHide(seconds: wantWatch ? 12.0 : 5.0)

        case "cursor":
            if !state.authActive {
                islandView.mode = .working
                if let text = msg["text"] as? String, !text.isEmpty {
                    islandView.setStatusText(text)
                }
                updateSpringTargets()
                ensureFrameLoop()
                scheduleAutoHide(seconds: num(msg, "auto_hide_sec") ?? 5.2)
            }

            if state.watching && !state.authActive {
                syncOverlayToTargetWindow()
                let nx = CGFloat(num(msg, "nx") ?? 500.0)
                let ny = CGFloat(num(msg, "ny") ?? 500.0)
                let kind = (msg["kind"] as? String) ?? "move"
                let badge = (msg["badge"] as? String) ?? "AI"

                let winW = max(1, overlayPanel.frame.width)
                let winH = max(1, overlayPanel.frame.height)
                let topOff = min(winH * 0.35, state.contentInsetTop)
                let cw = state.contentSize?.width ?? winW
                let ch = state.contentSize?.height ?? (winH - topOff)
                let ox = max(0, (winW - cw) / 2.0)

                let px = ox + (max(0, min(1000, nx)) / 1000.0) * cw
                let py = topOff + (max(0, min(1000, ny)) / 1000.0) * ch
                let isClick = (kind == "click" || kind == "double_click" || kind == "right_click")
                cursorCanvas.moveTo(
                    point: CGPoint(x: px, y: py),
                    badge: badge,
                    spawnClickRipple: isClick,
                    isRightClick: kind == "right_click"
                )
            }

        default: // "status"
            if state.authActive { return }
            let isDone = (msg["done"] as? Bool) ?? false
            islandView.mode = isDone ? .done : .working
            let text = (msg["text"] as? String) ?? "正在后台执行..."
            islandView.setStatusText(text)
            updateSpringTargets()
            ensureFrameLoop()
            let hideSec = num(msg, "auto_hide_sec") ?? (isDone ? 2.2 : 5.5)
            scheduleAutoHide(seconds: hideSec)
            if state.watching { syncOverlayToTargetWindow() }
        }
    }
}

// MARK: - UDP Loopback Listener (127.0.0.1:47866)

func startUDPServer(port: UInt16, controller: HUDController) -> Bool {
    let fd = socket(AF_INET, SOCK_DGRAM, 0)
    guard fd >= 0 else { return false }

    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

    let bindRes = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    if bindRes != 0 {
        close(fd)
        return false
    }

    DispatchQueue.global(qos: .userInteractive).async {
        var buf = [UInt8](repeating: 0, count: 8192)
        while true {
            let n = recv(fd, &buf, buf.count, 0)
            if n <= 0 { continue }
            let data = Data(buf[0..<n])
            guard let json = try? JSONSerialization.jsonObject(with: data, options: []) as? [String: Any] else {
                continue
            }
            DispatchQueue.main.async {
                controller.handleCommand(json)
            }
        }
    }
    return true
}

func sendPacketToRunningInstance(port: UInt16, payload: [String: Any]) {
    let fd = socket(AF_INET, SOCK_DGRAM, 0)
    guard fd >= 0 else { return }
    defer { close(fd) }

    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))

    if let data = try? JSONSerialization.data(withJSONObject: payload, options: []) {
        _ = data.withUnsafeBytes { rawBuf in
            withUnsafePointer(to: &addr) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                    sendto(fd, rawBuf.baseAddress, data.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
    }
}

func playWelcomeDemoSequence(send: @escaping ([String: Any]) -> Void) {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
        send([
            "cmd": "status",
            "app": "AGY 灵动岛",
            "text": "灵动岛已挂载 · 后台操作实时律动显示",
            "done": false,
            "auto_hide_sec": 5.0
        ])
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.55) {
        send([
            "cmd": "auth",
            "title": "遇到登录 / 扫码 / 权限会自动在此唤醒你",
            "text": "支持一键弹出窗口、扫码完成自动切回后台继续执行",
            "pop_window": false
        ])
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 3.25) {
        send([
            "cmd": "auth_done",
            "text": "平时 100% 隐形于刘海 · 干活自动丝滑展开"
        ])
    }
    DispatchQueue.main.asyncAfter(deadline: .now() + 4.35) {
        send([
            "cmd": "status",
            "text": "✓ AGY 灵动岛 v3.1 已就绪",
            "done": true,
            "auto_hide_sec": 1.3
        ])
    }
}

// MARK: - Entry Point

let args = CommandLine.arguments
let port: UInt16 = UInt16(ProcessInfo.processInfo.environment["AGY_HUD_PORT"] ?? "47866") ?? 47866
let isDemo = args.contains("--demo")

let app = NSApplication.shared
app.setActivationPolicy(.accessory) // Zero Dock icon, zero menu bar clutter!

let controller = HUDController()
controller.setupUI()

if !startUDPServer(port: port, controller: controller) {
    if isDemo {
        playWelcomeDemoSequence { pkt in
            sendPacketToRunningInstance(port: port, payload: pkt)
        }
        RunLoop.main.run(until: Date().addingTimeInterval(5.8))
    }
    exit(0)
}

try? "\(ProcessInfo.processInfo.processIdentifier)\n".write(
    toFile: controller.state.pidFilePath,
    atomically: true,
    encoding: .utf8
)

if isDemo {
    playWelcomeDemoSequence { pkt in
        controller.handleCommand(pkt)
    }
}

app.run()
