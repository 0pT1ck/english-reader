import Foundation
import ERContract

/// 今天这一份复习：**内容来自服务端，状态来自重放，分池用本机读过什么**。
///
/// **为什么在 Core 而不在视图模型里。** 两个宿主都要它:手机上那一屏，
/// 和命令行客户端。而 P5 那条纪律说得很清楚——同一条规则写两遍就会漂，
/// 而漂了不会报错。所以拼装在这里，宿主只负责显示。
///
/// **这个类型不决定任何规则。** 桶与封顶来自 ``Projection/todayQueue(day:now:)``
/// （它镜像服务端 `session.collect`），分池来自 ``SentencePool``
/// （镜像 `sentences.split_pools`），当天那一轮的状态机是 ``ReviewItemState``
/// （有服务端导出的向量校验）。这里做的只是**把三份东西对到一起**。
public struct ReviewDay: Sendable {

    /// 一道能问的题。
    public struct Entry: Sendable {
        public var key: Projection.Key
        public var bucket: Projection.Bucket
        public var capped: Bool
        public var state: ReviewItemState
        /// 拼出来的题目。
        ///
        /// **复用 `ReviewItem` 这个契约类型，而不是另定一个。** 它装的信息一字不差，
        /// 而复用它意味着 `HintLadder`、会话屏、那几套向量测试一行都不用改。
        /// 代价要说清楚:它带着一个 `queue_id`，而那个字段现在恒为 0——
        /// 见 ``ReviewDay/assemble(pool:projection:day:now:)``。
        public var item: Components.Schemas.ReviewItem
    }

    public var entries: [Entry] = []

    /// 投影说该问、而句子池里还没有它，有几个。
    ///
    /// **平时是 0。** 非零的情形是真实的:你离线标了个词，句子池还没重新取下来。
    /// 那个词没有丢（投影里它在），只是还问不了。**要显示出来**——
    /// 把它算进「共」会让 13/13 永远到不了，而不说出来的话，
    /// 「今天 12 个」和「13 个」的差别没人解释得了。
    public var awaitingContent = 0

    /// 服务端手上那份词池快照是什么时候的。nil ＝ 它还没收到过。
    ///
    /// **那和「你没在学任何词」是两件事**，所以它单独记着:
    /// 空列表 ＋ 这个为 nil，说的是「先联网报一次再说」。
    public var poolReportedAt: String?

    public init() {}

    /// 两张卡上那两个数。**只数能问的那些**，理由见 ``awaitingContent``。
    public func total(_ bucket: Projection.Bucket) -> Int {
        entries.count { $0.bucket == bucket }
    }

    public func done(_ bucket: Projection.Bucket) -> Int {
        entries.count { $0.bucket == bucket && $0.state.done }
    }

    /// 两个池子都走完了。**只有这时候才能说「今天的所有复习」**——
    /// 只做完一张卡就那么说是句假话（P7 决定 20）。
    public var allDone: Bool {
        !entries.isEmpty && entries.allSatisfy { $0.state.done }
    }

    /// 拼写那一轮开没开：**当天该复习的全部走完之后才为真**（P3 决定 13）。
    public var spellingAvailable: Bool { allDone }

    /// 今天要拼的词。**一个词多个义项只拼一次**——拼的是词形，跟义项无关。
    public func spellingWords() -> [SpellingWord] {
        var seen = Set<String>()
        return entries
            .sorted { ($0.key.key, $0.key.senseId) < ($1.key.key, $1.key.senseId) }
            .filter { seen.insert($0.key.key).inserted }
            .map { SpellingWord(key: $0.key.key,
                                phonetic: $0.item.word?.phonetic,
                                gloss: Self.gloss(of: $0.item)) }
    }

    /// 提示给中文，不给英文概念——**拼写考的是「听到／想到这个意思，写得出这个词」**，
    /// 而英文概念里常常就含着这个词的同根词。
    static func gloss(of item: Components.Schemas.ReviewItem) -> String {
        if let list = item.sense?.gloss_zh?.value1, !list.isEmpty {
            return list.joined(separator: "，")
        }
        if let single = item.sense?.gloss_zh?.value2 { return single }
        return item.word?.translation ?? ""
    }

    /// 把内容、状态、读过什么对到一起。
    ///
    /// **`queue_id` 恒为 0，而那不是没清干净的占位符。** 它原本是服务端队列表的
    /// 行号、每天重建，而队列现在是设备自己组的——再引它就不是可重放的日志了。
    /// 服务端那个作答端点认得「0 ＋ 身份」那一支:它只把事件记下来，不再算一遍。
    public static func assemble(pool: Components.Schemas.SentencePoolResponse?,
                                projection: Projection,
                                day: String,
                                now: Date) -> ReviewDay {
        var out = ReviewDay()
        out.poolReportedAt = pool?.reported_at

        var content: [Projection.Key: Components.Schemas.StudyItemSentences] = [:]
        for item in pool?.items ?? [] {
            content[Projection.Key(itemType: item.item_type, key: item.item_key,
                                   senseId: item.sense_id)] = item
        }

        let queue = projection.todayQueue(day: day, now: now)
        for entry in queue {
            guard let item = content[entry.key] else { continue }
            let split = SentencePool.split(item.sentences,
                                           finished: projection.finishedArticles,
                                           seen: projection.seenSentences)
            out.entries.append(Entry(
                key: entry.key, bucket: entry.bucket, capped: entry.capped,
                state: entry.state,
                item: Components.Schemas.ReviewItem(
                    queue_id: 0,
                    item_type: entry.key.itemType,
                    item_key: entry.key.key,
                    sense_id: entry.key.senseId,
                    bucket: entry.bucket.rawValue,
                    direction: entry.state.direction.rawValue,
                    asks: entry.state.asks,
                    misses: entry.state.misses,
                    weight: entry.state.weight,
                    done: entry.state.done,
                    word: item.word,
                    sense: item.sense,
                    questions: split.questions,
                    hints: split.hints)))
        }
        out.awaitingContent = queue.count - out.entries.count
        return out
    }
}

/// 拼写那一轮要的三样：词、音标、中文。
///
/// **2026-09-18 从 App 层搬进 Core**（P9 §11）:命令行客户端也要拼写那一轮，
/// 而 `ReviewDay` 在这里算它——留在 App 层就意味着 Core 引用不到它。
public struct SpellingWord: Identifiable, Equatable, Sendable {
    public let key: String
    public let phonetic: String?
    public let gloss: String
    public var id: String { key }

    public init(key: String, phonetic: String?, gloss: String) {
        self.key = key
        self.phonetic = phonetic
        self.gloss = gloss
    }
}
