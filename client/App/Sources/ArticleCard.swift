import Foundation
import ERContract

/// 列表上一张卡片要显示的东西。
///
/// **这是三个后端字段唯一的落点**：话题、一句中文概括、待学数。
/// 2026-09-15 接上了——契约里 `topic` / `summary_zh` / `pending_count` 都在，
/// 而这三样此前一直是 nil，界面只是留着位置。改动全在 `init(library:)` 一处，
/// 视图一行都没动，这正是当初不让视图直接读契约类型的理由。
struct ArticleCard: Identifiable, Hashable {
    let id: Int
    let title: String
    let source: String
    let sourceLabel: String
    let wordCount: Int
    /// 真题显示来源（`六级 2019.06`），生成文显示话题（决定 2）。
    let topline: String
    /// 一句中文概括。**真题恒为 nil**，那一行留空（决定 3）。
    let summary: String?
    /// 「待学」= 这篇的目标词里还没进入学习流程的个数（决定 4、5）。
    /// **真题恒为 nil**：真题不为教任何词而写，`target_count` 恒为 0。
    let pending: Int?
    /// 备稿日期，`Sep 16` 这样。卡片第一行右端，和话题同一行。
    ///
    /// **真题也有**：它那一格填的是入库的时刻，项目里这个字段一直叫「备稿时间」
    /// （排序选项里就是这么写的），对真题同样成立，所以不像话题那样留空。
    /// 解析不出来就是空字符串——那一行本来也可以空着。
    let preparedLine: String
    let isRead: Bool

    var isExamPaper: Bool { source != "generated" }

    /// 「504 词 · 23 待学」。待学没有就只显示词数。
    var countsLine: String {
        var parts = ["\(wordCount) 词"]
        if let pending { parts.append("\(pending) 待学") }
        return parts.joined(separator: " · ")
    }

    /// 两档，不多不少（决定 7）。
    var stateLabel: String { isRead ? "已学习" : "待学习" }

    init(library item: Components.Schemas.LibraryArticle) {
        id = item.id
        title = item.title
        source = item.source
        sourceLabel = item.source_label
        wordCount = item.word_count
        preparedLine = Self.day(from: item.prepared_at)
        isRead = item.read_at != nil

        let isGenerated = item.source == "generated"

        // 真题显示来源（`六级 2019.06`），生成文显示话题。生成文没有话题时
        // 宁可空着——真题的来源填进生成文那一格，读的人会以为它是从哪儿来的。
        topline = isGenerated ? (item.topic ?? "") : item.source_label
        summary = isGenerated ? item.summary_zh : nil
        // 真题的 `target_count` 恒为 0（它不为教任何词而写），所以待学对它
        // 没有意义——显示 0 会被读成「都学完了」。
        pending = isGenerated ? item.pending_count : nil
    }

    /// `2026-09-15T11:13:10+00:00` → `Sep 16`。
    ///
    /// **按本地时区折算。**服务端存的是 UTC，而夜间备稿是凌晨四点跑的——
    /// 北京时间 09-16 04:00 在 UTC 上是 09-15 20:00，照 UTC 写就会比
    /// 「昨天夜里备的」整整差一天，而这一行正是给人看哪天备的。
    ///
    /// **locale 写死 `en_US_POSIX`**：跟着系统走的话中文机器上 `MMM`
    /// 出来的是「9月」，而要的格式是 `Sep 16`。
    ///
    /// Formatter 都是 computed 而不是 `static let`——它们不是 `Sendable`，
    /// 严格并发下存起来编译不过（`Log.swift` 里是同一个形状）。
    private static func day(from iso: String?) -> String {
        guard let iso else { return "" }
        let parsers: [ISO8601DateFormatter.Options] = [
            [.withInternetDateTime],
            // 服务端现在写的是秒级，但 `isoformat()` 换个 timespec 就会带上
            // 小数秒，而那时上面这个解析器只会静默返回 nil。
            [.withInternetDateTime, .withFractionalSeconds],
        ]
        for options in parsers {
            let parser = ISO8601DateFormatter()
            parser.formatOptions = options
            if let date = parser.date(from: iso) {
                let formatter = DateFormatter()
                formatter.locale = Locale(identifier: "en_US_POSIX")
                formatter.dateFormat = "MMM d"
                return formatter.string(from: date)
            }
        }
        return ""
    }
}
