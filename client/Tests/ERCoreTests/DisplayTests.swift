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

/// 词性显示取短（P12，2026-09-23）。
///
/// 真机截图上点词面板显示的是柯林斯的英文语法标记（`ADJ-GRADED`、`COLOUR`）；
/// 换成中文之后，10% 的义项（2,502 条）挂着一句 20 字的**解释**：
/// 「能被表示程度的副词或介词词组修饰的形容词」。库里的值是柯林斯原文，
/// 不动；显示时取短。
struct PartOfSpeechTests {
    @Test func theLongExplanationShowsAsTheWordClassItExplains() {
        #expect(PartOfSpeech.short("能被表示程度的副词或介词词组修饰的形容词") == "形容词")
    }

    @Test func aListShowsItsFirstLabel() {
        // 柯林斯把几个标记用「；」并起来。第一个是它的主标记。
        #expect(PartOfSpeech.short("可数名词；头衔名词；称呼名词") == "可数名词")
        #expect(PartOfSpeech.short("名称名词；名称名词") == "名称名词")
        #expect(PartOfSpeech.short("不可数名词;复数名词") == "不可数名词")
    }

    @Test func anOrdinaryLabelIsLeftAlone() {
        // 阴性对照：这一条要是也被改了，上面两条过了也说明不了规则对。
        #expect(PartOfSpeech.short("可数名词") == "可数名词")
        #expect(PartOfSpeech.short("及物动词") == "及物动词")
        #expect(PartOfSpeech.short("形容词比较级形式") == "形容词比较级形式")
    }

    @Test func emptyMeansNothingToShow() {
        // 空的词性标签在屏幕上是一块解释不了的空白，所以空就是「不显示」。
        #expect(PartOfSpeech.short(nil) == nil)
        #expect(PartOfSpeech.short("") == nil)
        #expect(PartOfSpeech.short("  ") == nil)
    }
}

/// 标记必须落在一条真实的义项上（P12 决定 ⑯）。
///
/// 起因：真机上标了人名 Green，记下的是「义项 0」——造不出句子、永远进不了
/// 当天的复习名单，而屏幕上虚线照亮。本机语料里能点的 token 有 55% 会这样
/// （专有名词、功能词、标注判「都不贴合」的、没有义项集的）。
struct MarkTargetTests {
    @Test func aTrustworthyContextSenseIsMarkedDirectly() {
        // 阳性：决定 19 不变——本句中有真义项，滑块标的就是它。
        #expect(MarkTarget.resolve(kind: "content", senseId: 24657,
                                   senseIds: [24656, 24657], inPhrase: false)
                == .contextSense(24657))
    }

    @Test func properNounsAreNotMarkable() {
        // David Green 的 Green。给颜色 green 的释义就是给错的意思。
        #expect(MarkTarget.resolve(kind: "proper", senseId: nil,
                                   senseIds: [1, 2, 3], inPhrase: false) == .properNoun)
    }

    @Test func noContextSenseMeansThePersonPicks() {
        // 功能词从不标注（nil）、标注判「都不贴合」（-1）、老数据的 0：
        // 词有义项列表，就让人在列表里挑。
        for senseId in [nil, 0, -1] as [Int?] {
            #expect(MarkTarget.resolve(kind: "function", senseId: senseId,
                                       senseIds: [7, 8], inPhrase: false) == .pickFromList)
        }
    }

    @Test func aSenseThatIsNotOnTheListIsNotTrusted() {
        // 标着一个不在这个词现行义项里的号（退休了的、或者别处漏过来的）——
        // 当它是本句中去标，标出来的东西在屏幕上就对不上任何一行。
        #expect(MarkTarget.resolve(kind: "content", senseId: 99,
                                   senseIds: [7, 8], inPhrase: false) == .pickFromList)
    }

    @Test func aWordInsideAPhraseIsAlwaysPicked() {
        // 它自己的标注是在不知道自己在词组里的情况下做的（`in_phrase` 的契约说明）。
        #expect(MarkTarget.resolve(kind: "content", senseId: 7,
                                   senseIds: [7, 8], inPhrase: true) == .pickFromList)
    }

    @Test func aWordWithNoSensesCannotBeMarked() {
        #expect(MarkTarget.resolve(kind: "content", senseId: 0,
                                   senseIds: [], inPhrase: false) == .noSenses)
    }

    @Test func everyOutcomeThatMarksLandsOnARealSense() {
        // 规则本身的不变量：凡是给出一个义项号的，号一定 > 0 而且在列表里。
        // 只测上面几条的话，一个「什么都不让标」的实现也会过（阴性对照的另一半）。
        let kinds = ["content", "function", "proper"]
        let ids: [Int?] = [nil, -1, 0, 7, 99]
        for kind in kinds { for id in ids { for inPhrase in [false, true] {
            if case .contextSense(let s) = MarkTarget.resolve(
                kind: kind, senseId: id, senseIds: [7, 8], inPhrase: inPhrase) {
                #expect(s > 0 && [7, 8].contains(s))
            }
        } } }
    }
}
