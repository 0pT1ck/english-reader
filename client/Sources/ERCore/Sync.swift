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
    public var reviews: Components.Schemas.ReviewDayResponse { decoded.reviews }

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
    public var day: String? { decoded.day ?? decoded.reviews.day }

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
}

public actor SyncEngine {
    private let transport: any Transport
    private let outbox: Outbox
    private let articles: ArticleCache
    private let day: DayCache

    public init(transport: any Transport, outbox: Outbox,
                articles: ArticleCache, day: DayCache) {
        self.transport = transport
        self.outbox = outbox
        self.articles = articles
        self.day = day
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

    /// The check-in calendar and the streak.
    ///
    /// Its own endpoint rather than fields on the day package, because
    /// 跨 Phase 不变量 only allows obvious shapes to be reserved in place and a
    /// list of days is not one. No offline fallback: a calendar that silently
    /// shows stale days is worse than one that says it could not load.
    public func calendar(days: Int = 7)
        async throws -> Components.Schemas.CalendarResponse {
        let response = try await transport.send(
            HTTPRequest(method: .get, path: "/v1/client/reviews/calendar?days=\(days)"))
        guard response.isOK else {
            throw TransportError.server(
                status: response.status,
                body: String(decoding: response.body.prefix(400), as: UTF8.self))
        }
        return try JSONDecoder().decode(
            Components.Schemas.CalendarResponse.self, from: response.body)
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
        let pending = try outbox.pending()
        if !pending.damaged.isEmpty {
            // Left in place rather than discarded here: throwing away an event
            // is a loss, and it should be a decision the caller makes out loud.
            report.rejected += pending.damaged.count
        }
        guard !pending.entries.isEmpty else {
            report.remaining = outbox.count
            return report
        }

        for kind in [OutboxEntry.Kind.reading, .answer, .spelling] {
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
        report.remaining = outbox.count
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
