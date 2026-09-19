import Foundation
import Testing
@testable import ERCore

/// 把服务端算出来的排期原样重放一遍，看这一份算不算得一样。
///
/// `scheduler-vectors.json` 里的期望值是**调用服务端真实代码得到的**
/// （`scripts/export_scheduler_vectors.py`），所以这里红了就是这一份错了。
/// 方向很重要：手写的期望值没有权威性，而两边「都通过」也可能只是两边错得一样。
///
/// **这是一次性的交接凭证。** P9 把排期搬到设备上之后服务端不再需要 FSRS，
/// `py-fsrs` 会从依赖里删掉；在删之前，这个文件要能证明两边算得一样。
struct SchedulerVectorTests {

    // MARK: 向量文件

    struct Vectors: Decodable {
        struct Settings: Decodable {
            let fsrs_package: String
            let enable_fuzzing: Bool
            let desired_retention: Double
            let maximum_interval: Int
            let parameters: [Double]
            let parameters_configured: Bool
        }
        struct GradeCase: Decodable {
            let misses: Int
            let easy: Bool
            let revealed: Int
            let capped: Bool
            let rating: Int
            let rating_name: String
        }
        struct Grades: Decodable {
            let cases: [GradeCase]
        }
        /// 起点状态。**`stability` 为空 ＝ 从没复习过**，和服务端 `to_card` 的
        /// 判断一致（`if not state or state.get("stability") is None`）。
        struct Initial: Decodable {
            let stability: Double?
            let difficulty: Double?
            let fsrs_state: Int?
            let due_at: String?
            let last_review_at: String?
            let reps: Int?
            let lapses: Int?
        }
        struct Review: Decodable {
            let misses: Int
            let easy: Bool?
            let revealed: Int?
            let capped: Bool?
            let after_days: Double?
        }
        struct Step: Decodable {
            let rating: Int
            let rating_name: String
            let interval_days: Double
            let stability: Double
            let difficulty: Double
            let fsrs_state: Int
            let reps: Int
            let lapses: Int
        }
        struct ScheduleCase: Decodable {
            let name: String
            let initial: Initial?
            let reviews: [Review]
            let expected: [Step]
        }
        struct Schedules: Decodable {
            let now: String
            let cases: [ScheduleCase]
        }
        let settings: Settings
        let ratings: Grades
        let schedules: Schedules
    }

    static func load() throws -> Vectors {
        let url = Bundle.module.url(forResource: "Fixtures/scheduler-vectors",
                                    withExtension: "json")
        #expect(url != nil,
                "找不到 scheduler-vectors.json——先跑 scripts/export_scheduler_vectors.py")
        return try JSONDecoder().decode(Vectors.self, from: Data(contentsOf: url!))
    }

    /// ISO8601 带时区偏移，秒精度（导出时用的是 `timespec="seconds"`）。
    static func date(_ text: String) throws -> Date {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        let parsed = formatter.date(from: text)
        #expect(parsed != nil, "时间解不开：\(text)")
        return parsed ?? Date(timeIntervalSince1970: 0)
    }

    /// 双精度的容差，向量文件自己声明的那一个。
    ///
    /// **绝对 1e-6，不是相对。** 文件里的数是四舍五入到小数 6 位的，
    /// 所以不论数值多大，光是舍入就能差到 5e-7；而两种语言算同一组公式的差
    /// 实测远小于这个。收得更紧只会把舍入误差报成分歧。
    static let tolerance = 1e-6

    // MARK: 评级映射

    @Test("评级映射：32 种组合逐个对上服务端")
    func gradeMapping() throws {
        let vectors = try Self.load()
        #expect(vectors.ratings.cases.count == 32,
                "全枚举应当是 4 × 2 × 2 × 2 ＝ 32 条，实际 \(vectors.ratings.cases.count)")

        for expected in vectors.ratings.cases {
            let actual = ReviewScheduler.grade(
                misses: expected.misses,
                easy: expected.easy,
                revealed: expected.revealed,
                capped: expected.capped
            )
            #expect(actual.rawValue == expected.rating, """
                misses=\(expected.misses) easy=\(expected.easy) \
                revealed=\(expected.revealed) capped=\(expected.capped)：\
                服务端给 \(expected.rating_name)，这一份给 \(actual)
                """)
        }
    }

    /// 那三格是 2026-09-17 拆开两个上限时动的，单独立一条，
    /// 因为它们正是「混成一个条件」时会错的地方。
    @Test("两个上限不是同一条规则：Easy 遇提示降一档、遇当天封顶到 Hard")
    func ceilingsDiffer() throws {
        let hinted = ReviewScheduler.grade(misses: 0, easy: true, revealed: 1)
        let capped = ReviewScheduler.grade(misses: 0, easy: true, capped: true)
        #expect(hinted == .good, "提示是降一档：Easy → Good，实际 \(hinted)")
        #expect(capped == .hard, "当天刚标是封顶：Easy → Hard，实际 \(capped)")
        // 提示压不到 again 以下——答对了就不该被记成忘了。
        #expect(ReviewScheduler.grade(misses: 5, revealed: 1) == .again)
        #expect(ReviewScheduler.grade(misses: 1, revealed: 1) == .hard)
    }

    // MARK: 排期

    @Test("排期：逐个场景重放，间隔、记忆强度、难度、轮次都要对上")
    func schedules() throws {
        let vectors = try Self.load()
        let settings = ReviewScheduler.Settings(
            requestRetention: vectors.settings.desired_retention,
            maximumInterval: vectors.settings.maximum_interval,
            // 显式喂参数。两个包的「默认」不是同一组，见 Settings 的注释。
            parameters: vectors.settings.parameters,
            enableFuzz: vectors.settings.enable_fuzzing
        )
        #expect(settings.enableFuzz == false, "向量必须是关掉抖动导出的")
        #expect(settings.parameters.count == 21,
                "服务端用的是 FSRS-6（21 个参数），实际 \(settings.parameters.count)")

        let start = try Self.date(vectors.schedules.now)

        for scenario in vectors.schedules.cases {
            var state = Self.memoryState(from: scenario.initial)
            var now = start

            for (index, review) in scenario.reviews.enumerated() {
                now = now.addingTimeInterval((review.after_days ?? 0) * 86_400)
                let outcome = try ReviewScheduler.review(
                    state: state,
                    misses: review.misses,
                    now: now,
                    easy: review.easy ?? false,
                    revealed: review.revealed ?? 0,
                    capped: review.capped ?? false,
                    settings: settings
                )
                let expected = scenario.expected[index]
                let where_ = "\(scenario.name)｜第 \(index + 1) 次"

                #expect(outcome.grade.rawValue == expected.rating,
                        "\(where_)：评级 服务端 \(expected.rating_name) ≠ 这一份 \(outcome.grade)")
                #expect(outcome.state.fsrsState == expected.fsrs_state,
                        "\(where_)：FSRS 档位 \(expected.fsrs_state) ≠ \(outcome.state.fsrsState)")
                #expect(outcome.state.reps == expected.reps,
                        "\(where_)：轮次 \(expected.reps) ≠ \(outcome.state.reps)")
                #expect(outcome.state.lapses == expected.lapses,
                        "\(where_)：失手数 \(expected.lapses) ≠ \(outcome.state.lapses)")
                Self.close(outcome.intervalDays, expected.interval_days, "\(where_)：间隔天数")
                Self.close(outcome.state.stability, expected.stability, "\(where_)：记忆强度")
                Self.close(outcome.state.difficulty, expected.difficulty, "\(where_)：难度")

                state = outcome.state
                // 下一次复习默认发生在这一次的到期时刻，同导出脚本。
                now = outcome.dueAt
            }
        }
    }

    /// 坑 §4.5 那条序列单独立一条：它同时钉住「关掉同日重复」与 180 天封顶，
    /// 而当年接错线的那一版给的是 8→66→180。
    @Test("五次一次过的间隔序列是 2→11→46→163→180，不是 8→66→180")
    func intervalSequence() throws {
        let vectors = try Self.load()
        let scenario = vectors.schedules.cases.first { $0.reviews.count == 5 }
        #expect(scenario != nil, "向量里应当有那个连续五次的场景")
        let days = (scenario?.expected ?? []).map { Int($0.interval_days.rounded()) }
        #expect(days == [2, 11, 46, 163, 180], "向量里的序列是 \(days)")
    }

    // MARK: 工具

    static func memoryState(from initial: Vectors.Initial?) -> ReviewScheduler.MemoryState? {
        guard let initial, let stability = initial.stability,
              let difficulty = initial.difficulty else { return nil }
        return ReviewScheduler.MemoryState(
            stability: stability,
            difficulty: difficulty,
            dueAt: initial.due_at.flatMap { try? date($0) },
            lastReviewAt: initial.last_review_at.flatMap { try? date($0) },
            reps: initial.reps ?? 0,
            lapses: initial.lapses ?? 0,
            fsrsState: initial.fsrs_state ?? 2
        )
    }

    static func close(_ actual: Double, _ expected: Double, _ label: String) {
        #expect(abs(actual - expected) <= tolerance,
                "\(label)：服务端 \(expected) ≠ 这一份 \(actual)（差 \(abs(actual - expected))）")
    }
}
