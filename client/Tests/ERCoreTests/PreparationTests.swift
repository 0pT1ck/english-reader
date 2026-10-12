import Foundation
import Testing
import ERContract
@testable import ERCore

private actor PoolReportGate: Transport {
    private var requestWaiter: CheckedContinuation<Void, Never>?
    private var releaseWaiter: CheckedContinuation<Void, Never>?
    private var received = false
    private var gated = false

    func send(_ request: HTTPRequest) async throws -> HTTPResponse {
        if request.path == "/v1/client/progress/pool", !gated {
            gated = true
            received = true
            requestWaiter?.resume()
            requestWaiter = nil
            await withCheckedContinuation { releaseWaiter = $0 }
        }
        return HTTPResponse(status: 200, body: Data("{}".utf8))
    }

    func waitForRequest() async {
        if received { return }
        await withCheckedContinuation { requestWaiter = $0 }
    }

    func release() {
        releaseWaiter?.resume()
        releaseWaiter = nil
    }
}

struct PreparationTests {
    private struct Harness: Sendable {
        let root = SyncTests.temporaryRoot()
        let events: EventLog
        let sentences: DayCache
        let engine: SyncEngine

        init(_ transport: any Transport) throws {
            events = try EventLog(directory: root.appendingPathComponent("events"))
            sentences = try DayCache(directory: root.appendingPathComponent("sentences"))
            engine = SyncEngine(
                transport: transport,
                outbox: try Outbox(directory: root.appendingPathComponent("outbox")),
                events: events,
                articles: try ArticleCache(directory: root.appendingPathComponent("articles")),
                day: try DayCache(directory: root.appendingPathComponent("day")),
                sentences: sentences)
        }

        func cleanUp() { try? FileManager.default.removeItem(at: root) }
    }

    private static func mark(_ key: String) -> [String: JSONValue] {
        ["item_key": .string(key), "sense_id": .int(1), "kind": .string("unknown")]
    }

    private static func projection(_ events: EventLog) throws -> Projection {
        Projection.replay(try events.load(), weightDecay: 0.5,
                          settings: ProjectionTests.settings)
    }

    private static func bilingual(_ id: Int = 1) -> Components.Schemas.SentenceCard {
        var card = SentencePoolTests.card(id, source: "generated")
        card.text_zh = "这是一条关于市政的句子。"
        return card
    }

    private static func item(_ key: String = "municipal", state: String? = "ready",
                             cards: [Components.Schemas.SentenceCard])
        -> Components.Schemas.StudyItemSentences {
        var item = Components.Schemas.StudyItemSentences(
            item_type: "word", item_key: key, sense_id: 1, sentences: cards)
        item.preparation_state = state
        return item
    }

    private static func pool(_ items: [Components.Schemas.StudyItemSentences])
        -> Components.Schemas.SentencePoolResponse {
        .init(learner: .init(id: 1, name: "test"), reported_at: "2026-01-05T08:00:00Z",
              items: items)
    }

    @Test("事件已送达、快照失败后，零待发和重启不能吞掉补报")
    func aFailedSnapshotSurvivesEventAcknowledgementAndRestart() async throws {
        let transport = ScriptedTransport([
            ("/v1/client/events", SyncTests.ok(SyncTests.results([("mark", "accepted")]))),
            ("/v1/client/progress/pool", .failure(.server(status: 503, body: "busy"))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        try harness.events.append(kind: .marked, payload: Self.mark("municipal"), idemKey: "mark")
        let delivered = try await harness.engine.drain()
        #expect(delivered.landed == 1)
        #expect(try harness.events.unreported(kinds: LoggedEvent.sendableKinds).isEmpty)
        let snapshot = PoolSnapshot(projection: try Self.projection(harness.events))
        await #expect(throws: TransportError.server(status: 503, body: "busy")) {
            _ = try await harness.engine.reportPoolIfNeeded(snapshot, reportedAt: "first")
        }
        #expect(await harness.engine.poolNeedsReport(snapshot))

        let reopened = try EventLog(directory: harness.root.appendingPathComponent("events"))
        #expect(reopened.reportedPool() == nil)
        let online = ScriptedTransport([("/v1/client/progress/pool", SyncTests.ok(Data("{}".utf8)))])
        let resumed = SyncEngine(transport: online,
                                 outbox: try Outbox(directory: harness.root.appendingPathComponent("outbox")),
                                 events: reopened,
                                 articles: try ArticleCache(directory: harness.root.appendingPathComponent("articles")),
                                 day: try DayCache(directory: harness.root.appendingPathComponent("day")))
        #expect(try await resumed.reportPoolIfNeeded(snapshot, reportedAt: "resumed"))
        #expect(reopened.reportedPool() == snapshot)
        #expect(try await resumed.reportPoolIfNeeded(snapshot, reportedAt: "unchanged") == false)
        #expect(await online.requests().count == 1)
    }

    @Test("在途新标记不能被旧快照成功清掉；改标记仍有独立版本")
    func anOldSuccessCannotAcknowledgeNewMarks() async throws {
        let transport = PoolReportGate()
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        try harness.events.append(kind: .marked, payload: Self.mark("first"))
        let old = PoolSnapshot(projection: try Self.projection(harness.events))
        let reporting = Task { try await harness.engine.reportPool(old, reportedAt: "old") }
        await transport.waitForRequest()
        for key in ["second", "third", "fourth"] {
            try harness.events.append(kind: .marked, payload: Self.mark(key))
        }
        let latest = PoolSnapshot(projection: try Self.projection(harness.events))
        await transport.release()
        _ = try await reporting.value
        #expect(harness.events.reportedPool() == old)
        #expect(await harness.engine.poolNeedsReport(latest))
        #expect(try await harness.engine.reportPoolIfNeeded(latest, reportedAt: "latest"))
        #expect(harness.events.reportedPool()?.entries.count == 4)

        var amended = Self.mark("first")
        amended["kind"] = .string("fuzzy")
        try harness.events.append(kind: .marked, payload: amended)
        let amendment = PoolSnapshot(projection: try Self.projection(harness.events))
        #expect(amendment.entries == latest.entries)
        #expect(amendment.throughLocalSequence != latest.throughLocalSequence)
        #expect(await harness.engine.poolNeedsReport(amendment))
        try harness.events.append(kind: .unmarked,
                                  payload: ["item_key": .string("second"), "sense_id": .int(1)])
        let removal = PoolSnapshot(projection: try Self.projection(harness.events))
        #expect(removal.entries.count == 3)
        #expect(removal.entries.contains { $0.key == "second" } == false)
        #expect(await harness.engine.poolNeedsReport(removal))
    }

    @Test("离线标记重启后由日志重新得到待报快照")
    func offlineMarksRemainReconstructibleAfterRestart() async throws {
        let transport = ScriptedTransport([("/v1/client/events", .failure(.offline("train")))])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        try harness.events.append(kind: .marked, payload: Self.mark("municipal"))
        let result = try await harness.engine.drain()
        #expect(result.offline)
        let reopened = try EventLog(directory: harness.root.appendingPathComponent("events"))
        let snapshot = PoolSnapshot(projection: try Self.projection(reopened))
        #expect(snapshot.entries.map(\.key) == ["municipal"])
        #expect(reopened.reportedPool() == nil)
        #expect(try reopened.unreported(kinds: LoggedEvent.sendableKinds).count == 1)
    }

    @Test("空句子、仅英文以及待备状态不能进队列")
    func incompleteContentWaitsRatherThanPretendingToBeDone() {
        let projection = ProjectionTests.replay([
            (.marked, Self.mark("municipal"), "2026-01-05T08:00:00+00:00"),
        ])
        let english = SentencePoolTests.card(1, source: "generated")
        for item in [Self.item(cards: []), Self.item(cards: [english]),
                     Self.item(state: "pending", cards: [Self.bilingual()]),
                     Self.item(state: "running", cards: [Self.bilingual()])] {
            let day = ReviewDay.assemble(pool: Self.pool([item]), projection: projection,
                                         day: "2026-01-05", now: ProjectionTests.now)
            #expect(day.entries.isEmpty)
            #expect(day.awaitingContent == 1)
            #expect(!day.allDone && !day.spellingAvailable)
        }
        let ready = ReviewDay.assemble(pool: Self.pool([Self.item(cards: [english, Self.bilingual(2)])]),
                                       projection: projection, day: "2026-01-05", now: ProjectionTests.now)
        #expect(ready.entries.count == 1)
        #expect(ready.awaitingContent == 0)
        #expect(ready.entries.first?.item.questions.map(\.id) == [2])
    }

    @Test("旧缓存须实际双语可问，failed保留具体内容错误")
    func legacyCacheAndFactoryFailureAreDifferentStates() {
        let projection = ProjectionTests.replay([
            (.marked, Self.mark("municipal"), "2026-01-05T08:00:00+00:00"),
        ])
        #expect(SentencePreparation.isReady(Self.item(state: nil, cards: [Self.bilingual()])))
        #expect(!SentencePreparation.isReady(Self.item(state: nil,
                            cards: [SentencePoolTests.card(1, source: "generated")])))
        var untranslated = Self.bilingual()
        untranslated.text_zh = "This response was never translated."
        #expect(!SentencePreparation.isReady(Self.item(state: nil, cards: [untranslated])))
        var failed = Self.item(state: "failed", cards: [Self.bilingual()])
        failed.preparation_error = "翻译未完成，请稍后重试"
        let day = ReviewDay.assemble(pool: Self.pool([failed]), projection: projection,
                                     day: "2026-01-05", now: ProjectionTests.now)
        #expect(day.entries.isEmpty)
        #expect(day.failedContent.values.first == "翻译未完成，请稍后重试")
        #expect(day.awaitingContent == 1)
    }

    @Test("已答完的可用卡加一张等内容的卡，不解锁完成或拼写")
    func awaitingContentBlocksCompletionAndSpelling() {
        let projection = ProjectionTests.replay([
            (.marked, Self.mark("municipal"), "2026-01-05T08:00:00+00:00"),
            (.marked, Self.mark("waiting"), "2026-01-05T08:01:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "sense_id": .int(1), "passed": .bool(true)],
             "2026-01-05T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "sense_id": .int(1), "passed": .bool(true)],
             "2026-01-05T09:01:00+00:00"),
        ])
        let day = ReviewDay.assemble(pool: Self.pool([Self.item(cards: [Self.bilingual()])]),
                                     projection: projection, day: "2026-01-05", now: ProjectionTests.now)
        #expect(day.done(.today) == 1)
        #expect(day.awaitingContent == 1)
        #expect(!day.allDone && !day.spellingAvailable)
    }

    @Test("严格刷新不能把离线缓存当作新内容，已有缓存仍保留")
    func strictRefreshReportsNetworkFailureWithoutDiscardingCache() async throws {
        let transport = ScriptedTransport([("/v1/client/sentences", .failure(.offline("train")))])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        let cached = Self.pool([Self.item(cards: [Self.bilingual()])])
        try harness.sentences.store(JSONEncoder().encode(cached))
        #expect(try await harness.engine.fetchSentences() == cached)
        await #expect(throws: TransportError.offline("train")) {
            _ = try await harness.engine.fetchSentences(allowCachedFallback: false)
        }
        #expect(await harness.engine.cachedSentences() == cached)
    }

    @Test("日常同内容响应不无谓重写现有句子缓存")
    func unchangedContentPreservesTheCachedBytes() async throws {
        let pool = Self.pool([Self.item(cards: [Self.bilingual()])])
        let bytes = try JSONEncoder().encode(pool)
        var object = try #require(JSONSerialization.jsonObject(with: bytes) as? [String: Any])
        object["future_field"] = "keep the raw offline shape"
        let original = try JSONSerialization.data(withJSONObject: object)
        let transport = ScriptedTransport([("/v1/client/sentences", SyncTests.ok(bytes))])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        try harness.sentences.store(original)
        #expect(await harness.engine.cachedSentences() == pool)
        #expect(try await harness.engine.fetchSentences(allowCachedFallback: false) == pool)
        #expect(harness.sentences.load() == original)
    }
}

extension PreparationTests {
    @Test("拉取前页落盘后后页失败，序号缺口必须先重放而不能增量跨过")
    func partialPullFailureRequiresReconciliationBeforeLocalAppend() async throws {
        let firstPage = Data("""
        {"events":[{"sequence":1,"idem_key":"remote-mark","type":"word.marked",
          "payload":{"item_key":"remote","sense_id":1,"kind":"unknown"},
          "occurred_at":"2026-01-05T08:00:00Z"}],"through":1,"more":true}
        """.utf8)
        let transport = ScriptedTransport([
            ("/v1/client/events?after=0", SyncTests.ok(firstPage)),
            ("/v1/client/events?after=1", .failure(.offline("page two"))),
        ])
        let harness = try Harness(transport)
        defer { harness.cleanUp() }
        var projection = Projection()
        await #expect(throws: TransportError.offline("page two")) {
            _ = try await harness.engine.pull()
        }
        #expect(harness.events.latestLocalSequence == 0)
        #expect(harness.events.cursor().pulledThrough == 1)
        #expect(projection.throughLocalSequence != harness.events.latestLocalSequence)

        let local = try harness.events.append(kind: .marked, payload: Self.mark("local"))
        let acceptedAcrossGap = projection.applyNext(
            local, weightDecay: 0.5, settings: ProjectionTests.settings)
        #expect(!acceptedAcrossGap)
        #expect(projection.throughLocalSequence == -1)
        #expect(projection.items.isEmpty)
        projection = try Self.projection(harness.events)
        #expect(Set(projection.items.keys.map(\.key)) == ["remote", "local"])
        #expect(projection.throughLocalSequence == harness.events.latestLocalSequence)

        let next = try harness.events.append(kind: .marked, payload: Self.mark("next"))
        let acceptedNext = projection.applyNext(
            next, weightDecay: 0.5, settings: ProjectionTests.settings)
        #expect(acceptedNext)
        #expect(projection == (try Self.projection(harness.events)))
    }

    @Test("冻结due题不会被内容刷新换掉，但远端推进或跨日后不能提交旧答案")
    func frozenQuestionValidityTracksTheRealRoundNotContentRefresh() throws {
        let root = SyncTests.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let events = try EventLog(directory: root.appendingPathComponent("events"))
        try events.append(kind: .marked, payload: Self.mark("municipal"),
                          occurredAt: "2026-01-01T08:00:00Z")
        let projection = try Self.projection(events)
        let content = Self.pool([Self.item(cards: [Self.bilingual()])])
        let today = ReviewDay.assemble(pool: content, projection: projection,
                                      day: "2026-01-05", now: ProjectionTests.now)
        let frozen = try #require(today.entries.first)
        #expect(frozen.bucket == .due)
        #expect(frozen.acceptsAnswer(in: projection, questionDay: "2026-01-05",
                                    day: "2026-01-05", now: ProjectionTests.now))

        try events.append(kind: .marked, payload: Self.mark("another"),
                          occurredAt: "2026-01-05T08:00:00Z")
        let unrelated = try Self.projection(events)
        #expect(frozen.acceptsAnswer(in: unrelated, questionDay: "2026-01-05",
                                    day: "2026-01-05", now: ProjectionTests.now))
        try events.append(kind: .answered,
                          payload: ["item_key": .string("municipal"), "sense_id": .int(1),
                                    "passed": .bool(true)],
                          occurredAt: "2026-01-05T08:01:00Z")
        let advanced = try Self.projection(events)
        #expect(!frozen.acceptsAnswer(in: advanced, questionDay: "2026-01-05",
                                     day: "2026-01-05", now: ProjectionTests.now))
        let latest = ReviewDay.assemble(pool: content, projection: advanced,
                                       day: "2026-01-05", now: ProjectionTests.now)
        let fresh = try #require(latest.entries.first)
        #expect(fresh.bucket == .due && fresh.state.direction == .senseToWord)
        #expect(frozen.state.direction == .wordToSense)
        #expect(advanced.items[frozen.key]?.memory == nil)
        #expect(fresh.acceptsAnswer(in: advanced, questionDay: "2026-01-05",
                                   day: "2026-01-05", now: ProjectionTests.now))
        #expect(!frozen.acceptsAnswer(in: projection, questionDay: "2026-01-05",
                                     day: "2026-01-06",
                                     now: ProjectionTests.now.addingTimeInterval(86_400)))
    }

    @Test("检查之后远端再落盘，原子序号守卫不让旧答案完成第二向")
    func anInterveningFactAtomicallyRejectsTheOldAnswer() throws {
        let root = SyncTests.temporaryRoot()
        defer { try? FileManager.default.removeItem(at: root) }
        let events = try EventLog(directory: root.appendingPathComponent("events"))
        try events.append(kind: .marked, payload: Self.mark("municipal"),
                          occurredAt: "2026-01-01T08:00:00Z")
        let expected = events.latestLocalSequence
        let payload: [String: JSONValue] = [
            "item_key": .string("municipal"), "sense_id": .int(1), "passed": .bool(true),
        ]
        try events.appendPulled(kind: .answered, payload: payload,
                                idemKey: "remote-answer", occurredAt: "2026-01-05T08:00:00Z")
        #expect(throws: EventLogError.sequenceChanged(expected: expected, actual: expected + 1)) {
            try events.append(kind: .answered, payload: payload, idemKey: "stale-answer",
                              occurredAt: "2026-01-05T08:01:00Z",
                              expectedLocalSequence: expected)
        }
        let before = try Self.projection(events)
        let key = Projection.Key(key: "municipal", senseId: 1)
        #expect(before.rounds[key]?.state.direction == .senseToWord)
        #expect(before.rounds[key]?.state.asks == 1)
        #expect(before.items[key]?.memory == nil)
        #expect(try events.load().events.contains { $0.idemKey == "stale-answer" } == false)

        let at = ProjectionTests.now
        let answer = OutboxEntry.answered(queueId: 0, passed: true, itemType: "word",
                                          itemKey: "municipal", senseId: 1, occurredAt: at)
        try events.append(kind: .answered, payload: answer.payload,
                          idemKey: answer.idemKey, occurredAt: answer.occurredAt,
                          expectedLocalSequence: events.latestLocalSequence)
        let after = try Self.projection(events)
        #expect(after.rounds[key]?.state.done == true)
        #expect(after.rounds[key]?.state.asks == 2)
        #expect(after.items[key]?.memory?.reps == 1)
    }
}
