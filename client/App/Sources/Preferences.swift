import Foundation
import Observation

/// 偏好 preferences — 只属于这台设备的选择。
///
/// **These are the settings that are not learning rules.** Type size, accent,
/// speech rate: none of it changes what is taught or when it comes back, so
/// none of it belongs on the server. Everything the server decides is read-only
/// on the phone and lives in its own section of the settings screen.
///
/// `UserDefaults` rather than a file: a dozen scalars, and losing them costs a
/// re-tap. The two credentials are the opposite kind of thing and live in the
/// keychain — see `Connection`.
///
/// **Why the properties are computed rather than stored.** `@Observable` turns
/// a stored property into a computed one when it expands, which leaves no room
/// for a `didSet` to write the value out. Reading from `UserDefaults` on every
/// get and bumping `revision` on every set gets both: the defaults database
/// stays the single source of truth, and the observation machinery still sees
/// a change. The cost is that any change invalidates every reader of this
/// object — on a settings screen, nothing.
@MainActor
@Observable
final class Preferences {
    enum Accent: String, CaseIterable, Identifiable {
        case american, british
        var id: String { rawValue }
        var label: String { self == .american ? "美音" : "英音" }
        /// 四六级听力两种口音都考，所以这不是个人喜好问题。
        var voiceCode: String { self == .american ? "en-US" : "en-GB" }
    }

    static let fontScaleRange: ClosedRange<Double> = 0.85...1.6
    static let speechRateRange: ClosedRange<Double> = 0.6...1.3

    /// Read by every getter, bumped by every setter. That is the whole trick.
    private var revision = 0

    @ObservationIgnored private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    /// 正文字号的倍数。**入口在阅读屏的 `···` 里，不在设置页**——调字号要看着
    /// 正文调，退出去看效果再回来改，是把两秒的动作做成一分钟。
    var fontScale: Double {
        get { number(.fontScale, default: 1.0, in: Self.fontScaleRange) }
        set { write(Self.fontScaleRange.clamped(newValue), .fontScale) }
    }

    var accent: Accent {
        get {
            _ = revision
            return Accent(rawValue: defaults.string(forKey: Key.accent.rawValue) ?? "")
                ?? .american
        }
        set { write(newValue.rawValue, .accent) }
    }

    /// 相对系统默认语速的倍数。原先写死 0.9，理由是「单词单独念时词尾听不清」——
    /// 那是为单个词定的，整句朗读时它偏慢，所以交给人自己定。
    var speechRate: Double {
        get { number(.speechRate, default: 0.9, in: Self.speechRateRange) }
        set { write(Self.speechRateRange.clamped(newValue), .speechRate) }
    }

    /// 默认关：这个 App 大概率在图书馆和地铁上用，点一下就出声会吓人一跳。
    var speakOnTap: Bool {
        get { flag(.speakOnTap) }
        set { write(newValue, .speakOnTap) }
    }

    /// DEBUG 是否落盘。平时关着——查一件事的时候才打开。
    var verboseLog: Bool {
        get { flag(.verboseLog) }
        set { write(newValue, .verboseLog) }
    }

    /// 开发者选项开出来了没有。**故意存在 `UserDefaults` 而不是钥匙串**：
    /// 删了 App 就该回到藏起来的状态，而钥匙串是活得比 App 长的东西。
    var developerUnlocked: Bool {
        get { flag(.developerUnlocked) }
        set { write(newValue, .developerUnlocked) }
    }

    // MARK: 内部

    private enum Key: String {
        case fontScale = "er.pref.fontScale"
        case accent = "er.pref.accent"
        case speechRate = "er.pref.speechRate"
        case speakOnTap = "er.pref.speakOnTap"
        case verboseLog = "er.pref.verboseLog"
        case developerUnlocked = "er.pref.developerUnlocked"
    }

    /// `double(forKey:)` answers 0 for a key that was never set, and a font
    /// scale of zero is an invisible screen — so absence is checked for rather
    /// than leaned on.
    private func number(_ key: Key, default fallback: Double,
                        in range: ClosedRange<Double>) -> Double {
        _ = revision
        guard let stored = defaults.object(forKey: key.rawValue) as? Double else {
            return fallback
        }
        return range.clamped(stored)
    }

    private func flag(_ key: Key) -> Bool {
        _ = revision
        return defaults.bool(forKey: key.rawValue)
    }

    private func write(_ value: Any, _ key: Key) {
        defaults.set(value, forKey: key.rawValue)
        revision &+= 1
    }
}

extension ClosedRange where Bound == Double {
    func clamped(_ value: Double) -> Double {
        Swift.min(Swift.max(value, lowerBound), upperBound)
    }
}
