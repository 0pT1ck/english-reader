import Foundation
import ERContract

/// 显示语义 display semantics
///
/// **What this file answers, and what it refuses to.** It says what a word *is*
/// — a target word, a derived one, out of range, marked as unknown. It says
/// nothing about underlines or colours. The line is 决定 17 and the reason is
/// 架构前提 1: "this word is out of range" is the same sentence on every client,
/// while "out of range is drawn as a grey dashed underline" is one client's
/// choice, and the terminal client cannot draw a dashed underline at all.
///
/// **Why it is not left to each client.** Two rules underneath look like
/// presentation and are not:
///
/// * A word can be several things at once, and the order decides which wins.
/// * A word inside a phrase must not show its own mark, because tapping
///   anywhere in a phrase opens the phrase — painting the word's mark there
///   points at a record you cannot reach from that spot.
///
/// The second was found by getting it wrong: the first version painted phrases
/// over their words and measurably lost information — 28 out-of-range words and
/// 34 derived words had their own marking overwritten.
///
/// **This project already has two clients** (the terminal one here, the phone
/// one next), so a rule left to the client is a rule written twice.
public enum WordRole: Equatable, Sendable {
    /// A name. Explicitly dismissable — you are not expected to learn it.
    case properNoun
    /// Outside the syllabus. **Only ever reported for generated articles**:
    /// meeting an unknown word in an exam paper is the skill being practised,
    /// so flagging them there would be answering a question nobody asked.
    case beyondSyllabus
    /// This article was written to teach this word.
    case target
    /// Built from a root you may know; shown with its word-formation breakdown
    /// rather than as a new item to memorise.
    case derived
    /// Nothing special about it.
    case plain
}

/// A mark the learner put on something.
public enum MarkKind: String, Equatable, Sendable {
    case unknown
    case fuzzy
}

/// Everything a client needs to decide how to draw one token — expressed as
/// facts, not as instructions.
public struct TokenDisplay: Equatable, Sendable {
    public let role: WordRole
    /// The learner's mark on this word, if any. **Nil whenever the token is
    /// inside a phrase**, even when the word itself is marked: see `inPhrase`.
    public let mark: MarkKind?
    /// This token is part of a confirmed phrase. Two consequences, and both are
    /// semantics rather than styling: the word's own gloss would be the wrong
    /// meaning here, and its own mark is unreachable from this spot.
    public let inPhrase: Bool
    /// The headword to look up, or nil for punctuation.
    public let headword: String?

    public init(role: WordRole, mark: MarkKind?, inPhrase: Bool, headword: String?) {
        self.role = role
        self.mark = mark
        self.inPhrase = inPhrase
        self.headword = headword
    }
}

/// A phrase, which is one item and is marked as one.
public struct PhraseDisplay: Equatable, Sendable {
    public let phrase: String
    /// Token sequence numbers, inclusive. The span covers the words *and the
    /// space between them* — 跨 Phase 不变量: a phrase is one thing, so
    /// selecting and marking happen to the whole of it, on every client.
    public let startSeq: Int
    public let endSeq: Int
    /// **这一处用的是哪条义项**（P11 决定 ③）。词组从「一个整体一行中文」
    /// 变成了「一个整体带几条义项」，而标记跟着义项走，跟单词一模一样。
    public let senseId: Int
    /// 这一处那条义项上的标记。**不是「这个词组被标过没有」**——
    /// 标过 `think of` 的「想起」不等于 `think of` 的「考虑」也标过。
    public let mark: MarkKind?

    public init(phrase: String, startSeq: Int, endSeq: Int,
                senseId: Int = 0, mark: MarkKind?) {
        self.phrase = phrase
        self.startSeq = startSeq
        self.endSeq = endSeq
        self.senseId = senseId
        self.mark = mark
    }

    public func covers(_ seq: Int) -> Bool { seq >= startSeq && seq <= endSeq }
}

/// Turns one article's payload into display decisions.
public struct ArticleDisplay: Sendable {
    public let tokens: [TokenDisplay]
    public let phrases: [PhraseDisplay]
    /// Whether out-of-range words are called out at all in this article.
    public let marksBeyond: Bool

    /// The order a word's roles are resolved in. First match wins.
    ///
    /// Derived last on purpose: a word can be both derived and a target, and
    /// "this article is teaching you this" is the more useful thing to say.
    static func role(
        kind: String,
        beyond: Bool,
        isTarget: Bool,
        hasDerivation: Bool,
        marksBeyond: Bool
    ) -> WordRole {
        if kind == "proper" { return .properNoun }
        if beyond && marksBeyond { return .beyondSyllabus }
        if isTarget { return .target }
        if hasDerivation { return .derived }
        return .plain
    }

    public init(article: Components.Schemas.ArticleResponse) {
        let marksBeyond = article.article.mark_beyond ?? false
        self.marksBeyond = marksBeyond

        let glossary = article.glossary?.additionalProperties ?? [:]
        let phraseList = article.phrases ?? []

        self.phrases = phraseList.map { phrase in
            // 服务端只下发「这一处确实是词组」的那些（`sense_id > 0`），
            // 所以这里不用再判一次；`0` 只会在老缓存里出现。
            let senseId = phrase.sense_id ?? 0
            return PhraseDisplay(
                phrase: phrase.phrase,
                startSeq: phrase.start_seq,
                endSeq: phrase.end_seq,
                senseId: senseId,
                mark: phrase.marks?.additionalProperties[String(senseId)]
                    .flatMap(MarkKind.init(rawValue:))
            )
        }
        let phrases = self.phrases

        self.tokens = (article.tokens ?? []).map { token in
            let entry = token.headword.flatMap { glossary[$0] }
            let inPhrase = token.in_phrase || phrases.contains { $0.covers(token.seq) }

            // The word's own mark, looked up by the sense on screen. The
            // glossary carries marks for *every* sense of the word, not only
            // this one, so that a tap can say "you marked another sense of
            // this" — reading an unmarked word you know you marked is the app
            // looking like it forgot.
            var mark: MarkKind?
            if !inPhrase, let entry {
                let senseKey = String(token.sense_id ?? 0)
                mark = entry.marks.additionalProperties[senseKey].flatMap(MarkKind.init(rawValue:))
            }

            return TokenDisplay(
                role: Self.role(
                    kind: token.kind,
                    beyond: token.beyond,
                    isTarget: token.is_target,
                    hasDerivation: entry?.derivation != nil,
                    marksBeyond: marksBeyond
                ),
                mark: mark,
                inPhrase: inPhrase,
                headword: token.headword
            )
        }
    }

    /// Which item a tap at this token belongs to.
    ///
    /// **Tapping either half of a phrase selects the whole phrase.** Not a
    /// rendering choice: the phrase is the item that gets marked, and a client
    /// that resolved a tap to the inner word would record a mark against the
    /// wrong thing. The first version of the web reader opened the phrase panel
    /// but highlighted half a word — saying the same wrong thing more quietly.
    public func target(at seq: Int) -> TapTarget? {
        if let phrase = phrases.first(where: { $0.covers(seq) }) {
            return .phrase(phrase)
        }
        guard seq >= 0, seq < tokens.count else { return nil }
        let token = tokens[seq]
        guard let headword = token.headword else { return nil }
        return .word(headword, token)
    }

    public enum TapTarget: Equatable, Sendable {
        case word(String, TokenDisplay)
        case phrase(PhraseDisplay)
    }
}

/// Which gloss to show for a tapped token.
///
/// One rule, and it is the whole reason this is not left to the UI: **a word
/// inside a phrase must show the phrase's meaning, not its own.** The token's
/// sense was annotated without knowing it was part of a phrase, so showing the
/// word's gloss there is not merely unhelpful — `account` reads 账目 while the
/// sentence says `account for`.
public enum GlossChoice: Equatable, Sendable {
    case word(String)
    case phrase(String)
    /// Punctuation, or a token with nothing to look up.
    case none
}

extension ArticleDisplay {
    public func gloss(at seq: Int) -> GlossChoice {
        switch target(at: seq) {
        case .phrase(let phrase): return .phrase(phrase.phrase)
        case .word(let headword, _): return .word(headword)
        case nil: return .none
        }
    }
}
