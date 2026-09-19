import Foundation
import Testing
@testable import ERCore
import ERContract

/// 考句池与提示池的划分。
///
/// **这三条规则原本在服务端**（`sentences.split_pools`），P9 把它们搬过来:
/// 造句子是工厂的活，而「这一句该当考题还是当提示」取决于你读过什么，
/// 那是学习记录。所以服务端把一个词的句子原样全给，这边来分。
struct SentencePoolTests {

    static func card(_ id: Int, source: String,
                     articleId: Int? = nil, sentenceId: Int? = nil)
        -> Components.Schemas.SentenceCard {
        Components.Schemas.SentenceCard(
            id: id, text: "A sentence with municipal in it.",
            blank_start: 14, blank_end: 23, surface: "municipal",
            first_letter: "m", source: source,
            sentence_id: sentenceId, article_id: articleId)
    }

    @Test("生成的句子永远是考题，从不当提示")
    func generatedSentencesAreAlwaysQuestions() {
        // 连它出自的文章都读完了也不当提示——它本来就不出自任何文章，
        // 而它是为了教这个词才写的:当提示等于把答案当提示给出去。
        let split = SentencePool.split(
            [Self.card(1, source: "generated")],
            finished: [480], seen: [11])
        #expect(split.questions.count == 1)
        #expect(split.hints.isEmpty)
    }

    @Test("出自读完过的文章 → 提示")
    func aSentenceFromAFinishedArticleIsAHint() {
        let split = SentencePool.split(
            [Self.card(1, source: "corpus", articleId: 480, sentenceId: 11),
             Self.card(2, source: "corpus", articleId: 999, sentenceId: 77)],
            finished: [480], seen: [])
        #expect(split.hints.map(\.id) == [1])
        #expect(split.questions.map(\.id) == [2])
    }

    /// **这一条不能省，而它是最容易漏的那条。**
    ///
    /// 判据是「你见过这一句吗」，不是「你读完那篇了吗」——两者在最普通的情形里
    /// 就分家:你是在读的时候标记的，所以标记所在那一句肯定见过，
    /// **而文章可能几小时后才读完，也可能永远不读完**。
    /// 少了这一条那一句算「没见过」、可以被抽成考题——
    /// 于是你被五分钟前刚读过的那一句考了，看着像道容易题，
    /// 落下来是一个虚高的评级，**而没有任何东西会报告它**。
    @Test("那一句你确定见过 → 提示，哪怕那篇还没读完")
    func aSeenSentenceIsAHintEvenIfTheArticleIsUnfinished() {
        let split = SentencePool.split(
            [Self.card(1, source: "corpus", articleId: 999, sentenceId: 11)],
            finished: [],          // 那篇没读完
            seen: [11])            // 但那一句是标记发生的地方
        #expect(split.hints.map(\.id) == [1], "见过就是见过，不能拿它当考题")
        #expect(split.questions.isEmpty)
    }

    @Test("没有 id 的句子不会被误判成见过")
    func missingIdsDoNotCountAsSeen() {
        let split = SentencePool.split(
            [Self.card(1, source: "corpus")],   // 既没有文章也没有句子 id
            finished: [480], seen: [11])
        #expect(split.questions.map(\.id) == [1], "判不出来就当没见过——那是保守的方向")
    }

    @Test("投影记得读完过哪些文章、见过哪些句子")
    func theProjectionTracksWhatWasRead() {
        let met: JSONValue = .array([
            .object(["item_key": .string("municipal"), "sense_id": .int(0), "n": .int(1)]),
        ])
        let projection = ProjectionTests.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown"),
                       "article_id": .int(999), "sentence_id": .int(11)],
             "2026-01-05T08:00:00+00:00"),
            (.read, ["article_id": .int(480), "met": met], "2026-01-05T09:00:00+00:00"),
        ])
        #expect(projection.finishedArticles == [480])
        #expect(projection.seenSentences == [11], "标记所在那一句肯定见过")
    }

    @Test("撤销标记之后，那一句仍然算见过")
    func unmarkingDoesNotForgetASeenSentence() {
        let projection = ProjectionTests.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown"),
                       "article_id": .int(999), "sentence_id": .int(11)],
             "2026-01-05T08:00:00+00:00"),
            (.unmarked, ["item_key": .string("municipal")], "2026-01-05T08:30:00+00:00"),
        ])
        #expect(projection.seenSentences == [11],
                "日志只增不减——而那正是服务端留 introduced_sentence_id 想要的性质")
    }
}
