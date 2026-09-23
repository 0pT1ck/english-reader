import Foundation
import Testing
import ERContract
@testable import ERCore

/// Replay the server's own transitions through the client's mirror.
///
/// The expectations in `review-vectors.json` were produced by calling
/// `session.answer` on a real queue row — so when this fails, the client is
/// wrong. That direction matters: hand-written expectations have no authority,
/// and two sides can agree on the same mistake.
struct ReviewStateTests {
    struct Vectors: Decodable {
        struct Constants: Decodable {
            let word_to_sense: Int
            let sense_to_word: Int
            let max_reveal: Int
            let weight_decay: Double
            let weight_floor: Double
        }
        struct Snapshot: Decodable, Equatable {
            let direction: Int
            let asks: Int
            let misses: Int
            let weight: Double
            let done: Bool
        }
        struct Answer: Decodable {
            let passed: Bool
            let revealed: Int
            let easy: Bool
        }
        struct Case: Decodable {
            let name: String
            let initial: Snapshot
            let answers: [Answer]
            let expected: [Snapshot]
        }
        let constants: Constants
        let cases: [Case]
    }

    static func load() throws -> Vectors {
        let url = Bundle.module.url(forResource: "Fixtures/review-vectors", withExtension: "json")
        #expect(url != nil, "找不到 review-vectors.json——先跑 scripts/export_review_vectors.py")
        return try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: url!))
    }

    static func snapshot(_ state: ReviewItemState) -> Vectors.Snapshot {
        .init(
            direction: state.direction.rawValue,
            asks: state.asks,
            misses: state.misses,
            // Rounded the same way the exporter rounds: the decay is repeated
            // multiplication, and the last bits of a double are not something
            // two languages need to agree about.
            weight: (state.weight * 1_000_000).rounded() / 1_000_000,
            done: state.done
        )
    }

    @Test func constantsMatchTheServer() throws {
        let vectors = try Self.load()
        #expect(vectors.constants.word_to_sense == ReviewDirection.wordToSense.rawValue)
        #expect(vectors.constants.sense_to_word == ReviewDirection.senseToWord.rawValue)
        #expect(vectors.constants.weight_floor == ReviewItemState.weightFloor)
    }

    @Test func everyExportedCaseReplays() throws {
        let vectors = try Self.load()
        #expect(!vectors.cases.isEmpty, "没有向量就等于没有检查")

        for testCase in vectors.cases {
            var state = ReviewItemState(
                direction: ReviewDirection(rawValue: testCase.initial.direction)!,
                asks: testCase.initial.asks,
                misses: testCase.initial.misses,
                weight: testCase.initial.weight,
                done: testCase.initial.done
            )
            for (index, answer) in testCase.answers.enumerated() {
                #expect(state.acceptsAnswer, "\(testCase.name)：第 \(index + 1) 次作答时条目已经过了")
                state = state.applying(
                    ReviewAnswer(passed: answer.passed, revealed: answer.revealed, easy: answer.easy),
                    weightDecay: vectors.constants.weight_decay
                )
                #expect(
                    Self.snapshot(state) == testCase.expected[index],
                    """
                    \(testCase.name)
                      第 \(index + 1) 次作答（\(answer.passed ? "对" : "错")）之后对不上
                      客户端：\(Self.snapshot(state))
                      服务端：\(testCase.expected[index])
                    """
                )
            }
        }
    }

    /// 阳性对照 (坑 §4.3): a replay that passes proves nothing unless a wrong
    /// mirror would fail it. This deliberately breaks the rule that the second
    /// direction re-locks on a miss — 决定 7, the transition most likely to be
    /// written wrong — and checks the vectors catch it.
    @Test func aBrokenMirrorWouldBeCaught() throws {
        let vectors = try Self.load()
        let relock = vectors.cases.first { $0.name.contains("第二向答错") }
        #expect(relock != nil, "那条向量不见了，这个对照就没有意义了")
        guard let relock else { return }

        var state = ReviewItemState()
        var wrong = ReviewItemState()
        var diverged = false
        for answer in relock.answers {
            let reviewAnswer = ReviewAnswer(
                passed: answer.passed, revealed: answer.revealed, easy: answer.easy)
            state = state.applying(reviewAnswer, weightDecay: vectors.constants.weight_decay)
            wrong = Self.brokenApply(wrong, reviewAnswer,
                                     weightDecay: vectors.constants.weight_decay)
            if Self.snapshot(state) != Self.snapshot(wrong) { diverged = true }
        }
        #expect(diverged, "写坏的镜像居然也能通过——那这些向量什么都没在验")
    }

    /// The mirror with 决定 7 left out: a miss on the second direction goes back
    /// to the first but forgets to re-lock, so the item stays unlocked.
    static func brokenApply(
        _ state: ReviewItemState, _ answer: ReviewAnswer, weightDecay: Double
    ) -> ReviewItemState {
        var next = state
        next.asks += 1
        if !answer.passed { next.misses += 1 }
        if answer.passed {
            if state.direction == .wordToSense { next.direction = .senseToWord }
            else { next.done = true }
        } else {
            next.weight = max(ReviewItemState.weightFloor, state.weight * weightDecay)
        }
        return next
    }

    @Test func theDrawIsWeightedAndNeverExcludes() {
        let draw = ReviewDraw()
        let items: [(name: String, weight: Double, open: Bool)] = [
            ("卡住的", 0.0001, true),
            ("正常的", 1.0, true),
            ("做完的", 1.0, false),
        ]
        func pick(_ r: Double) -> String? {
            draw.pick(from: items, weight: \.weight, isOpen: \.open, random: r)?.name
        }
        #expect(pick(0.0) == "卡住的", "权重再小也还在袋子里——决不排除")
        #expect(pick(0.99) == "正常的")
        #expect(pick(0.5) == "正常的", "权重大的占掉绝大部分")

        let none = draw.pick(from: items, weight: \.weight, isOpen: { _ in false }, random: 0.5)
        #expect(none == nil, "都做完了就该是 nil，而不是硬挑一个")
    }

    /// P11 决定 ⑤b：**词组只做单向**。
    ///
    /// 「看词组想意思」问得了；反过来给「导致」让你想英文是道坏题——
    /// `lead to`／`result in`／`contribute to` 全对，而卡片只认一个答案。
    /// 所以答对第一向就走完这一轮，**没有第二向可解锁**。
    @Test("词组答对第一向就算走完，不解锁第二向")
    func phrasesAreAskedOneWayOnly() {
        let pass = ReviewAnswer(passed: true, revealed: 0, easy: false)

        let word = ReviewItemState().applying(pass, weightDecay: 0.5)
        #expect(word.direction == .senseToWord, "单词照旧解锁看义想词")
        #expect(!word.done)

        let phrase = ReviewItemState().applying(pass, weightDecay: 0.5,
                                                singleDirection: true)
        #expect(phrase.done, "词组答对就走完了")
        #expect(phrase.direction == .wordToSense, "方向不动——没有第二向")
        #expect(phrase.asks == 1, "问过几次照旧累加，那个数就是成绩")
    }
}

/// 词组不进拼写（P11 决定 ⑤b：`take sth into account` 中间有变量，拼什么）。
///
/// 这条 P11 定了、却没落到 Core 上：`spellingWords()` 不分条目类型，
/// 2026-09-23（P12）读代码读出来的。
struct SpellingSelectionTests {
    static func entry(_ type: String, _ key: String, sense: Int) -> ReviewDay.Entry {
        ReviewDay.Entry(
            key: Projection.Key(itemType: type, key: key, senseId: sense),
            bucket: .due, capped: false, state: ReviewItemState(),
            item: Components.Schemas.ReviewItem(
                queue_id: 0, item_type: type, item_key: key, sense_id: sense,
                bucket: "due", direction: 1, asks: 0, misses: 0, weight: 1, done: false,
                questions: [], hints: []))
    }

    @Test func phrasesAreNeverSpelled() {
        var day = ReviewDay()
        day.entries = [Self.entry("phrase", "account for", sense: 42809),
                       Self.entry("word", "account", sense: 14896)]
        // 阳性一半：单词照旧在——一个「什么都不拼」的实现过不了这一条。
        #expect(day.spellingWords().map(\.key) == ["account"])
    }
}
