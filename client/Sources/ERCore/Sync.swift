import Foundation
import ERContract

/// 同步 syncing: fetching the day, and draining the outbox.
///
/// **The contract's shape is the offline shape.** One request in the morning
/// brings the whole day — three articles with every gloss attached and every
/// review card with its sentences — and everything the learner does goes into
/// the outbox to be reported whenever there is a network. Online and offline
/// are the same code path, not two.
public struct DayPackage: Sendable {
    /// The bytes exactly as they arrived. Kept because a field this version of
    /// the client does not understand must still survive to the next one.
    public let raw: Data
    public let decoded: Components.Schemas.TodayResponse

    public init(raw: Data) throws {
        self.raw = raw
        do {
            self.decoded = try JSONDecoder().decode(
                Components.Schemas.TodayResponse.self, from: raw)
        } catch {
            throw TransportError.malformed("今日包解不开：\(error)")
        }
    }

    public var articles: [Components.Schemas.ArticleResponse] { decoded.articles }
    /// 复习那一份。**P9 起服务端不再下发它**——复习由设备自己算
    /// （句子来自 `/v1/client/sentences`，状态来自重放）。
    /// 留着这个访问器是为了老服务端:它照旧下发，而读它不伤人。
    public var reviews: Components.Schemas.ReviewDayResponse? { decoded.reviews }

    /// **今日包 ≠ 今天能读的全部。** The server says so in the payload rather
    /// than only in the documentation, because a client that assumed otherwise
    /// would silently hide 452 exam papers and look complete while doing it.
    public var excludesExamPapers: Bool { decoded.excludes_exam_papers }

    /// 服务端定的那几个数：每天几篇、「新备的」算几天、答错之后权重乘多少、
    /// 拼写开不开。**客户端只读它们**——改它们是管理接口的事（P8 §7），
    /// 而这几个值本来就随今日包发下来了，接进界面是零成本的。
    public var settings: Components.Schemas.TodaySettings { decoded.settings }

    /// 排期参数，**从原始字节里读**。
    ///
    /// 服务端 2026-09-17 把它们加进了 `settings`（P9：排期搬到设备上之后，
    /// 设备必须知道服务端配的是什么）。这里不走生成的契约类型，而是直接解 `raw`
    /// —— 原始字节保留的理由原话就是「a field this version of the client does
    /// not understand must still survive to the next one」，而重新生成 Swift
    /// 契约要 Swift 工具链，r5s 上不装（开发场地那条决定的代价）。
    ///
    /// **服务端没下发就返回 nil，绝不回落到一份本地默认。**
    /// 两边各用自己的默认会跑出不同的间隔，而且两边都在按自己的文档正常工作、
    /// 没有东西会报错——这正是 swift-fsrs 与 py-fsrs 那次差点踩进去的形状。
    /// 宁可说「服务端还没更新」，也不能悄悄算出别的排期。
    public var schedulerSettings: ReviewScheduler.Settings? {
        struct Wire: Decodable {
            struct Settings: Decodable {
                let fsrs_parameters: [Double]?
                let fsrs_desired_retention: Double?
                let fsrs_maximum_interval: Int?
                let fsrs_fuzz: Bool?
            }
            let settings: Settings
        }
        guard let wire = try? JSONDecoder().decode(Wire.self, from: raw),
              let parameters = wire.settings.fsrs_parameters, !parameters.isEmpty,
              let retention = wire.settings.fsrs_desired_retention,
              let maximum = wire.settings.fsrs_maximum_interval else { return nil }
        return ReviewScheduler.Settings(
            requestRetention: retention,
            maximumInterval: maximum,
            parameters: parameters,
            // **投影里一律关抖动。** 抖动是「排下一次」时的一点随机，而投影是
            // 把历史重放一遍——重放时抖一次就和当时排的对不上了。
            // 服务端那个开关(`fsrs_fuzz`)管的是服务端自己排期，这里不跟。
            enableFuzz: false
        )
    }

    /// 这份包是哪一天的。缓存要不要用，全看它——**昨天的包不是「旧一点」，
    /// 是错的**：题目做完了、日期变了，照着它渲染会让人对着一份不存在的
    /// 队列答题。
    public var day: String? { decoded.day ?? decoded.reviews?.day }

    /// 顶层回显的学习者。客户端据此认出「这是别人的缓存」，
    /// 而设置页拿它显示名字——**它是服务端给的值，不是自己编的**。
    public var learner: Components.Schemas.Learner { decoded.learner }

    /// 哪些留好的位置真的有值了。`level_estimate` 至今为假，
    /// 所以账号那一行写的是「功能待开发」而不是空白或者 0。
    public var capabilities: Components.Schemas.Capabilities { decoded.capabilities }

    public var articleCount: Int { decoded.articles.count }

    public var metadata: [ArticleMeta] {
        decoded.articles.map { article in
            ArticleMeta(
                id: article.article.id,
                title: article.article.title,
                source: article.article.source ?? "generated",
                wordCount: article.article.word_count ?? 0,
                sentenceCount: article.article.sentence_count ?? 0,
                readAt: article.article.read_at,
                percent: article.progress?.percent ?? 0
            )
        }
    }
}

/// What one attempt to drain the outbox achieved.
public struct SyncReport: Sendable, Equatable {
    public var landed = 0
    public var duplicates = 0
    public var rejected = 0
    /// Entries still in the queue afterwards — rejected ones, and anything the
    /// server never mentioned.
    public var remaining = 0
    /// Set when the attempt did not reach the server at all. Not a failure to
    /// report: it is the normal state on a train.
    public var offline = false

    public init() {}

    /// 把另一次尝试的结果并进来。
    ///
    /// `remaining` 不相加——它是「之后还剩几条」，由调用方在最后统一数一次，
    /// 两个来源各数一半再加起来，只会在中途变化时给出一个谁都不认的数。
    mutating func merge(_ other: SyncReport) {
        landed += other.landed
        duplicates += other.duplicates
        rejected += other.rejected
        offline = offline || other.offline
    }
}

public actor SyncEngine {
    /// 一次上报最多带多少条。
    ///
    /// **比服务端的上限小，而不是等于它。** 三个批量端点都写着
    /// `max_length=500`；取 200 的余量是为了「同一个数字在两处各写一遍」这件事
    /// 本来就会漂——两边相等的话，服务端哪天调小一点，客户端立刻整批被拒，
    /// 而那个拒绝长得像「包有问题」。
    ///
    /// 契约里没有这个数。**这是它该被质疑的地方**:服务端的上限应该由
    /// `/v1/client/me` 下发，客户端照着分片，那样两边就不会各记一份。
    /// 记进主文档 §M，这个 Phase 不动契约。
    static let uploadBatchSize = 200

    private let transport: any Transport
    private let outbox: Outbox
    private let events: EventLog?
    private let articles: ArticleCache
    private let day: DayCache
    /// 句子池的缓存。
    ///
    /// **复用 `DayCache` 而不是照抄一份**:那个类实际上是「一个文件 ＋ 一个版本号」，
    /// 名字窄了而形状正合适。各自一个目录，所以两份内容不会互相覆盖。
    private let sentences: DayCache

    /// - Parameter events: 事件日志。**P9 起它才是那五种事件的来源**；
    ///   发件箱退化成「遥测那几条的队列」。传 nil 是为了让老的调用点
    ///   （和只测传输的测试）还编得过，那时行为和 P9 之前一样。
    /// - Parameter sentences: 句子池的缓存。传 nil 就和 `day` 共用一个目录下的
    ///   另一个文件——**只为让老的调用点还编得过**，正经用法是给它自己的目录。
    public init(transport: any Transport, outbox: Outbox,
                events: EventLog? = nil,
                articles: ArticleCache, day: DayCache,
                sentences: DayCache? = nil) {
        self.transport = transport
        self.outbox = outbox
        self.events = events
        self.articles = articles
        self.day = day
        self.sentences = sentences ?? day
    }

    // MARK: Fetching

    /// Fetch today's package and cache it whole.
    ///
    /// Falls back to the cached copy when there is no network — which is the
    /// point of caching it, and the reason this returns a package rather than
    /// throwing on an absent one.
    public func fetchDay() async throws -> DayPackage {
        do {
            // **先问「变了吗」，而不是直接要一兆。**服务端拿本地这一份的版本号
            // 比一比，没变就回 304 和零字节——今日包实测约 1 MB，过隧道
            // 0.85–1.4 秒，而绝大多数时候它跟盘上那份**一模一样**。
            var headers: [String: String] = [:]
            if let etag = day.etag(), day.load() != nil {
                headers["If-None-Match"] = etag
            }
            let response = try await transport.send(
                HTTPRequest(method: .get, path: "/v1/client/today", headers: headers))

            if response.isNotModified, let cached = day.load() {
                // 没变。本地那份就是最新的，一个字节都不用传。
                return try DayPackage(raw: cached)
            }
            guard response.isOK else {
                throw TransportError.server(
                    status: response.status,
                    body: String(decoding: response.body.prefix(400), as: UTF8.self))
            }
            let package = try DayPackage(raw: response.body)
            try day.store(response.body, etag: response.header("ETag"))
            try articles.remember(package.metadata)
            for article in package.articles where article.preparing == nil {
                // Cached individually as well as inside the package: an article
                // is opened long after the package is stale, and the two paths
                // must return the same thing.
                if let body = try? JSONEncoder().encode(article) {
                    try articles.storeBody(article.article.id, body)
                }
            }
            return package
        } catch let error as TransportError {
            guard case .offline = error, let cached = day.load() else { throw error }
            return try DayPackage(raw: cached)
        }
    }

    /// The cached package, without touching the network.
    ///
    /// **`fetchDay` asks the server first and only falls back to this when the
    /// radio is off — which is not what 架构前提 2 describes.** The package is
    /// meant to be what the day runs on; re-fetching it before showing anything
    /// means every entry to the review tab waits for a megabyte to come back
    /// through the tunnel, measured at 0.85–1.4s because it goes via Los
    /// Angeles. The screen has the answer on disk the whole time.
    ///
    /// So callers show this first and refresh behind it. `day` is checked by
    /// the caller against its own idea of today: a package from yesterday is a
    /// wrong answer, not a stale one.
    public func cachedDay() -> DayPackage? {
        guard let data = day.load() else { return nil }
        return try? DayPackage(raw: data)
    }

    /// One article, from the cache when it is there and from the server when it
    /// is not. This is what makes clearing the cache safe: a cleared article is
    /// one request away.
    public func article(_ id: Int) async throws -> Components.Schemas.ArticleResponse {
        if let cached = try? articles.body(id),
           let decoded = try? JSONDecoder().decode(
               Components.Schemas.ArticleResponse.self, from: cached) {
            return decoded
        }
        let response = try await transport.send(
            HTTPRequest(method: .get, path: "/v1/client/articles/\(id)"))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        let decoded = try JSONDecoder().decode(
            Components.Schemas.ArticleResponse.self, from: response.body)
        // An article still being annotated comes back with `preparing` rather
        // than an error — asking for one is normal. Don't cache that: it is a
        // progress report, not an article.
        if decoded.preparing == nil {
            try articles.storeBody(id, response.body)
        }
        return decoded
    }

    /// 这把令牌是谁的。**测试连接用它。**
    ///
    /// 在这之前那个探针打的是日历那个端点（「最轻的一个」），而日历 P9 搬到了
    /// 设备上。这个是真正最轻的:不读学习记录、不组装任何东西、
    /// 不下发一个字节的内容，而它回答了探针真正关心的两件事——
    /// 令牌认不认，以及对面是谁。
    public func me() async throws -> Components.Schemas.MeResponse {
        let response = try await transport.send(
            HTTPRequest(method: .get, path: "/v1/client/me"))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        return try JSONDecoder().decode(
            Components.Schemas.MeResponse.self, from: response.body)
    }

    // `calendar(days:)` 没有了（P9 §11）。打卡日历是学习记录的一个函数，
    // 而学习记录在设备上——`ReviewCalendar.build` 一次前向重放就把它算出来了，
    // 不用问服务端，也就不会在飞机上变成一片空白。
    //
    // 服务端那个端点同时删掉了:它每答完一天都要重新扫一遍队列与历史，
    // 而那正是「服务端跟着拇指改学习状态」的另一种写法。

    /// 从会合点把别的设备做的事拉下来。
    ///
    /// **P9 §6:同步是双向的。** 在这之前只有上报——一台设备把事件送上去，
    /// 而另一台永远看不到它。状态是事件日志的纯函数，所以只要两台设备手里的
    /// 日志一样，算出来的状态就一样：**不需要合并算法**。Anki 撞到真冲突时要
    /// 弹窗让人选「上传还是下载」，我们不用。
    ///
    /// **自己的事件会原样拉回来，按幂等键跳过。** 游标是「大于某个号」，
    /// 而自己推上去的那些也在那个号后面。
    ///
    /// 返回收下了几条新的。`nil` 表示没有日志可写（没接日志的老调用点）。
    @discardableResult
    public func pull(pageLimit: Int = 500) async throws -> Int {
        guard let events else { return 0 }
        var known = try events.knownIdemKeys()
        var cursor = events.cursor()
        var adopted = 0

        // **有上限地翻页，不写没有出口的循环。** 一年约 2.5 万条事件、一页 500，
        // 所以一次同步最多几十页；给到 200 页是留足余量，而不是「跑到没有为止」——
        // 后者在服务端一直说 `more: true` 时会永不退出。
        for _ in 0..<200 {
            let response = try await transport.send(HTTPRequest(
                method: .get,
                path: "/v1/client/events?after=\(cursor.pulledThrough)&limit=\(pageLimit)"))
            guard response.isOK else {
                throw TransportError.server(
                    status: response.status,
                    body: String(decoding: response.body.prefix(400), as: UTF8.self))
            }
            let page = try Self.feed(from: response.body)
            for item in page.events {
                guard !known.contains(item.idemKey) else { continue }
                // 遥测（打开文章、读到哪、点了哪个词）不进日志——它们不改变状态。
                guard let kind = LoggedEvent.kind(forWireType: item.type) else { continue }
                // `known` 只是省一次读盘;**真正把住重复的是 `appendPulled` 自己**
                // （它按幂等键判，所以并发也只写得进一次）。返回 nil ＝ 已经有了。
                guard try events.appendPulled(kind: kind, payload: item.payload,
                                              idemKey: item.idemKey,
                                              occurredAt: item.occurredAt ?? "") != nil
                else { continue }
                known.insert(item.idemKey)
                adopted += 1
            }
            // **游标一页一推。** 崩在中途的话下次从这一页之后接着走，
            // 而已经写进日志的那些靠幂等键不会重复。
            cursor.pulledThrough = page.through
            try events.setCursor(cursor)
            guard page.more else { break }
        }
        return adopted
    }

    struct Feed: Sendable {
        struct Item: Sendable {
            let sequence: Int
            let idemKey: String
            let type: String
            let payload: [String: JSONValue]
            let occurredAt: String?
        }
        let events: [Item]
        let through: Int
        let more: Bool
    }

    /// 解会合点的回应。
    ///
    /// **手写解码，不走生成的契约类型。** 生成一次要 Swift 工具链，而 r5s 上不装
    /// （开发场地那条决定的代价）。这里解的字段少而稳，手写的风险小于
    /// 「等到能重新生成那天再接」。
    static func feed(from body: Data) throws -> Feed {
        struct Wire: Decodable {
            struct Item: Decodable {
                let sequence: Int
                let idem_key: String
                let type: String
                let payload: [String: JSONValue]
                let occurred_at: String?
            }
            let events: [Item]
            let through: Int
            let more: Bool
        }
        guard let wire = try? JSONDecoder().decode(Wire.self, from: body) else {
            throw TransportError.malformed("事件流解不开")
        }
        return Feed(
            events: wire.events.map {
                Feed.Item(sequence: $0.sequence, idemKey: $0.idem_key, type: $0.type,
                          payload: $0.payload, occurredAt: $0.occurred_at)
            },
            through: wire.through, more: wire.more)
    }

    /// 把词池快照报上去（P9 §7）。
    ///
    /// **这就是那份「进度报告」本身**，不是从日志里推出来的副产品。用户那句话
    /// 原话是「学习完把进度汇报给服务器，服务器根据进度继续生文」——
    /// 服务端拿它做两件事:避开已经在学的词，以及保证每篇有一定数量的生词。
    ///
    /// **只报非 `new` 的。** 服务端要的是「哪些词你已经在学或学过」，
    /// `new` 是补集——而词典里有 318,207 条，非 new 的实测 30 条。
    ///
    /// **PUT，因为它是替换。** 同一份报两遍和报一遍结果一样，所以重试天然安全。
    @discardableResult
    public func reportPool(_ entries: [Projection.PoolEntry],
                           reportedAt: String) async throws -> Int {
        struct Body: Encodable {
            struct Entry: Encodable {
                let item_type: String
                let item_key: String
                let sense_id: Int
                let pool: String
            }
            let reported_at: String
            let entries: [Entry]
        }
        let body = Body(
            reported_at: reportedAt,
            entries: entries.map {
                Body.Entry(item_type: $0.itemType, item_key: $0.key,
                           sense_id: $0.senseId, pool: $0.pool.rawValue)
            })
        let response = try await transport.send(HTTPRequest(
            method: .put, path: "/v1/client/progress/pool",
            body: try JSONEncoder.contract.encode(body)))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        return entries.count
    }

    /// 在学的那些词的句子——**服务端不分池**（P9 §11）。
    ///
    /// **依据是设备上报的词池快照**，所以调用顺序有意义:`drain()` 先推事件、
    /// 再报快照，然后这里取下来的才是对的那一批。没报过快照会拿到空列表，
    /// 而响应里 `reported_at` 为空正是在说「服务端还不知道」——
    /// 那和「你没在学任何词」是两件事。
    ///
    /// **取回来整份存盘。** 句子是内容，而内容要能离线用（架构铁律 2）：
    /// 这一份和今日包一样，是「拿一次、用一天」的东西。
    public func fetchSentences() async throws -> Components.Schemas.SentencePoolResponse {
        do {
            let response = try await transport.send(
                HTTPRequest(method: .get, path: "/v1/client/sentences"))
            guard response.isOK else {
                throw TransportError.server(
                    status: response.status,
                    body: String(decoding: response.body.prefix(400), as: UTF8.self))
            }
            let decoded = try JSONDecoder().decode(
                Components.Schemas.SentencePoolResponse.self, from: response.body)
            try sentences.store(response.body)
            return decoded
        } catch let error as TransportError {
            // 离线就用盘上那份。**这正是存它的理由**——
            // 而拿不到又没有缓存时照实抛错，不装作「你没在学任何词」。
            guard case .offline = error, let cached = sentences.load() else { throw error }
            return try JSONDecoder().decode(
                Components.Schemas.SentencePoolResponse.self, from: cached)
        }
    }

    /// 盘上那份，不碰网络。同 ``cachedDay``:先点亮屏幕，再在后面刷新。
    public func cachedSentences() -> Components.Schemas.SentencePoolResponse? {
        guard let data = sentences.load() else { return nil }
        return try? JSONDecoder().decode(
            Components.Schemas.SentencePoolResponse.self, from: data)
    }

    public func library(shelf: String = "fresh", source: String? = nil)
        async throws -> Components.Schemas.LibraryResponse {
        var path = "/v1/client/library?shelf=\(shelf)"
        if let source { path += "&source=\(source)" }
        let response = try await transport.send(HTTPRequest(method: .get, path: path))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        return try JSONDecoder().decode(
            Components.Schemas.LibraryResponse.self, from: response.body)
    }

    // MARK: Draining

    /// Report everything waiting, in order, and delete only what landed.
    ///
    /// **Three destinations, sent separately**, because they are three
    /// endpoints — but all three are drained in one pass so that "sync" means
    /// one thing to the caller.
    ///
    /// **Answers go in the order they were made.** A review session is a state
    /// machine, so replaying them out of order would produce a different day on
    /// the server than the learner saw on the device.
    @discardableResult
    public func drain() async throws -> SyncReport {
        var report = SyncReport()
        if let events {
            // **五种事件从日志走，不从发件箱走**（P9）。
            //
            // 在这之前两处都写:一条事件先进日志再进发件箱。那中间有一个窄口子——
            // 崩在两次写之间，事件在日志里而发件箱里没有，**于是它永远发不出去**，
            // 而屏幕上什么都不会说（本机重放算得出它，服务端永远不知道）。
            // 现在只有日志是那份记录，发件箱只装遥测。
            report.merge(try await drainLog(events))
        }
        let pending = try outbox.pending()
        if !pending.damaged.isEmpty {
            // Left in place rather than discarded here: throwing away an event
            // is a loss, and it should be a decision the caller makes out loud.
            report.rejected += pending.damaged.count
        }
        // **不提前返回。** 上一版在发件箱为空时直接 return，而那条路绕过了下面
        // 「还剩几条」的统计——日志那半的剩余量会被算成 0，
        // 于是待发数在「发件箱空、日志还有」时显示成零。
        for kind in pending.entries.isEmpty ? [] : [OutboxEntry.Kind.reading, .answer, .spelling] {
            let batch = pending.entries.filter { $0.kind == kind }
            guard !batch.isEmpty else { continue }
            do {
                let verdicts = try await post(kind, batch)
                try outbox.acknowledge(verdicts)
                for verdict in verdicts.values {
                    switch verdict {
                    case .landed: report.landed += 1
                    case .duplicate: report.duplicates += 1
                    case .rejected: report.rejected += 1
                    }
                }
            } catch let error as TransportError {
                guard case .offline = error else { throw error }
                report.offline = true
                break
            }
        }
        // 最后统一数一次，而不是两个来源各数一半——中途变化时那种加法
        // 会给出一个谁都不认的数。
        report.remaining = outbox.count
        if let events {
            report.remaining += (try? events.unreported(
                kinds: LoggedEvent.sendableKinds).count) ?? 0
        }
        return report
    }

    /// 把日志里还没上报的发出去。
    ///
    /// **按种类分批，但序号顺序不打乱。** 复习作答是状态机，乱序会在服务端造出
    /// 另一个一天;而一批里的回应是**逐条**的，所以哪一条落了就记哪一条——
    /// 被拒的那一条只挡住自己，不挡它后面的（见 `Cursor.reporting`）。
    private func drainLog(_ events: EventLog) async throws -> SyncReport {
        var report = SyncReport()
        let pending = try events.unreported(kinds: LoggedEvent.sendableKinds)
        guard !pending.isEmpty else { return report }

        var landed: [Int] = []
        for kind in [OutboxEntry.Kind.reading, .answer, .spelling] {
            let queued = pending.compactMap { event -> (LoggedEvent, OutboxEntry)? in
                guard let envelope = event.envelope, envelope.kind == kind else {
                    return nil
                }
                return (event, envelope)
            }
            guard !queued.isEmpty else { continue }

            var offline = false
            // **分片，而且片大小小于服务端的上限。** 2026-09-18 真机上栽的:
            // 攒到 534 条之后整批发出去，而三个端点都写着 `max_length=500`——
            // 于是每一次都是 422，而 422 不是 `.offline`，抛出去整趟同步就断。
            // **队列从此只会变长**，而每次回到前台都重试一遍几秒的请求，
            // 表现就是「一直转圈而且非常卡」。
            //
            // 这里不是「把上限调大」:一个没有上限的批总有一天会撞上某个上限
            // （请求体大小、网关超时、内存）。分片是那个上限存在时唯一正确的形状。
            for slice in stride(from: 0, to: queued.count, by: Self.uploadBatchSize) {
                let batch = Array(queued[slice..<min(slice + Self.uploadBatchSize,
                                                     queued.count)])
                do {
                    let verdicts = try await post(kind, batch.map(\.1))
                    for (event, envelope) in batch {
                        switch verdicts[envelope.idemKey] {
                        case .landed: report.landed += 1; landed.append(event.localSequence)
                        case .duplicate: report.duplicates += 1
                            landed.append(event.localSequence)
                        case .rejected:
                            // **也标记已上报——这是终局判断，不是「这次没连上」。**
                            // 三个批量端点的 `status: "failed"` 都是服务端主动认定
                            // 「这一条我不打算收」（见 `review/routes.py` 的
                            // `UnusableItem`/`item_key` 缺失那两条判断），不是
                            // 「暂时处理不了，回头再试」——它就是为「逐条拒绝」
                            // 这件事设计的，而逐条拒绝的另一半是「拒了就别再问」。
                            //
                            // 2026-09-19 真机上栽的:四条 P9 之前记的老作答
                            // （没有 item_key）永远被判 failed，而这里原本什么都不做
                            // ——于是它们永远待发、`pendingEvents` 永远非零，
                            // 而 `ReviewModel.load()` 那条「有待发才等」的分支因此
                            // 永远走阻塞路径:每次进复习页都白等一趟注定失败的同步，
                            // 表现就是「不卡死了，但还是转圈」。
                            report.rejected += 1
                            landed.append(event.localSequence)
                        case nil:
                            // 服务端没提这一条。**沉默不等于同意**——留着下次再发。
                            break
                        }
                    }
                } catch let error as TransportError {
                    guard case .offline = error else { throw error }
                    report.offline = true
                    offline = true
                    break
                }
            }
            if offline { break }
        }
        if !landed.isEmpty { try events.markReported(landed) }
        return report
    }

    private func post(_ kind: OutboxEntry.Kind, _ batch: [OutboxEntry])
        async throws -> [String: OutboxVerdict] {
        let path: String
        let payload: Data
        switch kind {
        case .reading:
            path = "/v1/client/events"
            payload = try JSONEncoder.contract.encode(
                ["events": batch.map { EventEnvelope($0) }])
        case .answer:
            path = "/v1/client/reviews/answers"
            payload = try JSONEncoder.contract.encode(
                ["answers": batch.map { FlatEnvelope($0) }])
        case .spelling:
            path = "/v1/client/reviews/spellings"
            payload = try JSONEncoder.contract.encode(
                ["spellings": batch.map { FlatEnvelope($0) }])
        }

        let response = try await transport.send(
            HTTPRequest(method: .post, path: path, body: payload))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        return try Self.verdicts(from: response.body)
    }

    /// Read the server's per-item answers.
    ///
    /// **The verdict is per key, never per batch.** An entry the server did not
    /// mention gets no verdict and therefore stays — silence is not consent, and
    /// deleting on the strength of a 200 is how a partly-applied batch loses the
    /// half that failed.
    static func verdicts(from body: Data) throws -> [String: OutboxVerdict] {
        struct Result: Decodable {
            let idem_key: String
            let status: String
            let reason: String?
        }
        struct Envelope: Decodable { let results: [Result] }

        guard let envelope = try? JSONDecoder().decode(Envelope.self, from: body) else {
            throw TransportError.malformed("上报的回应里没有逐条结果")
        }
        var verdicts: [String: OutboxVerdict] = [:]
        for result in envelope.results {
            switch result.status {
            case "accepted": verdicts[result.idem_key] = .landed
            case "duplicate": verdicts[result.idem_key] = .duplicate
            default: verdicts[result.idem_key] = .rejected(result.reason ?? result.status)
            }
        }
        return verdicts
    }
}

// MARK: - Wire shapes

/// `/events` nests the body under `payload`; the review endpoints take the
/// fields flat. Two envelopes rather than one guessed compromise.
private struct EventEnvelope: Encodable {
    let idem_key: String
    let type: String
    let payload: [String: JSONValue]
    let occurred_at: String

    init(_ entry: OutboxEntry) {
        idem_key = entry.idemKey
        type = entry.eventType
        payload = entry.payload
        occurred_at = entry.occurredAt
    }
}

private struct FlatEnvelope: Encodable {
    let idem_key: String
    let occurred_at: String
    let fields: [String: JSONValue]

    init(_ entry: OutboxEntry) {
        idem_key = entry.idemKey
        occurred_at = entry.occurredAt
        fields = entry.payload
    }

    func encode(to encoder: any Encoder) throws {
        var container = encoder.container(keyedBy: DynamicKey.self)
        try container.encode(idem_key, forKey: DynamicKey("idem_key"))
        try container.encode(occurred_at, forKey: DynamicKey("occurred_at"))
        for (key, value) in fields {
            try container.encode(value, forKey: DynamicKey(key))
        }
    }
}

private struct DynamicKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init(_ value: String) { stringValue = value }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { nil }
}
