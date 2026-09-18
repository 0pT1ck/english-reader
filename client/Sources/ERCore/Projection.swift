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

        public init() {}

        /// 这一天要不要封顶。**算出来的,不是存着的。**
        ///
        /// 三个条件缺一不可,镜像服务端 `session.collect`:标的是**模糊**
        /// (「不认识」在 P3 就是当天问、当天算全分,决定 18 的另一半)、
        /// 标在**今天**、而且**从没排过期**——已经有记忆状态的词是到期回来的,
        /// 不是几分钟前才读到的。
        public func capped(on day: String) -> Bool {
            marks.contains(.fuzzy) && lastMarkedDay == day && memory == nil
        }
    }

    /// 一天的记录,打卡条要用。
    public struct Day: Sendable, Equatable {
        public var answered = 0
        public var finishedRounds = 0
        public var readArticles = 0

        public init() {}
    }

    /// 当天那一轮走到哪儿了。**只保留最近一天的**——跨天就是新的一轮
    /// (P3 决定 7:解锁每一轮重新挣)。
    ///
    /// **桶和封顶在这一轮开始时就定死,之后不再算。** 镜像服务端:队列行是
    /// `enqueue` 那一刻定好 `bucket` 与 `capped` 的,做完只是打上 `done_at`。
    /// 这一条是 2026-09-17 被测试逼出来的——原先每次都现算,而一轮做完之后
    /// 条目有了排期,现算的结果就变成「还没到点」,**于是那张卡从今天的队列里
    /// 消失了,「已复习」那个数也跟着丢**。而那正是这个 Phase 要修的症状本身。
    public struct Round: Sendable, Equatable {
        public var day: String
        public var bucket: Bucket
        public var capped: Bool
        public var state: ReviewItemState
    }

    public var items: [Key: Item] = [:]
    public var rounds: [Key: Round] = [:]
    public var days: [String: Day] = [:]

    /// 读完过的文章。**考句池与提示池的划分要它**（见 ``SentencePool``）。
    public var finishedArticles: Set<Int> = []

    /// 确定见过的句子，一条一条。
    ///
    /// **它补的是「读完文章」这个代理指标的缺口**，而那个缺口在最普通的情形里就出现:
    /// 你是**在读的时候**标记的，所以标记所在那一句肯定见过——**而文章可能几小时后
    /// 才读完，也可能永远不读完**。不管的话那一句算「没见过」，可以被抽成考题:
    /// 于是你被五分钟前刚读过的那一句考了，看着像道容易题，落下来是一个虚高的评级，
    /// **而没有任何东西会报告它**。
    ///
    /// 服务端那边是两个来源（`study_marks.sentence_id` 与
    /// `study_states.introduced_sentence_id`）。在日志这边它们合成一个:
    /// 标记事件带着 `sentence_id`，而日志只增不减——撤销标记也不会让它消失，
    /// 那正是服务端留 `introduced_sentence_id` 想要的性质。
    public var seenSentences: Set<Int> = []
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

        for event in load.events {
            projection.throughLocalSequence = event.localSequence
            let day = Self.day(of: event.occurredAt)

            switch event.kind {
            case .marked:
                guard let key = Self.key(from: event.payload) else { break }
                var item = projection.items[key] ?? Item()
                if let kind = Self.string(event.payload["kind"]).flatMap(MarkKind.init) {
                    item.marks.insert(kind)
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
                // 标记所在那一句肯定见过。**撤销标记不会让它消失**——
                // 日志只增不减，而那正是服务端留 `introduced_sentence_id` 的用意。
                if let sentence = Self.int(event.payload["sentence_id"]) {
                    projection.seenSentences.insert(sentence)
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
                projection.items[key] = item

            case .read:
                // **读完只增加遇见次数,不改变词池位置。** 试过两版自动记账,
                // 都推翻了(跨 Phase 不变量)。
                var record = projection.days[day] ?? Day()
                record.readArticles += 1
                projection.days[day] = record
                if let article = Self.int(event.payload["article_id"]) {
                    projection.finishedArticles.insert(article)
                }
                for met in Self.met(in: event.payload) {
                    // **只给已经有记录的条目加,不为没标过的词建行。**
                    // 镜像服务端那句 `if key in known: touch_state(...)`——
                    // 遇见记录是挂在一条已有记录上的计数，而记录是标记那一刻建的。
                    // 少了这一条，一篇文章会为它的每个实词造一行,
                    // 而那些词你并没有在学。
                    guard var item = projection.items[met.key] else { continue }
                    // **加的是出现次数,不是加一**——同服务端那句 `COUNT(*) AS n`。
                    item.encounters += met.count
                    projection.items[met.key] = item
                }

            case .answered:
                guard let key = Self.key(from: event.payload) else { break }
                var record = projection.days[day] ?? Day()
                record.answered += 1
                projection.days[day] = record

                // 跨天了就是新的一轮。**新一轮开始时定桶与封顶**,
                // 用的是这一刻的条目状态——之后它会因为有了排期而变。
                let existing = projection.rounds[key]?.day == day
                    ? projection.rounds[key]
                    : nil
                let before = projection.items[key] ?? Item()
                let bucket = existing?.bucket
                    ?? (before.memory == nil
                        ? (before.lastMarkedDay == day ? Bucket.today : .due)
                        : .due)
                let capped = existing?.capped ?? before.capped(on: day)
                var round = existing?.state ?? ReviewItemState()
                guard round.acceptsAnswer else { break }

                let answer = ReviewAnswer(
                    passed: Self.bool(event.payload["passed"]) ?? false,
                    revealed: Self.int(event.payload["revealed"]) ?? 0,
                    easy: Self.bool(event.payload["easy"]) ?? false
                )
                round = round.applying(answer, weightDecay: weightDecay)
                projection.rounds[key] = Round(day: day, bucket: bucket,
                                               capped: capped, state: round)

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
                    // 用这一轮开始时定下的那个,不是现算的——现算的话
                    // 在同一次调用里就已经不对了。
                    capped: capped,
                    settings: settings
                ) {
                    item.memory = outcome.state
                }
                // 封顶算出来就自己消失了:这一轮之后 `memory` 不再是 nil，
                // 而 `capped(on:)` 的第三个条件正是「从没排过期」。
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
        var count: Int
    }

    /// 「读完」事件自带的词表(§9)。
    ///
    /// **自带而不是事后从文章里推**,两个理由:重放不需要文章内容(不然换台设备
    /// 恢复要把读过的全下回来);而且更忠实——文章将来可能被重新分析过,
    /// 而「我在第 480 篇第 12 句遇见过它」是一件历史事实。
    ///
    /// 词表由 `Encounters.met(in:)` 算出来,它逐条镜像服务端那句 SQL。
    /// **没有 `met` 的事件就是没有**:那是一条来自还不带词表的旧客户端的事件,
    /// 重放它只算「读完一篇」,不动任何遇见次数——**而不是去猜**。
    static func met(in payload: [String: JSONValue]) -> [Met] {
        guard case .array(let list)? = payload["met"] else { return [] }
        return list.compactMap { entry in
            guard case .object(let fields) = entry, let key = key(from: fields) else {
                return nil
            }
            return Met(key: key, count: int(fields["n"]) ?? 1)
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

// MARK: - 今天的队列

extension Projection {

    /// 两个池子。
    public enum Bucket: String, Sendable, Codable, CaseIterable {
        /// 今天刚标的，今天就问（P3 决定 18／19）。
        case today
        /// 排期说到点了，或者标了却一直没排上（那是欠着的）。
        case due
    }

    public struct QueueEntry: Sendable, Equatable {
        public var key: Key
        public var bucket: Bucket
        /// 这一次封顶。见 ``Item/capped(on:)``。
        public var capped: Bool
        /// 这一轮走到哪儿了。抽题、解锁、权重都看它。
        public var state: ReviewItemState
    }

    /// 今天该问哪些，每个在哪个池子里。
    ///
    /// **逐条镜像服务端 `session.collect`**，一条都不是这里新想的：
    ///
    /// * 排期到点的 → `due`
    /// * 从没排过期、**今天**标的 → `today`，而且标的是「模糊」就封顶
    /// * 从没排过期、**更早**标的 → `due`，它是欠着的
    ///
    /// **只看 `reviewing` 这一档。** `new` 是你没想学的（不认识却不标记
    /// ＝ 不想学，系统不替你查证），`graduated` 是已经放过它了。
    ///
    /// - Parameters:
    ///   - day: 本地日期 `yyyy-MM-dd`。**用设备时钟切**，排序才用服务端序号。
    ///   - now: 判到期用的时刻。传进来而不是读时钟，这样排期能被测。
    public func todayQueue(day: String, now: Date) -> [QueueEntry] {
        var entries: [QueueEntry] = []
        // **只取词，不取词组。** 镜像服务端 `session.collect` 那句
        // `study_states WHERE item_type = 'word'`——复习有意跳过词组
        // （`verify_phase3` 2.2 守的就是「它是被有意跳过的，不是碰巧没查到」），
        // 而 P6 定的界面初版也不认词组。
        //
        // **少了这一句，标过的词组会被拉进复习队列**，而那一屏没有能问它的题:
        // 句子池是按词与义项建的，词组拿不到句子，于是它会占着「共」那个数
        // 却永远问不出来——13/13 因此永远到不了。2026-09-18 对照
        // `verify_phase3` 2.2 时发现的，那时这一句还没有。
        for (key, item) in items
        where item.pool == .reviewing && key.itemType == "word" {
            // **今天已经问过的，桶和封顶按那一轮开始时定的来。**
            // 它答完之后会有一个未来的到期时间，而那不该让它从今天的名单上消失——
            // 服务端那边它是一行带着 `done_at` 的队列行，照样在今天的名单里。
            if let round = rounds[key], round.day == day {
                entries.append(QueueEntry(key: key, bucket: round.bucket,
                                          capped: round.capped, state: round.state))
                continue
            }
            let bucket: Bucket
            if let due = item.memory?.dueAt {
                // 还没到点的不进来。**这是唯一一条会把条目挡在外面的规则**，
                // 剩下的都在里面——一个标过的词永远不会「消失」，只会等到点。
                guard due <= now else { continue }
                bucket = .due
            } else {
                bucket = item.lastMarkedDay == day ? .today : .due
            }
            entries.append(QueueEntry(key: key, bucket: bucket,
                                      capped: item.capped(on: day),
                                      state: ReviewItemState()))
        }
        // 稳定的顺序，抽题才复现得出来（抽的随机数由调用方注入）。
        return entries.sorted {
            ($0.key.key, $0.key.senseId) < ($1.key.key, $1.key.senseId)
        }
    }

    /// 两张卡片上的那两个数。
    ///
    /// **这就是 §1 那个 0/13 的落点。** 它们现在从重放算出来，
    /// 而不是读服务端快照里的 `progress.buckets_done`——所以答完一张，
    /// 数字当场对，刷新也不会倒退，离线也一样。
    ///
    /// 写成「已复习/共」而不是「已复习/待复习」（P7 决定 2）：后者会让 30 变成 23。
    public struct Progress: Sendable, Equatable {
        public var total: [Bucket: Int] = [:]
        public var done: [Bucket: Int] = [:]

        public init() {}

        public func total(_ bucket: Bucket) -> Int { total[bucket] ?? 0 }
        public func done(_ bucket: Bucket) -> Int { done[bucket] ?? 0 }
        /// 两个池子都走完了。**只有这时候才能说「今天的所有复习」**——
        /// 只做完一张卡就那么说是句假话（P7 决定 20）。
        public var allDone: Bool {
            let everything = Bucket.allCases
            let t = everything.reduce(0) { $0 + total($1) }
            return t > 0 && everything.allSatisfy { done($0) >= total($0) }
        }
    }

    public func progress(day: String, now: Date) -> Progress {
        var progress = Progress()
        for entry in todayQueue(day: day, now: now) {
            progress.total[entry.bucket, default: 0] += 1
            if entry.state.done { progress.done[entry.bucket, default: 0] += 1 }
        }
        return progress
    }

    /// 拼写那一轮开没开：**当天该复习的全部走完之后才为真**（P3 决定 13）。
    ///
    /// 第三个条件是 2026-09-16 服务端补的：这一轮拼过了就不再提示，
    /// 少了它拼完回主界面那一行还在、点进去又是全部的词。
    /// 这里由调用方给 `spelledToday`，因为「拼过没有」是当天的事实，
    /// 投影里 `days` 那张表还没记它。
    public func spellingAvailable(day: String, now: Date, spelledToday: Bool) -> Bool {
        !spelledToday && progress(day: day, now: now).allDone
    }
}
