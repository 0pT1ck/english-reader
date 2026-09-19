import Foundation

/// 排期 scheduling / 记忆状态 memory state / 评级 grade / 可提取性 retrievability
///
/// **P9 把这条规则搬到了设备上。** 在那之前它只在服务端，客户端从不计算间隔——
/// `export_review_vectors.py` 的说明写得很清楚：「Intervals, ratings and due dates
/// are the scheduler's, the client never computes them」。P9 那条线（服务端只管生文）
/// 让这句话不再成立，于是它来了这里。
///
/// **搬下来之后世上只剩一份实现。** 服务端不再需要 FSRS——夜间备稿从学习记录里只要
/// 一个「正在学的词」的集合，不读排期（`generation/daily.py` 的 `words_in_progress`）。
/// 所以这不是「同一条规则写两遍」，架构铁律 1 当年担心的那件事在这里不发生。
///
/// **为什么是自己实现，而不是引一个包**（2026-09-17，两个都试过了）：
/// 官方 `open-spaced-repetition/swift-fsrs` 算法版本对（FSRS-6）、MIT、零包依赖，
/// 但它 `import JavaScriptCore`，**Linux 上没有这个模块**，而 Core 必须在 Linux 上
/// 编得过（`Package.swift` 开头那段，CI 的 L0 就在那儿跑）。`4rays/SwiftFSRS` 编得过
/// 但只实现 FSRS-5。所以这里照 `py-fsrs`（MIT，服务端用的就是它）移植 FSRS-6 的
/// 长期路径。**移植敢做的前提是向量网已经建好了**：`scheduler-vectors.json` 是从
/// 服务端真实代码导出的，这一份错一位它就红。
///
/// **只实现「关掉同日重复」那条路径。** 服务端配的是 `learning_steps=()` 与
/// `relearning_steps=()`，理由是同日重复由我们自己的加权池子负责，两套都开会把每天
/// 数两遍。两个列表都空之后 `py-fsrs` 的状态机塌成一条直线：新卡取初始值，老卡按
/// 间隔算，评级再差也只是重新排一次期——`Relearning` 永远不出现。
public enum ReviewScheduler {

    // MARK: 评级

    /// 四档评级。**按它们的意思命名，不按 FSRS 命名**——同服务端那句
    /// 「Named for what they mean, not for FSRS: swapping schedulers must not
    /// need a migration」。原始值和服务端、和 FSRS 都对得上（1–4）。
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
    /// - `easy` **是学习者自己说的，从不由系统推断**。FSRS 把「我会了」和「这太简单」
    ///   分开，而分开它们要的是「多快、多确定」这类度量，这个项目不收集。把每次干净
    ///   通过都判成 easy，正是当年间隔跑成 8→66→180 的原因。它藏在右上角的二级菜单里，
    ///   点它的人知道自己在干什么。
    /// - `revealed` **降一档**：提示不要钱的话，所有人都先点提示。
    /// - `capped` **封顶**：当天刚标的词，你几分钟前才读到它，答对证明不了什么。
    ///
    /// **这两条是两条规则，不是一条。** 2026-09-17 服务端刚拆开：它们在 `good` 上结果
    /// 相同（都落到 `hard`），所以并成一个条件写了很久也没露出来；在 `easy` 上一个给
    /// `good`、一个给 `hard`。别再并回去。
    public static func grade(misses: Int, easy: Bool = false,
                             revealed: Int = 0, capped: Bool = false) -> Grade {
        let misses = max(0, misses)
        var grade: Grade = (easy && misses == 0) ? .easy : fromMisses(misses)
        if revealed > 0 { grade = oneLower(grade) }
        if capped { grade = min(grade, cappedCeiling) }
        return grade
    }

    // MARK: 配置与状态

    /// 调度器的配置。
    ///
    /// **`parameters` 必须显式给。** 向量文件里带着服务端真正用的那 21 个数，
    /// 这里照着喂——两边各取「自己的默认」是 2026-09-17 差点踩进去的坑：
    /// `py-fsrs` 6.3.2 默认 FSRS-6，而那个官方 Swift 包默认仍是 FSRS-5 的 19 个，
    /// **两边都在按自己的文档正常工作，没有东西会报错**。
    public struct Settings: Sendable, Codable, Equatable {
        public var requestRetention: Double
        public var maximumInterval: Int
        public var parameters: [Double]
        /// 抖动。开着是正常使用的样子（它让同一天学的一批词不会永远同一天回来）；
        /// 向量和验收一律关掉，否则两种语言的随机数种子不同就成了「分歧」。
        ///
        /// **目前这一份不实现抖动**，置 true 会抛错而不是悄悄不抖——见 ``review``。
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
        /// FSRS 自己的状态机档位。关掉了同日重复之后它只会是 2（review）。
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

    public enum SchedulerError: Error, Equatable {
        /// 参数个数不对。**宁可抛错也不猜**：19 个是 FSRS-5，21 个是 FSRS-6，
        /// 拿 19 个去跑 6 的公式不会报错，只会给出别的间隔。
        case wrongParameterCount(Int)
        /// 抖动还没实现。置 true 就抛，而不是悄悄不抖——静默地不抖会让「为什么
        /// 一批词总在同一天回来」变成查不出来的问题。
        case fuzzNotImplemented
    }

    // MARK: 常量（照 py-fsrs）

    static let stabilityMin = 0.001
    static let difficultyMin = 1.0
    static let difficultyMax = 10.0
    /// FSRS-6 的参数个数。第 21 个（下标 20）是 decay。
    static let parameterCount = 21

    // MARK: 排期

    /// 一个词、一天问下来的结果，落成下一次的到期时间。
    ///
    /// `now` 是传进来的而不是读时钟的——这样排期能被测，不用等三天。同服务端。
    public static func review(state: MemoryState?, misses: Int, now: Date,
                              easy: Bool = false, revealed: Int = 0,
                              capped: Bool = false,
                              settings: Settings) throws -> Outcome {
        guard settings.parameters.count == parameterCount else {
            throw SchedulerError.wrongParameterCount(settings.parameters.count)
        }
        guard !settings.enableFuzz else { throw SchedulerError.fuzzNotImplemented }

        let w = settings.parameters
        let grade = grade(misses: misses, easy: easy, revealed: revealed, capped: capped)
        let decay = -w[20]
        let factor = pow(0.9, 1 / decay) - 1

        let stability: Double
        let difficulty: Double

        if let state {
            // **`.days` 是整天数，向下取整。** py-fsrs 用的是 `timedelta.days`，
            // 而它对正值就是 floor；照着取整才对得上，`/86400` 直接用会差一点。
            let elapsed = state.lastReviewAt.map { wholeDays(from: $0, to: now) }

            if let elapsed, elapsed < 1 {
                // 同一天里又答了一次。**我们的规矩是「一天是一次复习」所以走不到这儿**，
                // 但公式照 py-fsrs 实现着——不实现就是留一个静默给错值的口子。
                stability = shortTermStability(stability: state.stability,
                                               grade: grade, w: w)
            } else {
                let retrievability = elapsed.map { e in
                    pow(1 + factor * Double(e) / state.stability, decay)
                } ?? 0
                stability = nextStability(difficulty: state.difficulty,
                                          stability: state.stability,
                                          retrievability: retrievability,
                                          grade: grade, w: w)
            }
            difficulty = nextDifficulty(difficulty: state.difficulty, grade: grade, w: w)
        } else {
            stability = clampStability(w[grade.rawValue - 1])
            difficulty = clampDifficulty(initialDifficulty(grade: grade, w: w))
        }

        let days = nextInterval(stability: stability, settings: settings,
                                decay: decay, factor: factor)
        let due = now.addingTimeInterval(Double(days) * 86_400)

        // reps 与 lapses 按**我们的**算法记：lapses 只在 `again` 时加一，
        // 而那是这个项目对「失手」的定义。
        return Outcome(
            state: MemoryState(
                stability: stability,
                difficulty: difficulty,
                dueAt: due,
                lastReviewAt: now,
                reps: (state?.reps ?? 0) + 1,
                lapses: (state?.lapses ?? 0) + (grade == .again ? 1 : 0),
                // 两个步骤列表都空 ⇒ 永远直接落在 review 那一档。
                fsrsState: 2
            ),
            dueAt: due,
            intervalDays: due.timeIntervalSince(now) / 86_400,
            grade: grade
        )
    }

    // MARK: 公式（逐条对着 py-fsrs 的同名函数）

    /// 整天数，向下取整，不小于 0。对应 Python 的 `(a - b).days` 再 `max(0, …)`。
    static func wholeDays(from start: Date, to end: Date) -> Int {
        max(0, Int(floor(end.timeIntervalSince(start) / 86_400)))
    }

    static func clampStability(_ value: Double) -> Double { max(value, stabilityMin) }

    static func clampDifficulty(_ value: Double) -> Double {
        min(max(value, difficultyMin), difficultyMax)
    }

    /// `_initial_difficulty`，**不夹紧**——`nextDifficulty` 里的 `arg_1` 用的是没夹紧
    /// 的那个值，夹紧了会把均值回归拉偏。
    static func initialDifficulty(grade: Grade, w: [Double]) -> Double {
        w[4] - exp(w[5] * Double(grade.rawValue - 1)) + 1
    }

    /// `_next_interval`。
    ///
    /// **取整要用「四舍六入五成双」。** Python 的 `round()` 是这么干的，而 Swift 的
    /// `rounded()` 是逢五进一——差别只在正好半天那种边界上，但那正是最难查的一类差。
    static func nextInterval(stability: Double, settings: Settings,
                             decay: Double, factor: Double) -> Int {
        let raw = (stability / factor) * (pow(settings.requestRetention, 1 / decay) - 1)
        let rounded = Int(raw.rounded(.toNearestOrEven))
        return min(max(rounded, 1), settings.maximumInterval)
    }

    /// `_short_term_stability`。
    static func shortTermStability(stability: Double, grade: Grade, w: [Double]) -> Double {
        var increase = exp(w[17] * (Double(grade.rawValue) - 3 + w[18]))
            * pow(stability, -w[19])
        if grade != .again { increase = max(increase, 1.0) }
        return clampStability(stability * increase)
    }

    /// `_next_difficulty`，含线性阻尼与均值回归。
    static func nextDifficulty(difficulty: Double, grade: Grade, w: [Double]) -> Double {
        let arg1 = initialDifficulty(grade: .easy, w: w)
        let deltaDifficulty = -(w[6] * Double(grade.rawValue - 3))
        let damped = (10.0 - difficulty) * deltaDifficulty / 9.0
        let arg2 = difficulty + damped
        return clampDifficulty(w[7] * arg1 + (1 - w[7]) * arg2)
    }

    /// `_next_stability`：忘了走一条，记得走另一条。
    static func nextStability(difficulty: Double, stability: Double,
                              retrievability: Double, grade: Grade,
                              w: [Double]) -> Double {
        let next: Double = grade == .again
            ? forgetStability(difficulty: difficulty, stability: stability,
                              retrievability: retrievability, w: w)
            : recallStability(difficulty: difficulty, stability: stability,
                              retrievability: retrievability, grade: grade, w: w)
        return clampStability(next)
    }

    /// `_next_forget_stability`：长期那条与短期那条取小。
    static func forgetStability(difficulty: Double, stability: Double,
                                retrievability: Double, w: [Double]) -> Double {
        let longTerm = w[11]
            * pow(difficulty, -w[12])
            * (pow(stability + 1, w[13]) - 1)
            * exp((1 - retrievability) * w[14])
        let shortTerm = stability / exp(w[17] * w[18])
        return min(longTerm, shortTerm)
    }

    /// `_next_recall_stability`。
    static func recallStability(difficulty: Double, stability: Double,
                                retrievability: Double, grade: Grade,
                                w: [Double]) -> Double {
        let hardPenalty = grade == .hard ? w[15] : 1
        let easyBonus = grade == .easy ? w[16] : 1
        return stability * (1
            + exp(w[8])
            * (11 - difficulty)
            * pow(stability, -w[9])
            * (exp((1 - retrievability) * w[10]) - 1)
            * hardPenalty
            * easyBonus)
    }
}
