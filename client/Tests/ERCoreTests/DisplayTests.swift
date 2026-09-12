import Foundation
import Testing
import ERContract
@testable import ERCore

/// Display semantics, checked against a real article rather than a constructed
/// one — the rules being tested are about how several facts about a word
/// interact, and a hand-built token has only the facts I remembered to put in.
struct DisplayTests {
    static func day() throws -> Components.Schemas.TodayResponse {
        let url = Bundle.module.url(forResource: "Fixtures/today", withExtension: "json")!
        return try JSONDecoder().decode(
            Components.Schemas.TodayResponse.self, from: Data(contentsOf: url))
    }

    static func firstArticleWithPhrases() throws -> Components.Schemas.ArticleResponse {
        let day = try day()
        return day.articles.first { !($0.phrases ?? []).isEmpty } ?? day.articles[0]
    }

    @Test func rolesFollowTheStatedOrder() {
        // A word is often several of these at once and exactly one wins. The
        // order is the rule; the colours are not.
        #expect(ArticleDisplay.role(kind: "proper", beyond: true, isTarget: true,
                                    hasDerivation: true, marksBeyond: true) == .properNoun)
        #expect(ArticleDisplay.role(kind: "word", beyond: true, isTarget: true,
                                    hasDerivation: true, marksBeyond: true) == .beyondSyllabus)
        #expect(ArticleDisplay.role(kind: "word", beyond: false, isTarget: true,
                                    hasDerivation: true, marksBeyond: true) == .target)
        #expect(ArticleDisplay.role(kind: "word", beyond: false, isTarget: false,
                                    hasDerivation: true, marksBeyond: true) == .derived)
        #expect(ArticleDisplay.role(kind: "word", beyond: false, isTarget: false,
                                    hasDerivation: false, marksBeyond: true) == .plain)
    }

    @Test func outOfRangeIsOnlyCalledOutWhereItWasAsked() {
        // 真题里碰到生词正是要练的那件事，所以真题不标超纲词。同一个 token，
        // 只因为所在文章不同，答案就不同——这说明它是文章的属性，不是词的。
        #expect(ArticleDisplay.role(kind: "word", beyond: true, isTarget: false,
                                    hasDerivation: false, marksBeyond: true) == .beyondSyllabus)
        #expect(ArticleDisplay.role(kind: "word", beyond: true, isTarget: false,
                                    hasDerivation: false, marksBeyond: false) == .plain)
    }

    @Test func realArticleProducesOneDecisionPerToken() throws {
        let article = try Self.firstArticleWithPhrases()
        let display = ArticleDisplay(article: article)
        #expect(display.tokens.count == (article.tokens ?? []).count)
        #expect(display.tokens.contains { $0.role == .target },
                "生成文是为教一批词而写的，总该有目标词")
    }

    @Test func tappingEitherHalfOfAPhraseSelectsTheWholeThing() throws {
        let article = try Self.firstArticleWithPhrases()
        let display = ArticleDisplay(article: article)
        guard let phrase = display.phrases.first else {
            #expect(Bool(false), "样本里没有词组，这条就验不了——重抓一份带词组的")
            return
        }

        // 跨 Phase 不变量：词组是一个整体，选中与标记按整体发生。
        for seq in phrase.startSeq...phrase.endSeq {
            #expect(display.target(at: seq) == .phrase(phrase),
                    "点第 \(seq) 个 token 应该选中整个词组，而不是里面那半个词")
        }
    }

    @Test func aWordInsideAPhraseShowsThePhrasesMeaning() throws {
        let article = try Self.firstArticleWithPhrases()
        let display = ArticleDisplay(article: article)
        guard let phrase = display.phrases.first else { return }

        // 这个 token 的义项标注是在不知道它属于某个词组的情况下做的，所以
        // 在这里显示这个词自己的释义就是在给错的意思——`account` 会读成「账目」，
        // 而句子说的是 `account for`。
        #expect(display.gloss(at: phrase.startSeq) == .phrase(phrase.phrase))
        #expect(display.gloss(at: phrase.endSeq) == .phrase(phrase.phrase))
    }

    @Test func aWordInsideAPhraseDoesNotShowItsOwnMark() throws {
        let article = try Self.firstArticleWithPhrases()
        let display = ArticleDisplay(article: article)
        guard let phrase = display.phrases.first else { return }

        for seq in phrase.startSeq...phrase.endSeq where seq < display.tokens.count {
            #expect(display.tokens[seq].inPhrase)
            #expect(display.tokens[seq].mark == nil,
                    "词组里不画词自己的标记——点这里打开的是词组，画了就是指向一个你够不着的记录")
        }
    }

    @Test func punctuationHasNothingToLookUp() throws {
        let article = try Self.firstArticleWithPhrases()
        let display = ArticleDisplay(article: article)
        let punctuation = (article.tokens ?? []).enumerated()
            .first { $0.element.headword == nil && $0.element.kind != "word" }
        guard let punctuation else { return }
        #expect(display.gloss(at: punctuation.offset) == .none)
    }

    @Test func outOfRangeTapsReturnNothingRatherThanCrash() throws {
        let display = ArticleDisplay(article: try Self.firstArticleWithPhrases())
        #expect(display.target(at: -1) == nil)
        #expect(display.target(at: 10_000) == nil)
    }
}
