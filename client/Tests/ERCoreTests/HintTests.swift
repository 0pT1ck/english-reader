import Foundation
import Testing
import ERContract
@testable import ERCore

/// The hint, and what each direction asks, against a real review payload.
///
/// What makes these worth writing rather than eyeballing: the two directions
/// ask different questions, so a hint that is harmless in one is the answer in
/// the other. The first web reader showed the English word in both — answering
/// a question nobody asked, in one of the two — and its own comment says that
/// is the kind of mistake only a real run finds.
///
/// **P7 collapsed the ladder to one level** and, in doing so, turned up the
/// reason these tests were not enough: the old level 2 of 看义想词 printed the
/// original sentence unblanked, and the word is in it. Nothing here asked "does
/// a hint ever contain the answer", so nothing went red for three phases. That
/// question is now `hintNeverContainsTheAnswerInSenseToWord`.
struct HintTests {
    static func day() throws -> Components.Schemas.ReviewDayResponse {
        let url = Bundle.module.url(forResource: "Fixtures/reviews", withExtension: "json")!
        return try JSONDecoder().decode(
            Components.Schemas.ReviewDayResponse.self, from: Data(contentsOf: url))
    }

    static func itemWithSentences() throws -> Components.Schemas.ReviewItem? {
        try day().items.first { !$0.questions.isEmpty || !$0.hints.isEmpty }
    }

    @Test func oneLevel() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first
        for direction in [ReviewDirection.wordToSense, .senseToWord] {
            let ladder = HintLadder.hints(for: item, asked: asked, direction: direction)
            #expect(ladder.count <= HintLadder.maxReveal,
                    "开了几级就是成绩——级数不能超过服务端的 MAX_REVEAL")
            #expect(ladder.map(\.level) == Array(1...ladder.count).map { $0 }
                    || ladder.isEmpty)
        }
    }

    /// **The one that matters.** 看义想词 asks which English word fits; the
    /// original sentence contains that word, so handing it over raw *is* the
    /// answer. This is a property of the direction, not of one rung — written
    /// that way so adding a level back cannot reintroduce it.
    @Test func hintNeverContainsTheAnswerInSenseToWord() throws {
        let day = try Self.day()
        for item in day.items {
            let asked = item.questions.first ?? item.hints.first
            let ladder = HintLadder.hints(for: item, asked: asked, direction: .senseToWord)
            guard let surface = asked?.surface, !surface.isEmpty else { continue }
            for hint in ladder {
                #expect(!hint.body.lowercased().contains(surface.lowercased()),
                        "看义想词的提示里出现了答案本身：\(hint.body)")
                #expect(!hint.body.lowercased().contains(item.item_key.lowercased()),
                        "看义想词的提示里出现了那个词：\(hint.body)")
            }
        }
    }

    /// 看词想义 has the word on screen already, so its hint is context — never
    /// the meaning, which is what the reveal screen is for.
    @Test func wordToSenseHintIsAnotherSentence() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first
        let ladder = HintLadder.hints(for: item, asked: asked, direction: .wordToSense)
        guard let hint = ladder.first else { return }
        #expect(hint.body != item.item_key, "给的词就在题面上，把它当答案等于什么都没给")
        #expect(!hint.body.isEmpty)
    }

    @Test func senseToWordHintCarriesTheFirstLetter() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first ?? item.hints.first
        let ladder = HintLadder.hints(for: item, asked: asked, direction: .senseToWord)
        guard let hint = ladder.first else { return }
        let expected = asked?.first_letter ?? String(item.item_key.prefix(1))
        #expect(hint.body.contains(expected + "＿＿＿"),
                "中文题面对不出近义词——首字母是唯一消歧的那一样")
    }

    @Test func theHintIsNotTheSentenceAlreadyOnScreen() throws {
        let day = try Self.day()
        guard let item = day.items.first(where: {
            ($0.questions.count + $0.hints.count) >= 2
        }) else { return }

        let asked = item.questions.first ?? item.hints.first
        let ladder = HintLadder.hints(for: item, asked: asked, direction: .wordToSense)
        if let askedText = asked?.text, item.hints.contains(where: { $0.id != asked?.id }),
           let hint = ladder.first {
            #expect(hint.body != askedText,
                    "提示是「当初读到它的那句」——跟题面同一句的话，提示等于没提示")
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
            if let zh = asked.text_zh, !zh.isEmpty {
                #expect(toWord == zh, "看义想词：题面是整句中文")
            } else {
                #expect(toWord.contains("＿＿＿"),
                        "还没翻译的句子退回挖空的英文——空题面比什么都糟")
            }
        }
    }

    /// The Chinese question asks with a sentence; the word being tested is
    /// highlighted in it. **A wrong span is worse than none**: it would split
    /// 估计 down the middle and nothing would report it.
    @Test func theChineseHighlightStaysInsideTheSentence() throws {
        let day = try Self.day()
        for item in day.items {
            for card in item.questions + item.hints {
                guard let range = HintLadder.promptHighlight(card, direction: .senseToWord),
                      let zh = card.text_zh else { continue }
                #expect(range.lowerBound >= 0)
                #expect(range.upperBound <= zh.count)
                #expect(range.lowerBound < range.upperBound)
            }
        }
    }

    @Test func noHighlightInTheOtherDirection() throws {
        guard let item = try Self.itemWithSentences() else { return }
        let asked = item.questions.first
        #expect(HintLadder.promptHighlight(asked, direction: .wordToSense) == nil,
                "看词想义的题面是英文，中文高亮在那儿没有意义")
    }
}

extension HintTests {
    /// 方向 2 的题面**永远不能是空的**。
    ///
    /// 2026-09-16 真机上出过：那个词的句子池一条都没有（刚标记的词，例句还没
    /// 生成出来），而它的义项又恰好没有中文——三层兜底全落空，屏幕上是一片
    /// 空白，人不知道被问的是什么。**贫乏的问题还能答，空白的不能。**
    @Test func senseToWordNeverAsksNothing() {
        // 没有句子、没有义项中文、只有词典释义。
        let bare = Components.Schemas.ReviewItem(
            queue_id: 1, item_type: "word", item_key: "after", sense_id: 0,
            bucket: "today", direction: 1, asks: 0, misses: 0, weight: 1, done: false,
            word: Components.Schemas.WordCard(headword: "after", translation: "在…之后"),
            questions: [], hints: [])
        let prompt = HintLadder.prompt(for: bare, asked: nil, direction: .senseToWord)
        #expect(!prompt.isEmpty)
        #expect(prompt.contains("在…之后"))

        // 连词典释义都没有时，说一句话，仍然不是空白。
        let barest = Components.Schemas.ReviewItem(
            queue_id: 2, item_type: "word", item_key: "after", sense_id: 0,
            bucket: "today", direction: 1, asks: 0, misses: 0, weight: 1, done: false,
            questions: [], hints: [])
        #expect(!HintLadder.prompt(for: barest, asked: nil, direction: .senseToWord).isEmpty)
    }
}
