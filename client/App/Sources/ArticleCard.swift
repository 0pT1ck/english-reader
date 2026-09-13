import Foundation
import ERContract

/// 列表上一张卡片要显示的东西。
///
/// **这是三个后端新字段唯一的落点。**话题、一句中文概括、待学数在契约里还没有
/// （方案 §7 的 1–3 条），所以下面三个属性现在恒为 nil / 0，界面照旧留着位置。
/// 后端加完字段、契约重新生成之后，要改的只有 `init(library:)` 这一个地方——
/// 界面一行都不用动。
///
/// 之所以不让视图直接读契约类型，就是为了让这件事只有一处。
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
        isRead = item.read_at != nil

        let isGenerated = item.source == "generated"

        // ↓↓↓ 后端字段加上、契约重新生成之后，改的就是这三行 ↓↓↓
        //
        //   topline = isGenerated ? (item.topic ?? "") : item.source_label
        //   summary = isGenerated ? item.summary_zh : nil
        //   pending = isGenerated ? item.pending_count : nil
        //
        // 在那之前：真题那一格现在就是对的（来源本来就有），
        // 生成文那两格空着——**空着而不是编一个**，编出来的话
        // 下一个人会以为这条路已经通了。
        topline = isGenerated ? "" : item.source_label
        summary = nil
        pending = nil
    }
}
