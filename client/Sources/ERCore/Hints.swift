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
    ///
    /// **One level as of P7.** 「不确定」 is a single tap: the whole hint appears
    /// and the three buttons become two. Three levels were a ladder the
    /// interface never wanted — and the old middle rung of the 看义想词 ladder
    /// handed over the answer a level early, because it printed the original
    /// sentence unblanked and the word is in it.
    public static let maxReveal = 1

    /// The hint for one question. One element, or none when there is nothing
    /// useful left to offer.
    ///
    /// - Parameters:
    ///   - item: today's queue row, with its sentences already attached.
    ///   - asked: the sentence being asked with — the hint has to avoid
    ///     repeating it, or it would show what is already on screen.
    ///   - direction: which way round the question is.
    public static func hints(
        for item: Components.Schemas.ReviewItem,
        asked: Components.Schemas.SentenceCard?,
        direction: ReviewDirection
    ) -> [Hint] {
        let original = item.hints.first { $0.id != asked?.id } ?? item.hints.first
        // Falling back to another question sentence keeps 「不确定」 from being a
        // button that does nothing: a word marked in an article you have not
        // finished may have no hint sentence yet.
        let spare = item.questions.first { $0.id != asked?.id }
        let source = original ?? spare
        // Only a real hint sentence has a provenance worth showing — a spare
        // question sentence is one you have not read, so "——文章a" would be a lie.
        let label = original?.article_title.map { "——\($0)" } ?? "另一句"

        switch direction {
        case .wordToSense:
            guard let source else { return [] }
            return [Hint(level: 1, label: label, body: source.text,
                         articleId: original?.article_id)]

        case .senseToWord:
            // **The sentence is always blanked in this direction**, whichever
            // pool it came from. The question asks which English word fits, and
            // the word is right there in the sentence — printing it raw is the
            // answer, not a hint. Written as a rule rather than as a fix to one
            // rung, so that adding a level back cannot reintroduce it.
            //
            // The first letter rides along in the same level rather than being
            // a level of its own: P3 决定 22 added it to settle "着手解决 could
            // also be deal with", and a Chinese sentence makes that worse, not
            // better — 「估计」 fits estimate, reckon, figure and calculate
            // equally well, and context alone does not choose between them.
            let firstLetter = (asked?.first_letter ?? String(item.item_key.prefix(1)))
                + "＿＿＿"
            guard let source else {
                return [Hint(level: 1, label: "首字母", body: firstLetter)]
            }
            return [Hint(level: 1, label: label,
                         body: blanked(source) + "\n\n" + firstLetter,
                         articleId: original?.article_id)]
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
            // **The Chinese sentence, not a blanked English one** (P7 决定 15).
            // Filling a blank is a production task, and the whole system is
            // aimed at "read it and recognise it" — asking which English word a
            // Chinese sentence needs tests exactly that, and gives away nothing
            // (P3 决定 20 worried about English definitions leaking the answer;
            // Chinese cannot leak an English word).
            //
            // Falls back to the blanked English while a sentence has no
            // translation yet — that is a real state, not an error, and it is
            // better than an empty question.
            if let zh = asked.text_zh, !zh.isEmpty { return zh }
            return blanked(asked)
        }
    }

    /// Which sentence this question is asked with.
    ///
    /// **A rule, not a convenience** — which is why it lives here rather than in
    /// each client. 看中文想英文 asks with the Chinese, so a sentence that has
    /// not been translated yet would fall back to a blanked English one, and
    /// that is a different question being asked under the same name. Preferring
    /// a translated sentence keeps the direction honest; falling back keeps the
    /// day running while the translation batch catches up.
    public static func askedSentence(
        for item: Components.Schemas.ReviewItem,
        direction: ReviewDirection
    ) -> Components.Schemas.SentenceCard? {
        let all = item.questions + item.hints
        switch direction {
        case .wordToSense:
            return all.first
        case .senseToWord:
            return all.first { ($0.text_zh?.isEmpty == false) } ?? all.first
        }
    }

    /// Where the target sits inside the Chinese prompt, for the highlight.
    ///
    /// Nil is an ordinary answer: some words have no clean Chinese span, and a
    /// span pointing at the wrong characters would split 估计 down the middle
    /// with nothing anywhere reporting it.
    public static func promptHighlight(
        _ asked: Components.Schemas.SentenceCard?,
        direction: ReviewDirection
    ) -> Range<Int>? {
        guard direction == .senseToWord, let asked,
              let zh = asked.text_zh, !zh.isEmpty,
              let start = asked.zh_start, let end = asked.zh_end,
              start >= 0, end > start, end <= zh.count else { return nil }
        return start..<end
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
