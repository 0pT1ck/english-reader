#if DEBUG
import Foundation
import ERCore

/// 模拟器开发用的启动开关（2026-09-23，P12）。**只在 Debug 构建里存在。**
///
/// 命令行点不了模拟器的屏幕，于是界面改动在本地走不到「打开一篇文章、点一个词」
/// 那一步——只能推 TestFlight 等真机，那正是坑 §7.7 的形状。这几个开关让
/// `xcrun simctl launch` 直接把 App 带到要看的那一屏：
///
///     SIMCTL_CHILD_ER_DEV_BASE_URL=http://127.0.0.1:8000 \
///     SIMCTL_CHILD_ER_DEV_TOKEN=… \
///     SIMCTL_CHILD_ER_DEV_OPEN_ARTICLE=508 \
///     SIMCTL_CHILD_ER_DEV_TAP_SEQ=42 \
///     xcrun simctl launch <udid> com.optick.englishreader
///
/// **按序号点而不按坐标点**：坐标跟着字号与排版变，token 序号不变，
/// 同一条命令每次都落在同一个词上。
///
/// 地址与令牌那两个在 `Connection.seedFromEnvironment()` 里读，
/// 因为它们要走 `update()` 那条存进钥匙串的路。
enum DevLaunch {
    private static var env: [String: String] { ProcessInfo.processInfo.environment }

    /// 列表加载完就推进这一篇。
    static var openArticle: Int? { env["ER_DEV_OPEN_ARTICLE"].flatMap(Int.init) }

    /// 文章打开后点这个 token。
    static var tapSeq: Int? { env["ER_DEV_TAP_SEQ"].flatMap(Int.init) }

    /// 文章打开后滚到第几段（段落 id），看滚过标题之后栏里是什么样。
    static var scrollParagraph: Int? { env["ER_DEV_SCROLL_PARAGRAPH"].flatMap(Int.init) }

    /// 点词面板一出来就是展开那一档（看用法、其他释义）。
    static var expandLookup: Bool { env["ER_DEV_EXPAND"] == "1" }

    /// 词组面板一出来就打开「这个词本身」。
    static var openInner: Bool { env["ER_DEV_INNER"] == "1" }

    /// 点完之后在列表里选中这条义项（`ER_DEV_INNER=1` 时选的是「这个词本身」里的）。
    static var pickSense: Int? { env["ER_DEV_PICK"].flatMap(Int.init) }

    /// 点完（选完）之后把滑块拨到这一档：`unknown` / `fuzzy`，或者 `known`（＝撤销标记）。
    static var markRequested: Bool { env["ER_DEV_MARK"] != nil }
    static var markKind: MarkKind? { env["ER_DEV_MARK"].flatMap(MarkKind.init(rawValue:)) }

    /// 一开屏落在哪一格：`review` / `settings`，不给就是阅读。
    static var startTab: String? { env["ER_DEV_TAB"] }

    /// 复习屏载入后直接开始这一池：`today` / `due`。
    static var reviewBucket: Projection.Bucket? {
        env["ER_DEV_REVIEW"].flatMap(Projection.Bucket.init(rawValue:))
    }

    /// 开始之后直接翻到揭晓屏（`answer` 只切屏，**不记事件**——记事件在 `advance`）。
    static var revealAnswer: Bool { env["ER_DEV_REVEAL"] == "1" }
}
#endif
