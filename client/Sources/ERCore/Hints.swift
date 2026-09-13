import Foundation
import ERContract

/// 提示分级 graded hints
///
/// **Three levels per direction, and the level reached is the score.** Opening
/// a hint is not a neutral act — the server turns `revealed` into a grade, so
/// which hints exist and in what order is a rule, not a presentation choice.
/// Hence Core, per 决定 17: an Android client would need the same three, and
/// the terminal one needs them too.
///
/// **The answer is whatever the direction asked for.** 看词想义 asks what the
/// word means here, so its last resort is the Chinese sense; 看义想词 asks which
/// word fills the blank, so its last resort is the word itself. The first
/// version of the web reader showed the English word in both — answering a
/// question nobody asked, in one of the two cases — and its own comment says
/// that is the sort of mistake only a real run finds. Written down here so the
/// next client does not have to find it again.
public struct Hint: Equatable, Sendable {
    /// 0-based: opening the first hint means `revealed` becomes 1.
    public let level: Int
    public let label: String
    public let body: String
    /// Set when the deepest 看词想义 hint can send the reader back to the
    /// article the word was met in. Absent for generated sentences.
    public let articleId: Int?

    public init(level: Int, label: String, body: String, articleId: Int? = nil) {
        self.level = level
        self.label = label
        self.body = body
        self.articleId = articleId
    }
}

public enum HintLadder {
    /// How far the hints go. Matches the server's `MAX_REVEAL`, and the review
    /// vectors assert the two agree.
    public static let maxReveal = 3

    /// The ladder for one question.
    ///
    /// - Parameters:
    ///   - item: today's queue row, with its sentences already attached.
    ///   - asked: the sentence being asked with — the "original" hint has to
    ///     avoid repeating it, or level one would show what is already on screen.
    ///   - direction: which way round the question is.
    public static func hints(
        for item: Components.Schemas.ReviewItem,
        asked: Components.Schemas.SentenceCard?,
        direction: ReviewDirection
    ) -> [Hint] {
        let original = item.hints.first { $0.id != asked?.id } ?? item.hints.first
        let gloss = chineseGloss(item)

        switch direction {
        case .wordToSense:
            return [
                Hint(level: 1, label: "当初读到它的那句",
                     body: original.map(\.text) ?? "（没有别的原句了）",
                     articleId: original?.article_id),
                Hint(level: 2, label: "中文", body: gloss.isEmpty ? "（无）" : gloss),
                Hint(level: 3,
                     label: original?.article_id != nil ? "跳回原文" : "英文定义",
                     body: original?.article_id != nil
                        ? "第 \(original!.article_id!) 篇"
                        : (item.sense?.concept_en ?? "（无）"),
                     articleId: original?.article_id),
            ]
        case .senseToWord:
            return [
                Hint(level: 1, label: "首字母",
                     body: (asked?.first_letter ?? String(item.item_key.prefix(1))) + "＿＿＿"),
                Hint(level: 2, label: "当初读到它的那句",
                     body: original.map(\.text) ?? "（没有别的原句了）",
                     articleId: original?.article_id),
                // The word itself, not its English definition: this direction
                // asked which word fills the blank.
                Hint(level: 3, label: "答案", body: asked?.surface ?? item.item_key),
            ]
        }
    }

    /// The Chinese sense, flattened. `gloss_zh` arrives as a list or a single
    /// string because both shapes exist in the real data.
    public static func chineseGloss(_ item: Components.Schemas.ReviewItem) -> String {
        guard let value = item.sense?.gloss_zh else { return "" }
        if let list = value.value1 { return list.joined(separator: "；") }
        return value.value2 ?? ""
    }

    /// What the question itself says.
    ///
    /// Here rather than in the renderer for the same reason as the hints: which
    /// way round the question is asked is the rule the whole direction system
    /// rests on, and a client that got it backwards would be grading the wrong
    /// skill while looking perfectly normal.
    public static func prompt(
        for item: Components.Schemas.ReviewItem,
        asked: Components.Schemas.SentenceCard?,
        direction: ReviewDirection
    ) -> String {
        switch direction {
        case .wordToSense:
            // The word in the sentence it was met in, if there is one — the
            // point of the sentence pool is that a word is recalled in context
            // rather than off a card.
            if let asked { return asked.text }
            return item.item_key
        case .senseToWord:
            guard let asked else { return chineseGloss(item) }
            return blanked(asked)
        }
    }

    /// The asked sentence with the target word blanked out.
    ///
    /// The span comes from the server (`blank_start`/`blank_end`) rather than
    /// being searched for here: the word may appear more than once, and in an
    /// inflected form that a naive search would miss or blank in the wrong
    /// place.
    public static func blanked(_ sentence: Components.Schemas.SentenceCard) -> String {
        let text = Array(sentence.text)
        let start = max(0, min(sentence.blank_start, text.count))
        let end = max(start, min(sentence.blank_end, text.count))
        return String(text[0..<start]) + "＿＿＿" + String(text[end...])
    }
}
