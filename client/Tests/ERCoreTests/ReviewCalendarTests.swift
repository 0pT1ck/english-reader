import Foundation
import Testing
@testable import ERCore

/// 打卡日历。**四种状态，而第三种是关键的那个。**
///
/// 规则来自服务端 `review/calendar.py`，这里逐条镜像。最要紧的一句是
/// 「系统没派活的日子不该判成你失败」——那是灰，不是红。
struct ReviewCalendarTests {
    static let settings = ProjectionTests.settings

    static func build(_ events: [(LoggedEvent.Kind, [String: JSONValue], String)],
                      today: String, span: Int = 7)
        -> (days: [ReviewCalendar.Day], streak: Int) {
        ReviewCalendar.build(ProjectionTests.log(events), weightDecay: 0.5,
                            settings: settings, today: today, span: span)
    }

    @Test("有活而一条没做完 → 红")
    func aDayWithWorkLeftUndoneIsMissed() {
        let out = Self.build([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+08:00"),
        ], today: "2026-01-05", span: 1)
        #expect(out.days.map(\.status) == [.missed])
        #expect(out.streak == 0)
    }

    @Test("有活而全做完 → 绿，而且算进连续天数")
    func aFinishedDayIsComplete() {
        let out = Self.build([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+08:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-05T09:00:00+08:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-05T09:01:00+08:00"),
        ], today: "2026-01-05", span: 1)
        #expect(out.days.map(\.status) == [.complete])
        #expect(out.streak == 1)
    }

    /// **这一条是那个文件存在的理由。**
    /// 系统没派活的日子不该判成你失败——那是灰，不是红。
    @Test("那天压根没活 → 灰，不是红")
    func aDayWithNothingToDoIsNotAFailure() {
        // 只读完一篇，没标任何词:队列是空的。
        let met: JSONValue = .array([
            .object(["item_key": .string("x"), "sense_id": .int(0), "n": .int(1)]),
        ])
        let out = Self.build([
            (.read, ["article_id": .int(1), "met": met], "2026-01-05T08:00:00+08:00"),
        ], today: "2026-01-05", span: 1)
        #expect(out.days.map(\.status) == [.partial], "没活可做是灰")
    }

    @Test("完全没有记录的日子 → unknown，不猜")
    func aDayWithNoRecordIsUnknown() {
        let out = Self.build([], today: "2026-01-05", span: 3)
        #expect(out.days.map(\.status) == [.unknown, .unknown, .unknown])
        #expect(out.streak == 0)
    }

    /// **今天还没做完不打断连续天数。** 你可能正要去做，
    /// 而一个在你还没机会动手就归零的计数器，量的是时钟不是你。
    @Test("今天还没做完，连续天数从昨天数起")
    func todayBeingUnfinishedDoesNotBreakTheStreak() {
        var events: [(LoggedEvent.Kind, [String: JSONValue], String)] = []
        // 昨天:标一个词、答完两问 ⇒ 绿。
        events.append((.marked, ["item_key": .string("a"), "kind": .string("unknown")],
                       "2026-01-04T08:00:00+08:00"))
        events.append((.answered, ["item_key": .string("a"), "passed": .bool(true)],
                       "2026-01-04T09:00:00+08:00"))
        events.append((.answered, ["item_key": .string("a"), "passed": .bool(true)],
                       "2026-01-04T09:01:00+08:00"))
        // 今天:又标一个，没答 ⇒ 今天是红的。
        events.append((.marked, ["item_key": .string("b"), "kind": .string("unknown")],
                       "2026-01-05T08:00:00+08:00"))
        let out = Self.build(events, today: "2026-01-05", span: 2)
        #expect(out.days.map(\.status) == [.complete, .missed])
        #expect(out.streak == 1, "从昨天数起，实际 \\(out.streak)")
    }

    /// **空池子不许算成进展。** 第一版让它算了，于是一天有 13 条到期、
    /// 一条没做，回来的是灰而不是红——因为空着的「今日学习」投了「做完了」。
    @Test("一个池子空着，另一个欠着 → 红，不是灰")
    func anEmptyBucketDoesNotVoteForProgress() {
        // 前天标的词，今天到期 ⇒ 只有 due 池有活；today 池是空的。
        let out = Self.build([
            (.marked, ["item_key": .string("owed"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+08:00"),
        ], today: "2026-01-05", span: 1)
        #expect(out.days.map(\.status) == [.missed],
                "空着的 today 池不许把这天投成灰")
    }

    @Test("日期键和投影的分日用同一个时区")
    func dayKeysAgreeWithTheProjection() {
        // 本地时间当天早八点的那条事件，必须落在同一个日期键上。
        let stamp = "2026-01-05T08:00:00" + Self.localOffset()
        #expect(Projection.day(of: stamp) == "2026-01-05")
        #expect(ReviewCalendar.dayKeys(endingAt: "2026-01-05", count: 1) == ["2026-01-05"])
    }

    static func localOffset() -> String {
        let seconds = TimeZone.current.secondsFromGMT()
        let sign = seconds < 0 ? "-" : "+"
        let minutes = abs(seconds) / 60
        return String(format: "%@%02d:%02d", sign, minutes / 60, minutes % 60)
    }
}
