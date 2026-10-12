import Foundation
import Testing
import ERContract
@testable import ERCore

/// Syncing, against a transport that answers from a script.
///
/// The point of these is the unhappy paths. A client whose only tested path is
/// "the server answered 200" is a client that has never been on a train.
actor ScriptedTransport: Transport {
    /// path prefix → what to answer. `nil` means "no network".
    private var script: [(match: String, answer: Result<HTTPResponse, TransportError>)]
    private(set) var sent: [HTTPRequest] = []

    init(_ script: [(String, Result<HTTPResponse, TransportError>)]) {
        self.script = script.map { (match: $0.0, answer: $0.1) }
    }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        sent.append(request)
        for entry in script where request.path.hasPrefix(entry.match) {
            switch entry.answer {
            case .success(let response): return response
            case .failure(let error): throw error
            }
        }
        throw TransportError.server(status: 404, body: "脚本里没有 \(request.path)")
    }

    func requests() -> [HTTPRequest] { sent }
}

struct SyncTests {
    static func temporaryRoot() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("sync-test-\(UUID().uuidString)")
    }

    static func fixture(_ name: String) throws -> Data {
        let url = Bundle.module.url(forResource: "Fixtures/\(name)", withExtension: "json")!
        return try Data(contentsOf: url)
    }

    struct Harness {
        let root: URL
        let outbox: Outbox
        let articles: ArticleCache
        let day: DayCache
        let engine: SyncEngine

        init(_ transport: any Transport) throws {
            root = SyncTests.temporaryRoot()
            outbox = try Outbox(directory: root.appendingPathComponent("outbox"))
            articles = try ArticleCache(directory: root.appendingPathComponent("articles"))
            day = try DayCache(directory: root.appendingPathComponent("day"))
            engine = SyncEngine(transport: transport, outbox: outbox,
                                articles: articles, day: day)
        }
        func cleanUp() { try? FileManager.default.removeItem(at: root) }
    }

    static func ok(_ data: Data) -> Result<HTTPResponse, TransportError> {
        .success(HTTPResponse(status: 200, body: data))
    }

    static func results(_ pairs: [(String, String)]) -> Data {
        let items = pairs.map { "{\"idem_key\":\"\($0.0)\",\"status\":\"\($0.1)\"}" }
        return Data("""
        {"accepted":0,"duplicates":0,"failed":0,"results":[\(items.joined(separator: ","))]}
        """.utf8)
    }

    // MARK: Fetching

    @Test func fetchingTheDayCachesItWhole() async throws {
        let transport = ScriptedTransport([("/v1/client/today", Self.ok(try Self.fixture("today")))])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        let package = try await harness.engine.fetchDay()
        #expect(package.articles.count == 3)
        #expect(package.excludesExamPapers, "今日包不含真题，这件事写在响应里")
        #expect(harness.day.hasPackage)
        #expect(harness.articles.articles().count == 3, "元信息记下来了")
        let bodies = harness.articles.bodySizes()
        #expect(bodies.count == 3, "每篇正文单独缓存一份——点开一篇是在今日包过期很久之后")
    }

    /// The morning after, on a train. This is the case the whole design exists
    /// for, so it gets a test of its own rather than being implied.
    @Test func offlineFallsBackToYesterdaysPackage() async throws {
        let online = ScriptedTransport([("/v1/client/today", Self.ok(try Self.fixture("today")))])
        let harness = try Harness(online)
        defer { harness.cleanUp() }
        _ = try await harness.engine.fetchDay()

        let offline = ScriptedTransport([("/v1/client/today", .failure(.offline("没有网络")))])
        let sameCaches = SyncEngine(transport: offline, outbox: harness.outbox,
                                    articles: harness.articles, day: harness.day)
        let package = try await sameCaches.fetchDay()
        #expect(package.articles.count == 3, "缓存的那一份照样能把一天走完")
    }

    @Test func offlineWithNothingCachedSaysSoRatherThanPretending() async throws {
        let transport = ScriptedTransport([("/v1/client/today", .failure(.offline("没有网络")))])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        await #expect(throws: TransportError.offline("没有网络")) {
            _ = try await harness.engine.fetchDay()
        }
    }

    @Test func aClearedArticleIsOneRequestAway() async throws {
        let day = try Self.fixture("today")
        let article = try JSONDecoder().decode(
            Components.Schemas.TodayResponse.self, from: day).articles[0]
        let articleData = try JSONEncoder().encode(article)
        let id = article.article.id

        let transport = ScriptedTransport([
            ("/v1/client/today", Self.ok(day)),
            ("/v1/client/articles/", Self.ok(articleData)),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        _ = try await harness.engine.fetchDay()
        try harness.articles.clearAllBodies()
        #expect(harness.articles.bodySizes().isEmpty)

        let refetched = try await harness.engine.article(id)
        #expect(refetched.article.id == id)
        #expect(harness.articles.bodySizes()[id] != nil, "重新拉回来之后又缓存上了")
    }

    // MARK: Draining

    @Test func drainingDeletesOnlyWhatLanded() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/events", Self.ok(Self.results([
                ("a", "accepted"), ("b", "duplicate"), ("c", "failed"),
            ]))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        for key in ["a", "b", "c", "d"] {
            try harness.outbox.append(
                OutboxEntry(idemKey: key, kind: .reading, eventType: "word.tapped",
                            payload: ["headword": "probe"]))
        }

        let report = try await harness.engine.drain()
        #expect(report.landed == 1)
        #expect(report.duplicates == 1)
        #expect(report.rejected == 1)
        // `d` was in the batch but the server said nothing about it. Silence is
        // not consent — it stays.
        #expect(report.remaining == 2)
        let left = try harness.outbox.pending().entries.map(\.idemKey).sorted()
        #expect(left == ["c", "d"])
    }

    @Test func goingOfflineMidDrainKeepsEverythingLeft() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/events", Self.ok(Self.results([("read", "accepted")]))),
            ("/v1/client/reviews/answers", .failure(.offline("信号没了"))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        try harness.outbox.append(OutboxEntry(idemKey: "read", kind: .reading,
                                              eventType: "word.tapped", payload: [:]))
        try harness.outbox.append(OutboxEntry(idemKey: "answer", kind: .answer,
                                              payload: ["queue_id": .int(1)]))

        let report = try await harness.engine.drain()
        #expect(report.offline, "断线不是错误，是这台设备的常态")
        #expect(report.landed == 1)
        #expect(try harness.outbox.pending().entries.map(\.idemKey) == ["answer"],
                "发出去的删掉，没发的留着，下次接着发")
    }

    @Test func answersAreSentInTheOrderTheyWereMade() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/reviews/answers", Self.ok(Self.results(
                (0..<6).map { ("ans-\($0)", "accepted") }))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        for index in 0..<6 {
            try harness.outbox.append(
                OutboxEntry(idemKey: "ans-\(index)", kind: .answer,
                            payload: ["queue_id": .int(index)]))
        }
        _ = try await harness.engine.drain()

        let body = await transport.requests()
            .first { $0.path.contains("answers") }?.body
        let text = String(decoding: body ?? Data(), as: UTF8.self)
        // 复习是个状态机——答对第一向解锁第二向。顺序反了，服务端算出来的
        // 那一天就跟设备上看到的不是同一天。
        let positions = (0..<6).map { text.range(of: "ans-\($0)")?.lowerBound }
        #expect(positions.allSatisfy { $0 != nil })
        #expect(positions.compactMap { $0 } == positions.compactMap { $0 }.sorted())
    }

    @Test func eachKindGoesToItsOwnEndpoint() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/events", Self.ok(Self.results([("r", "accepted")]))),
            ("/v1/client/reviews/answers", Self.ok(Self.results([("a", "accepted")]))),
            ("/v1/client/reviews/spellings", Self.ok(Self.results([("s", "accepted")]))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        try harness.outbox.append(OutboxEntry(idemKey: "r", kind: .reading,
                                              eventType: "word.tapped", payload: [:]))
        try harness.outbox.append(OutboxEntry(idemKey: "a", kind: .answer, payload: [:]))
        try harness.outbox.append(OutboxEntry(idemKey: "s", kind: .spelling, payload: [:]))

        let report = try await harness.engine.drain()
        #expect(report.landed == 3)
        #expect(harness.outbox.count == 0)

        let paths = await transport.requests().map(\.path)
        #expect(paths.contains("/v1/client/events"))
        #expect(paths.contains("/v1/client/reviews/answers"))
        #expect(paths.contains("/v1/client/reviews/spellings"),
                "拼写也有自己的批量端点了——P4 只给了作答，这次补上")
    }

    /// 阳性对照 (坑 §4.3): the per-key rule only means something if the
    /// careless reading of the same response would behave differently.
    @Test func aBodyWithNoPerItemResultsIsRefused() throws {
        let ambiguous = Data("{\"accepted\":3,\"duplicates\":0,\"failed\":0}".utf8)
        #expect(throws: TransportError.self) {
            _ = try SyncEngine.verdicts(from: ambiguous)
        }
        // 整批看起来是成功的——正因为如此，才会有人想整批删。
    }

    @Test func readingEventsCarryTheirTypeOnTheWire() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/events", Self.ok(Self.results([("m", "accepted")]))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }

        var entry = OutboxEntry.marked("account", senseId: 42, kind: .unknown,
                                       articleId: 463)
        entry = OutboxEntry(idemKey: "m", kind: entry.kind, eventType: entry.eventType,
                            payload: entry.payload)
        try harness.outbox.append(entry)
        _ = try await harness.engine.drain()

        let body = await transport.requests().first { $0.path.contains("events") }?.body
        let text = String(decoding: body ?? Data(), as: UTF8.self)
        #expect(text.contains("word.marked"))
        #expect(text.contains("\"headword\":\"account\""))
        #expect(text.contains("\"sense_id\":42"))
    }
}

// MARK: - 分片与并发（2026-09-18 真机上栽的两条）

/// 一个照收照答的服务端：对每个 `idem_key` 都回 `landed`，
/// **但拒收超过 `limit` 条的一批**——真的服务端就是这么做的（`max_length=500`）。
actor CountingTransport: Transport {
    let limit: Int
    private(set) var batchSizes: [Int] = []
    private(set) var rejected = 0

    init(limit: Int) { self.limit = limit }

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        struct Envelope: Decodable { let idem_key: String }
        struct Body: Decodable {
            let events: [Envelope]?
            let answers: [Envelope]?
            let spellings: [Envelope]?
            var all: [Envelope] { events ?? answers ?? spellings ?? [] }
        }
        guard let data = request.body,
              let body = try? JSONDecoder().decode(Body.self, from: data) else {
            return HTTPResponse(status: 200, body: Data("{\"results\":[]}".utf8))
        }
        batchSizes.append(body.all.count)
        if body.all.count > limit {
            rejected += 1
            // 真服务端回的正是 422，而 422 不是「离线」。
            return HTTPResponse(status: 422, body: Data("{\"detail\":\"too_long\"}".utf8))
        }
        // **服务端说的是 `accepted`，不是 `landed`。** 第一版这个假服务端写的是
        // `landed`，于是每一条都被解析成「被拒」——而当时那条测试只断言了
        // `landed == 534`，分不出「没发出去」和「发出去被拒了」。
        // 所以下面补了 `rejected == 0`:一个分不出两种情况的断言等于没有断言（坑 §6.7）。
        let items = body.all.map { "{\"idem_key\":\"\($0.idem_key)\",\"status\":\"accepted\"}" }
        return HTTPResponse(status: 200,
                            body: Data("{\"results\":[\(items.joined(separator: ","))]}".utf8))
    }

    func sizes() -> [Int] { batchSizes }
    func rejectedCount() -> Int { rejected }
}

extension SyncTests {
    /// **这一条是那次「一直转圈而且非常卡」的回归测试。**
    ///
    /// 真机上攒到 534 条待发，而三个端点都写着 `max_length=500`——
    /// 整批发出去每次都是 422，422 不是 `.offline` 所以抛出去整趟同步就断，
    /// 队列从此只会变长，而每次回到前台都重试一遍几秒的请求。
    @Test("待发比服务端上限还多时，分片发完，而不是整批被拒")
    func uploadsInSlicesBelowTheServerLimit() async throws {
        let transport = CountingTransport(limit: 500)
        let root = SyncTests.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }

        let events = try EventLog(directory: root.appendingPathComponent("events"))
        for i in 0..<534 {
            try events.append(kind: .marked,
                              payload: ["item_key": .string("w\(i)"), "sense_id": .int(1)],
                              idemKey: "mark-\(i)")
        }
        let engine = SyncEngine(
            transport: transport,
            outbox: try Outbox(directory: root.appendingPathComponent("outbox")),
            events: events,
            articles: try ArticleCache(directory: root.appendingPathComponent("articles")),
            day: try DayCache(directory: root.appendingPathComponent("day")),
            sentences: try DayCache(directory: root.appendingPathComponent("sentences")))

        let report = try await engine.drain()

        #expect(await transport.rejectedCount() == 0,
                "一片都不许超过上限——超了就是那次 422 又回来了")
        let sizes = await transport.sizes()
        #expect(sizes.allSatisfy { $0 <= 500 }, "实际发出去的片：\(sizes)")
        #expect(sizes.count >= 3, "534 条按 200 一片，至少三片，实际 \(sizes.count) 片")
        #expect(report.landed == 534, "全都要送到，不是只送头一片")
        #expect(report.rejected == 0, "一条都不该被拒——被拒和没发出去是两件事")
        #expect(try events.unreported(kinds: LoggedEvent.sendableKinds).isEmpty,
                "送到了就该记下来，否则下一趟又从头发一遍")
    }

    /// **这一条是「不卡死了，但还是转圈」的回归测试**（2026-09-19 真机上栽的）。
    ///
    /// 服务端的 `status: "failed"` 是终局判断，不是「这次没连上」——见
    /// `review/routes.py` 的 `UnusableItem`：一条作答没有 `item_key`
    /// （P9 之前记的老事件）会被永久判 `failed`，重发一百次结果都一样。
    ///
    /// 真机上撞到的正是这个:四条 2026-09-13 记的老作答，`item_key` 是
    /// `nil`，永远被拒、永远待发，`pendingEvents` 因此永远非零——而
    /// `ReviewModel.load()` 那条「有待发才等」的分支就此永远走阻塞路径，
    /// 每次进复习页都白等一趟注定失败的同步。
    @Test("服务端判 failed 的那条不再永远待发——拒了就别再问")
    func aPermanentlyFailedAnswerStopsBeingRetried() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/reviews/answers", Self.ok(Self.results([
                ("ok-1", "accepted"), ("no-identity", "failed"),
            ]))),
        ])
        let root = SyncTests.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }

        let events = try EventLog(directory: root.appendingPathComponent("events"))
        try events.append(kind: .answered,
                          payload: ["queue_id": .int(1), "passed": .bool(true)],
                          idemKey: "ok-1")
        // 老事件的形状:只有 queue_id，没有身份字段。
        try events.append(kind: .answered,
                          payload: ["queue_id": .int(281), "passed": .bool(false)],
                          idemKey: "no-identity")

        let engine = SyncEngine(
            transport: transport,
            outbox: try Outbox(directory: root.appendingPathComponent("outbox")),
            events: events,
            articles: try ArticleCache(directory: root.appendingPathComponent("articles")),
            day: try DayCache(directory: root.appendingPathComponent("day")))

        let report = try await engine.drain()
        #expect(report.landed == 1)
        #expect(report.rejected == 1)
        #expect(try events.unreported(kinds: LoggedEvent.sendableKinds).isEmpty,
                "被拒的那条也该标记已上报——它不会因为再问一次就通过")

        // 再 drain 一次:没有待发的了，不该再打一次网络请求——
        // `ScriptedTransport` 只认得上面那一条脚本，再打一次也会拿到同样的回应，
        // 但这里要验证的是「根本没打」，这正是转圈那个症状消失的原因。
        let again = try await engine.drain()
        #expect(again.landed == 0 && again.duplicates == 0 && again.rejected == 0,
                "没有待发的了，drain 应该什么都不做")
    }
}
