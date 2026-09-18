import Foundation

/// 打卡日历与连续天数。
///
/// **四种状态，而第三种正是这个类型存在的理由。** 两个池子都清空 → 绿；
/// 有活而留着 → 红；只清空一个、**或者那天压根没活** → 灰。
/// 最后那种要紧:**系统没派活的日子不该判成你失败**。
///
/// **连续天数只数绿的**，而今天还没做完不打断它——你可能正要去做。
/// 这是这里唯一一处宽容，而它是有意的:一个在你还没机会动手就归零的计数器，
/// 量的是时钟，不是你。
///
/// **个人的连续天数做，打卡分享不做**（主文档 §J）。私下的一个计数不给任何人看、
/// 不和任何人比，但它和排行榜挨得够近，所以这条线写在这儿而不是留着以后重推。
///
/// ## 为什么不是简单地「每天重放一次」
///
/// 一天算什么颜色，取决于**那一天当时的队列**——而队列取决于那一刻的记忆状态。
/// 所以不能拿最终状态去算过去某一天:那会把后来才排到期的条目算进那天。
///
/// 而每一天都从头重放一次是 `O(天数 × 事件数)`，连续天数要回看 400 天，
/// 那就是四百遍。这里改成**一次前向重放、在日界处求值**:
/// `O(事件数 + 天数 × 条目数)`。没有任何事件的那些天也求得到值——
/// 它们的状态就是「上一条事件之后的状态」，而那正是前向走到那里时手上的东西。
public enum ReviewCalendar {

    /// 一天能是什么。**名字和服务端一字不差**，客户端只上色——
    /// 「那天算不算完成」是一条规则，不是渲染问题。
    public enum Status: String, Sendable, Codable {
        case complete    // 两项都做完了
        case partial     // 只做完一项，或者那天压根没活
        case missed      // 有活，一项都没做完
        case unknown     // 那天没有任何记录，重建不出来
    }

    public struct Day: Sendable, Equatable {
        public var day: String
        public var status: Status
        public var isToday: Bool
    }

    /// 回看多少天算连续。**有上限**，否则一次查询能走好几年。
    public static let lookBack = 400

    /// 一天的两个池子各有多少、做完多少。
    struct Counts {
        var total: [Projection.Bucket: Int] = [:]
        var done: [Projection.Bucket: Int] = [:]
        /// 那天有没有任何记录。没有就是 `unknown`——**不猜**。
        var recorded = false
    }

    /// 一天的颜色。
    ///
    /// **只有真的有活的池子才有投票权。** 空池子在「这天做完了没有」这件事上算完成，
    /// 但它不许算成**进展**——第一版让它算了，于是一天有 13 条到期、一条没做，
    /// 回来的是灰而不是红，因为空着的「今日学习」投了「做完了」。
    static func verdict(_ counts: Counts?) -> Status {
        guard let counts, counts.recorded else { return .unknown }
        var worked: [Bool] = []
        for bucket in Projection.Bucket.allCases {
            let total = counts.total[bucket] ?? 0
            if total > 0 { worked.append((counts.done[bucket] ?? 0) >= total) }
        }
        if worked.isEmpty { return .partial }        // 那天两项都是空的，没活可做
        if worked.allSatisfy({ $0 }) { return .complete }
        if worked.contains(true) { return .partial }
        return .missed
    }

    /// 算出一段日历，以及连续天数。
    ///
    /// - Parameters:
    ///   - today: 客户端眼里的今天（`yyyy-MM-dd`）。**用设备时钟切**。
    ///   - span: 显示多少天。
    public static func build(_ load: EventLogLoad,
                             weightDecay: Double,
                             settings: ReviewScheduler.Settings,
                             today: String,
                             span: Int = 7) -> (days: [Day], streak: Int) {
        let span = max(1, min(span, 60))
        // 连续天数要回看得更远，所以求值的窗口取两者的并集。
        let window = max(span, lookBack)
        let all = dayKeys(endingAt: today, count: window)
        let counts = evaluate(load, weightDecay: weightDecay, settings: settings,
                              days: all)

        let visible = dayKeys(endingAt: today, count: span).map { key in
            Day(day: key, status: verdict(counts[key]), isToday: key == today)
        }
        return (visible, streak(counts, today: today))
    }

    /// **一次前向重放，在日界处求值。**
    ///
    /// 走到某一天的末尾时，手上的状态就是那一天结束时的状态——
    /// 拿它算那一天的队列。没有事件的那些天照样求得到值，
    /// 因为「上一条事件之后的状态」就是它们的状态。
    static func evaluate(_ load: EventLogLoad,
                         weightDecay: Double,
                         settings: ReviewScheduler.Settings,
                         days: [String]) -> [String: Counts] {
        var out: [String: Counts] = [:]
        var index = 0
        var consumed: [LoggedEvent] = []

        for day in days {
            // 把这一天（含）之前的事件都吃进去。
            while index < load.events.count,
                  Projection.day(of: load.events[index].occurredAt) <= day {
                consumed.append(load.events[index])
                index += 1
            }
            let projection = Projection.replay(
                EventLogLoad(events: consumed, damaged: 0, tornTail: false),
                weightDecay: weightDecay, settings: settings)
            guard let end = endOfDay(day) else { continue }
            var counts = Counts()
            // **那天有记录吗**:有事件、或者那天的队列非空。
            // 后者是「有活而你没开 App」——服务端那边是 `missed`，这里也是。
            let queue = projection.todayQueue(day: day, now: end)
            counts.recorded = !queue.isEmpty
                || consumed.contains { Projection.day(of: $0.occurredAt) == day }
            for entry in queue {
                counts.total[entry.bucket, default: 0] += 1
                if entry.state.done { counts.done[entry.bucket, default: 0] += 1 }
            }
            out[day] = counts
        }
        return out
    }

    /// 到今天为止连续做完了几天。
    ///
    /// **今天还没做完不打断它。** 所以今天是绿的就从今天数起，否则从昨天数起。
    static func streak(_ counts: [String: Counts], today: String) -> Int {
        let keys = dayKeys(endingAt: today, count: lookBack).reversed()
        var iterator = Array(keys).makeIterator()
        guard var cursor = iterator.next() else { return 0 }
        if verdict(counts[cursor]) != .complete {
            guard let yesterday = iterator.next() else { return 0 }
            cursor = yesterday
        }
        var count = 0
        while verdict(counts[cursor]) == .complete {
            count += 1
            guard let next = iterator.next() else { break }
            cursor = next
        }
        return count
    }

    // MARK: 日期

    /// **和 `Projection.day(of:)` 用同一个时区（设备本地）。**
    /// 两边不一致的话，日历的日期键和投影的分日就对不上——而那种错
    /// 只在跨时区或半夜才现形，平时看着一切正常。
    static var calendar: Foundation.Calendar {
        var calendar = Foundation.Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone.current
        return calendar
    }

    static func key(of date: Date) -> String {
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d",
                      parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    static func midnight(of day: String) -> Date? {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var components = DateComponents()
        components.year = parts[0]
        components.month = parts[1]
        components.day = parts[2]
        return calendar.date(from: components)
    }

    static func dayKeys(endingAt today: String, count: Int) -> [String] {
        guard let end = midnight(of: today) else { return [] }
        return (0..<count).reversed().compactMap { offset in
            calendar.date(byAdding: .day, value: -offset, to: end).map(key(of:))
        }
    }

    /// 那一天的末尾（本地时间的最后一瞬）。到期判断用它:
    /// **那一天里任何时刻到期的都算那天的活。**
    static func endOfDay(_ day: String) -> Date? {
        guard let start = midnight(of: day),
              let next = calendar.date(byAdding: .day, value: 1, to: start) else {
            return nil
        }
        return next.addingTimeInterval(-1)
    }
}
