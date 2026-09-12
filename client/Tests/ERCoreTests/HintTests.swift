import Foundation
import Testing
import ERContract
@testable import ERCore

/// The hint ladder, against a real review payload.
///
/// What makes these worth writing rather than eyeballing: the two directions
/// ask different questions, so their deepest hint has to be a different thing.
/// The first web reader showed the English word in both — answering a question
/// nobody asked, in one of the two — and its own comment says that is the kind
/// of mistake only a real run finds. A test is cheaper than a real run.
struct HintTests {
    static func day() throws -> Components.Schemas.ReviewDayResponse {
        let url = Bundle.module.url(forResource: "Fixtures/reviews", withExtension: "json")!
        return try JSONDecoder().decode(
            Components.Schemas.ReviewDayResponse.self, from: Data(contentsOf: url))
    }

    static func itemWithSentences() throws -> Components.Schemas.ReviewItem? {
        try day().items.first { !$0.questions.isEmpty || !$0.hints.isEmpty }
    }

    @Test func bothDirectionsGetThreeLevels() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first
        for direction in [ReviewDirection.wordToSense, .senseToWord] {
            let ladder = HintLadder.hints(for: item, asked: asked, direction: direction)
            #expect(ladder.count == HintLadder.maxReveal,
                    "开了几级就是成绩——级数必须跟服务端的 MAX_REVEAL 一致")
            #expect(ladder.map(\.level) == [1, 2, 3])
        }
    }

    /// The one that matters: the last resort has to answer the question that
    /// was actually asked.
    @Test func theDeepestHintAnswersTheQuestionAsked() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first

        // 看词想义 asks what the word means, so the deepest hint is the meaning
        // or a way back to the passage — never the word itself, which is
        // already on screen.
        let toSense = HintLadder.hints(for: item, asked: asked, direction: .wordToSense)
        #expect(!toSense.contains { $0.body == item.item_key },
                "给的词就在题面上，把它当答案等于什么都没给")
        #expect(toSense[1].body.contains(HintLadder.chineseGloss(item))
                || HintLadder.chineseGloss(item).isEmpty)

        // 看义想词 asks which word fills the blank, so the deepest hint is the
        // word.
        let toWord = HintLadder.hints(for: item, asked: asked, direction: .senseToWord)
        #expect(toWord[2].label == "答案")
        if let surface = asked?.surface {
            #expect(toWord[2].body == surface)
        }
        #expect(toWord[0].label == "首字母")
    }

    @Test func theFirstHintIsNotTheSentenceAlreadyOnScreen() throws {
        let day = try Self.day()
        // A word met in more than one sentence: only then can the hint be a
        // different one, which is the case worth checking.
        guard let item = day.items.first(where: {
            ($0.questions.count + $0.hints.count) >= 2
        }) else { return }

        let asked = item.questions.first ?? item.hints.first
        let ladder = HintLadder.hints(for: item, asked: asked, direction: .wordToSense)
        if let askedText = asked?.text, item.hints.contains(where: { $0.id != asked?.id }) {
            #expect(ladder[0].body != askedText,
                    "第一级提示是「当初读到它的那句」——跟题面同一句的话，提示等于没提示")
        }
    }

    @Test func blankingUsesTheSpanTheServerGave() {
        let sentence = Components.Schemas.SentenceCard(
            id: 1,
            text: "The technician fixed the technician's own radio.",
            blank_start: 4, blank_end: 14,
            surface: "technician",
            first_letter: "t"
        )
        let blanked = HintLadder.blanked(sentence)
        #expect(blanked == "The ＿＿＿ fixed the technician's own radio.",
                "按服务端给的区间挖，不是按字符串搜——同一个词出现两次时，搜会挖错地方")
    }

    @Test func blankingSurvivesAnOutOfRangeSpan() {
        let sentence = Components.Schemas.SentenceCard(
            id: 1, text: "短", blank_start: 100, blank_end: 200,
            surface: "x", first_letter: "x")
        // 坏数据不该让一天的复习崩在这里。
        #expect(HintLadder.blanked(sentence) == "短＿＿＿")
    }

    @Test func theQuestionMatchesTheDirection() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first

        let toSense = HintLadder.prompt(for: item, asked: asked, direction: .wordToSense)
        #expect(toSense == (asked?.text ?? item.item_key),
                "看词想义：把它出现的那句原样给出来，词就在里面")

        let toWord = HintLadder.prompt(for: item, asked: asked, direction: .senseToWord)
        if let asked {
            #expect(toWord.contains("＿＿＿"), "看义想词：词要挖掉，不然就是给答案")
            #expect(!toWord.contains(asked.surface) || asked.text.components(
                separatedBy: asked.surface).count > 2)
        }
    }
}
