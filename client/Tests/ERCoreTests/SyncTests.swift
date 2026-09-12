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
