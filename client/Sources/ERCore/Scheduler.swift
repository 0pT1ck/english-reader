import Foundation
import FSRS

/// 排期 scheduling / 记忆状态 memory state / 评级 grade
///
/// **P9 把这条规则搬到了设备上。** 在那之前它只在服务端（`backend/modules/
/// review/scheduler.py`），而客户端从不计算间隔——`export_review_vectors.py`
/// 的说明里写得很清楚：「Intervals, ratings and due dates are the scheduler's,
/// the client never computes them」。P9 的那条线（服务端只管生文）让这句话不再
///成立，于是它来了这里。
///
/// **搬下来之后世上只剩一份实现。** 服务端不再需要 FSRS——查过了，夜间备稿从学习
/// 记录里只要一个「正在学的词」的集合，不读排期（`generation/daily.py` 的
/// `words_in_progress`）。所以这不是「同一条规则写两遍」，架构铁律 1 当年担心的
/// 那件事在这里不发生。`scheduler-vectors.json` 是一次性的交接凭证：它证明这一份
/// 和被替换掉的那一份算得一样，之后服务端那份就删掉。
///
/// **两个上限是两条规则，别再把它们并成一个条件。** 2026-09-17 服务端刚拆开：
/// 开过提示是「降一档」，当天刚标是「封顶为 Hard」。它们在 `Good` 上结果相同，
/// 所以混着写了很久也没露出来；在 `Easy` 上一个给 `Good`、一个给 `Hard`。
public enum ReviewScheduler {

    /// 四档评级。**按它们的意思命名，不按 FSRS 命名**——同服务端那句
    /// 「Named for what they mean, not for FSRS: swapping schedulers must not
    /// need a migration」。换掉底下那个包不该改这个类型。
    ///
    /// 原始值和服务端、和 FSRS 都对得上（1–4），所以过线时不用转换表。
    public enum Grade: Int, Sendable, Codable, CaseIterable, Comparable {
        case again = 1
        case hard = 2
        case good = 3
        case easy = 4

        public static func < (lhs: Grade, rhs: Grade) -> Bool {
            lhs.rawValue < rhs.rawValue
        }
    }

    /// 看了提示之后不会掉到这一档以下。`again` 说的是「你忘了」，那是关于回忆的
    /// 判断——用了帮助不等于忘了。
    static let hintFloor: Grade = .hard

    /// 当天刚标的词，那一次最多拿到这一档（P7 §3 的原话是「封顶为 Hard」）。
    static let cappedCeiling: Grade = .hard

    /// 失误次数决定的那一档。**数的是失误，不是问了几次**：一轮天生是两问，
    /// 按问数算会让一轮无误得 2、磕一下得 4，于是磕一下被判成最差的 `again`。
    static func fromMisses(_ misses: Int) -> Grade {
        switch misses {
        case 0: .good
        case 1: .hard
        default: .again
        }
    }

    /// 降一档，但不低于 ``hintFloor``。
    static func oneLower(_ grade: Grade) -> Grade {
        guard grade > hintFloor, let lower = Grade(rawValue: grade.rawValue - 1) else {
            return grade
        }
        return lower
    }

    /// 这一天问下来该给哪一档。
    ///
    /// - `easy` **是学习者自己说的，从不由系统推断**。FSRS 把「我会了」（good）和
    ///   「这太简单」（easy）分开，而分开它们要的是「多快、多确定」这类度量，这个
    ///   项目不收集。把每次干净通过都判成 easy，正是当年间隔跑成 8→66→180 的原因。
    ///   它藏在右上角的二级菜单里，点它的人知道自己在干什么。
    /// - `revealed` **降一档**：提示不要钱的话，所有人都先点提示。
    /// - `capped` **封顶**：当天刚标的词，你几分钟前才读到它，答对证明不了什么。
    ///
    /// 两者都只会往下压：提示有下限，封顶绝不抬高评级。
    public static func grade(misses: Int, easy: Bool = false,
                             revealed: Int = 0, capped: Bool = false) -> Grade {
        let misses = max(0, misses)
        var grade: Grade = (easy && misses == 0) ? .easy : fromMisses(misses)
        if revealed > 0 { grade = oneLower(grade) }
        if capped { grade = min(grade, cappedCeiling) }
        return grade
    }

    /// 调度器的配置。
    ///
    /// **`parameters` 必须显式给，不许靠任何一边的「默认」。** 2026-09-17 实测：
    /// `py-fsrs` 6.3.2 默认是 FSRS-6（21 个数），而 `swift-fsrs` 把 FSRS-6 那组
    /// 放在另一个常量里、`w` 仍然默认 FSRS-5 的 19 个。两边各取自己的默认就会跑
    /// 不同的算法、给不同的间隔，**而且两边都在按自己的文档正常工作，没有东西会
    /// 报错**。所以向量文件里带着服务端真正用的那组数，这里照着喂。
    public struct Settings: Sendable, Codable, Equatable {
        public var requestRetention: Double
        public var maximumInterval: Int
        public var parameters: [Double]
        /// 抖动。开着是正常使用的样子（它让同一天学的一批词不会永远同一天回来）；
        /// 向量和验收一律关掉，否则两种语言的随机数种子不同就成了「分歧」。
        public var enableFuzz: Bool

        public init(requestRetention: Double, maximumInterval: Int,
                    parameters: [Double], enableFuzz: Bool) {
            self.requestRetention = requestRetention
            self.maximumInterval = maximumInterval
            self.parameters = parameters
            self.enableFuzz = enableFuzz
        }
    }

    /// 存下来的那几列。**按它们的意思命名**，理由同 ``Grade``。
    ///
    /// 整个结构为 nil ＝ 这个词从没被复习过（P3 之前标的词全是这样），而那正是
    /// 正确答案，不是缺数据。
    public struct MemoryState: Sendable, Codable, Equatable {
        public var stability: Double
        public var difficulty: Double
        public var dueAt: Date?
        public var lastReviewAt: Date?
        public var reps: Int
        public var lapses: Int
        /// FSRS 自己的状态机档位。关掉了同日重复之后它只会是 learning 或 review，
        /// `relearning` 永远不出现。
        public var fsrsState: Int

        public init(stability: Double, difficulty: Double, dueAt: Date? = nil,
                    lastReviewAt: Date? = nil, reps: Int = 0, lapses: Int = 0,
                    fsrsState: Int = 2) {
            self.stability = stability
            self.difficulty = difficulty
            self.dueAt = dueAt
            self.lastReviewAt = lastReviewAt
            self.reps = reps
            self.lapses = lapses
            self.fsrsState = fsrsState
        }
    }

    /// 一次复习对一个词做了什么。
    public struct Outcome: Sendable, Equatable {
        public var state: MemoryState
        public var dueAt: Date
        public var intervalDays: Double
        public var grade: Grade
    }

    /// 一个词、一天问下来的结果，落成下一次的到期时间。
    ///
    /// `now` 是传进来的而不是读时钟的——这样排期能被测，不用等三天。同服务端。
    ///
    /// **抛错只有一种可能**：把 FSRS 的 `manual` 档传下去。我们永远不传，所以这个
    /// 抛错到不了；但吞掉它就等于把「真的传错了」变成静默的错值。
    public static func review(state: MemoryState?, misses: Int, now: Date,
                              easy: Bool = false, revealed: Int = 0,
                              capped: Bool = false,
                              settings: Settings) throws -> Outcome {
        let grade = grade(misses: misses, easy: easy, revealed: revealed, capped: capped)

        // 每次现建，不缓存：便宜，而缓存过的那个会在配置改了之后继续用旧参数。同服务端。
        let engine = FSRS(parameters: FSRSParameters(
            requestRetention: settings.requestRetention,
            maximumInterval: Double(settings.maximumInterval),
            w: settings.parameters,
            enableFuzz: settings.enableFuzz,
            // **这一项对应服务端的 `learning_steps=()` 与 `relearning_steps=()`。**
            // 关掉之后走 `LongTermScheduler`，也就是「一天是一次复习」——同日重复由
            // 我们自己的加权池子负责（`ReviewItemState`），两套都开会把每天数两遍。
            enableShortTerm: false
        ))

        let result = try engine.next(card: card(from: state, now: now), now: now,
                                     grade: Rating(rawValue: grade.rawValue) ?? .good)
        let updated = result.card

        // reps 与 lapses 按**我们的**算法记，不读 FSRS 自己那两个计数器：
        // lapses 只在 `again` 时加一，而那是这个项目对「失手」的定义。
        let reps = (state?.reps ?? 0) + 1
        let lapses = (state?.lapses ?? 0) + (grade == .again ? 1 : 0)

        return Outcome(
            state: MemoryState(
                stability: updated.stability,
                difficulty: updated.difficulty,
                dueAt: updated.due,
                lastReviewAt: updated.lastReview,
                reps: reps,
                lapses: lapses,
                fsrsState: updated.state.rawValue
            ),
            dueAt: updated.due,
            intervalDays: updated.due.timeIntervalSince(now) / 86_400,
            grade: grade
        )
    }

    /// 把存下来的几列还原成 FSRS 的卡。
    ///
    /// **没有记忆状态时交给 FSRS 自己的「新卡」**，而不是照服务端那份的内部表示去拼。
    /// 两个包对「没复习过」的记法不一样：`py-fsrs` 是 `Learning` ＋ stability 为空，
    /// `swift-fsrs` 是 `new` ＋ 0。各用各的说法，让向量去核**结果**是否一致——
    /// 照着另一边的内部表示拼，才是真会算出不同数的做法。
    static func card(from state: MemoryState?, now: Date) -> Card {
        guard let state else { return Card(due: now) }
        return Card(
            due: state.dueAt ?? now,
            stability: state.stability,
            difficulty: state.difficulty,
            reps: state.reps,
            lapses: state.lapses,
            state: CardState(rawValue: state.fsrsState) ?? .review,
            lastReview: state.lastReviewAt
        )
    }
}
