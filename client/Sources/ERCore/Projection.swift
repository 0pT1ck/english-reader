import Foundation

/// 投影 projection / 重放 replay / 词池 word pool
///
/// **状态是事件日志的纯函数。** 这里是那句话的实现:把 ``EventLog`` 里的五种事件
/// 重放一遍,得出屏幕要的一切——词池、遇见次数、记忆状态、进度。
/// **没有一样是独立存着的事实**,所以没有「两个来源,旧的那个赢」的余地,
/// 而那正是 P9 的起因(§1 那个 0/13)。
///
/// **每一条规则都是服务端已有的,这里只镜像。** 和 ``ReviewItemState`` 同一条纪律:
/// mirror, never invent。底下每一条都注了它镜像的是哪一处。
///
/// **重放很便宜,所以不缓存、不做快照。** 实测全部历史 375 条,增速约 2.5 万条/年
/// (§14 U3)。什么时候回来看这条:日志到百万级,或者重放超过 200 ms。
public struct Projection: Sendable, Equatable {

    /// 词池的三档。**和服务端 `study_states.pool` 的取值一字不差**,
    /// 因为 §7 的进度报告要按它上报,拼错了服务端就选不对生词。
    public enum Pool: String, Sendable, Codable, CaseIterable {
        case new
        case reviewing
        case graduated
    }

    /// 标记的两种。服务端 `MARK_KINDS`。
    public enum MarkKind: String, Sendable, Codable, CaseIterable {
        /// 不认识。
        case unknown
        /// 模糊。**它是最常用的那一个**,所以当天那一次要封顶
        /// (P7 推翻 P3 决定 18)。
        case fuzzy
    }

    /// 一个条目的身份。**词组是一个整体**:不认识 `account for` 不说明
    /// `account` 怎么样,所以词组自带 `senseId` 0、和词分开计。
    public struct Key: Hashable, Sendable, Codable {
        public var itemType: String
        public var key: String
        public var senseId: Int

        public init(itemType: String = "word", key: String, senseId: Int = 0) {
            self.itemType = itemType
            self.key = key
            self.senseId = senseId
        }
    }

    public struct Item: Sendable, Equatable {
        public var pool: Pool = .new
        /// **标记是一个集合,不是一个布尔。** 镜像服务端:`clear_mark` 只清指定的
        /// 那一种,`demote_if_unmarked` 只在一种都不剩时才退回 `new`。
        public var marks: Set<MarkKind> = []
        /// 被遇见过几次。**只有「读完」会加它**,而且撤销标记之后它保留——
        /// 它确实被遇见过(跨 Phase 不变量)。
        public var encounters: Int = 0
        public var introducedAt: String?
        public var introducedArticleId: Int?
        public var introducedSentenceId: Int?
        /// FSRS 记忆状态。nil ＝ 从没复习过。
        ///
        /// **撤销标记不清它**(镜像服务端:`demote_if_unmarked` 只动 `pool`)。
        /// 所以再标一次会接着原来的排期走,而不是从头来——那是对的:
        /// 你对这个词的记忆没有因为你撤了个标记而重置。
        public var memory: ReviewScheduler.MemoryState?
        /// 最近一次标记发生在哪一天(本地日期,`yyyy-MM-dd`)。
        /// 当天池与封顶要用它。
        public var lastMarkedDay: String?
        /// 当天这一次要不要封顶。**只有「模糊」封顶**:「不认识」在 P3 就是当天问、
        /// 当天算全分(决定 18 的另一半)。
        public var cappedToday: Bool = false

        public init() {}
    }

    /// 一天的记录,打卡条要用。
    public struct Day: Sendable, Equatable {
        public var answered = 0
        public var finishedRounds = 0
        public var readArticles = 0

        public init() {}
    }

    public var items: [Key: Item] = [:]
    public var days: [String: Day] = [:]
    /// 重放时读不出来的事件数。**平时是 0。**
    public var damagedEvents = 0
    /// 重放到哪个本地序号(含)。-1 ＝ 一条都没有。
    public var throughLocalSequence = -1

    public init() {}

    // MARK: 重放

    /// 把一份日志重放成投影。
    ///
    /// - Parameters:
    ///   - weightDecay: 服务端下发的失败权重衰减。**不是这里写死的常量**——
    ///     规则是服务端的,客户端从袋子里抽。
    ///   - settings: 调度器配置,同样来自服务端下发的那一份。
    public static func replay(_ load: EventLogLoad, weightDecay: Double,
                             settings: ReviewScheduler.Settings) -> Projection {
        var projection = Projection()
        projection.damagedEvents = load.damaged

        // 当天那一轮的进行状态,按条目分开记。一轮走完(两个方向都过)才交给
        // 调度器算下一次——「一天是一次复习」,而评级是那一天失误了几次。
        var rounds: [Key: (state: ReviewItemState, day: String)] = [:]

        for event in load.events {
            projection.throughLocalSequence = event.localSequence
            let day = Self.day(of: event.occurredAt)

            switch event.kind {
            case .marked:
                guard let key = Self.key(from: event.payload) else { break }
                var item = projection.items[key] ?? Item()
                if let kind = Self.string(event.payload["kind"]).flatMap(MarkKind.init) {
                    item.marks.insert(kind)
                    item.cappedToday = kind == .fuzzy
                }
                // **进复习队列只有这一条路。** 已经 graduated 的不往回拉:
                // 镜像服务端,只有 `reviewing` 这一档被 demote 碰过。
                if item.pool == .new { item.pool = .reviewing }
                item.lastMarkedDay = day
                if item.introducedAt == nil {
                    item.introducedAt = event.occurredAt
                    item.introducedArticleId = Self.int(event.payload["article_id"])
                    item.introducedSentenceId = Self.int(event.payload["sentence_id"])
                }
                projection.items[key] = item

            case .unmarked:
                guard let key = Self.key(from: event.payload),
                      var item = projection.items[key] else { break }
                if let kind = Self.string(event.payload["kind"]).flatMap(MarkKind.init) {
                    item.marks.remove(kind)
                } else {
                    // 没说哪一种就全清,同服务端 `clear_mark` 的 kind 为空那一支。
                    item.marks.removeAll()
                }
                // **一种都不剩才退回 `new`**,而且只动 `reviewing`。
                if item.marks.isEmpty && item.pool == .reviewing { item.pool = .new }
                // **遇见记录与记忆状态都保留。** 它确实被遇见过;而你对它的记忆
                // 也没有因为撤一个标记就重置。
                item.cappedToday = false
                projection.items[key] = item

            case .read:
                // **读完只增加遇见次数,不改变词池位置。** 试过两版自动记账,
                // 都推翻了(跨 Phase 不变量)。
                var record = projection.days[day] ?? Day()
                record.readArticles += 1
                projection.days[day] = record
                for met in Self.met(in: event.payload) {
                    var item = projection.items[met.key] ?? Item()
                    item.encounters += 1
                    if item.introducedAt == nil {
                        item.introducedAt = event.occurredAt
                        item.introducedArticleId = met.articleId
                        item.introducedSentenceId = met.sentenceId
                    }
                    projection.items[met.key] = item
                }

            case .answered:
                guard let key = Self.key(from: event.payload) else { break }
                var record = projection.days[day] ?? Day()
                record.answered += 1
                projection.days[day] = record

                // 跨天了就是新的一轮。
                var round = rounds[key]?.day == day
                    ? rounds[key]!.state
                    : ReviewItemState()
                guard round.acceptsAnswer else { break }

                let answer = ReviewAnswer(
                    passed: Self.bool(event.payload["passed"]) ?? false,
                    revealed: Self.int(event.payload["revealed"]) ?? 0,
                    easy: Self.bool(event.payload["easy"]) ?? false
                )
                round = round.applying(answer, weightDecay: weightDecay)
                rounds[key] = (round, day)

                guard round.done else { break }
                // 一轮走完,交给调度器。**评级看的是这一天失误了几次**,不是问了几次。
                record = projection.days[day] ?? Day()
                record.finishedRounds += 1
                projection.days[day] = record

                var item = projection.items[key] ?? Item()
                let at = Self.date(of: event.occurredAt) ?? Date(timeIntervalSince1970: 0)
                if let outcome = try? ReviewScheduler.review(
                    state: item.memory, misses: round.misses, now: at,
                    easy: round.easy,
                    revealed: Self.int(event.payload["revealed"]) ?? 0,
                    capped: item.cappedToday,
                    settings: settings
                ) {
                    item.memory = outcome.state
                }
                // 当天那一次的封顶用掉就没了——它说的是「你几分钟前才读到它」。
                item.cappedToday = false
                projection.items[key] = item

            case .spelled:
                // **拼错不影响调度,只记一笔**(P3 决定 13)。所以这里什么都不改。
                break

            case .decided:
                // 决策日志是给人和 AI 查的,不参与任何状态。
                break
            }
        }
        return projection
    }

    // MARK: 给服务端的进度报告(§7)

    /// 词池快照。**这就是那份「进度报告」本身**,不是从日志里推出来的副产品。
    ///
    /// 服务端拿它做两件事:避开已经在学的词,以及保证每篇有一定数量的生词。
    /// 「我学过的」＝ `reviewing` ∪ `graduated`。
    public func poolSnapshot() -> [PoolEntry] {
        items.compactMap { key, item in
            item.pool == .new ? nil
                : PoolEntry(itemType: key.itemType, key: key.key,
                            senseId: key.senseId, pool: item.pool)
        }
        .sorted { ($0.key, $0.senseId) < ($1.key, $1.senseId) }
    }

    public struct PoolEntry: Sendable, Codable, Equatable {
        public var itemType: String
        public var key: String
        public var senseId: Int
        public var pool: Pool
    }

    // MARK: 取字段

    /// **日期按设备本地时钟切。** 排序用服务端的全序序号,判「当天」用设备时钟——
    /// 两个用途分开,别用同一个数(§6 细则 ①)。
    static func day(of timestamp: String) -> String {
        guard let date = date(of: timestamp) else { return String(timestamp.prefix(10)) }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone.current
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d",
                      parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    static func date(of timestamp: String) -> Date? {
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = full.date(from: timestamp) { return parsed }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        return plain.date(from: timestamp)
    }

    /// 条目身份。
    ///
    /// **`answered` 事件必须自带身份,不能只带 `queue_id`。** 那个号是服务端
    /// 队列表的行号,而 P9 之后队列是设备自己组的——日志里引一个别处的行号,
    /// 就不是可重放的日志了。契约上的影响记在 §16。
    static func key(from payload: [String: JSONValue]) -> Key? {
        guard let key = string(payload["item_key"]) ?? string(payload["headword"]) else {
            return nil
        }
        return Key(itemType: string(payload["item_type"]) ?? "word",
                   key: key,
                   senseId: int(payload["sense_id"]) ?? 0)
    }

    struct Met {
        var key: Key
        var articleId: Int?
        var sentenceId: Int?
    }

    /// 「读完」事件自带的词表(§9)。
    ///
    /// **自带而不是事后从文章里推**,两个理由:重放不需要文章内容(不然换台设备
    /// 恢复要把读过的全下回来);而且更忠实——文章将来可能被重新分析过,
    /// 而「我在第 480 篇第 12 句遇见过它」是一件历史事实。
    static func met(in payload: [String: JSONValue]) -> [Met] {
        guard case .array(let list)? = payload["met"] else { return [] }
        let articleId = int(payload["article_id"])
        return list.compactMap { entry in
            guard case .object(let fields) = entry, let key = key(from: fields) else {
                return nil
            }
            return Met(key: key, articleId: articleId,
                       sentenceId: int(fields["sentence_id"]))
        }
    }

    static func string(_ value: JSONValue?) -> String? {
        if case .string(let text)? = value { return text }
        return nil
    }

    static func int(_ value: JSONValue?) -> Int? {
        switch value {
        case .int(let number): return number
        case .double(let number): return Int(number)
        default: return nil
        }
    }

    static func bool(_ value: JSONValue?) -> Bool? {
        if case .bool(let flag)? = value { return flag }
        return nil
    }
}
