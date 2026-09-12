import Foundation
import Testing
@testable import ERCore

/// The outbox holds the only data on the device that cannot be fetched again,
/// so these tests are about losing things, not about storing them.
struct OutboxTests {
    static func temporaryDirectory() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("outbox-test-\(UUID().uuidString)")
        return url
    }

    static func entry(_ key: String, _ kind: OutboxEntry.Kind = .reading,
                      _ payload: [String: JSONValue] = ["headword": "probe"]) -> OutboxEntry {
        OutboxEntry(idemKey: key, kind: kind, payload: payload)
    }

    @Test func keepsTheOrderItWasGiven() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try Outbox(directory: directory)

        // Review answers are a state machine — passing 看词想义 unlocks
        // 看义想词 — so the server replays them in the client's order. A queue
        // that reordered them would produce a different day.
        for index in 0..<25 {
            try outbox.append(Self.entry("key-\(index)", .answer, ["queue_id": .int(index)]))
        }
        let pending = try outbox.pending()
        #expect(pending.damaged.isEmpty)
        #expect(pending.entries.map(\.idemKey) == (0..<25).map { "key-\($0)" })
    }

    @Test func survivesReopening() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        try Outbox(directory: directory).append(Self.entry("persisted"))
        // A new instance is what a relaunch looks like.
        let reopened = try Outbox(directory: directory)
        #expect(try reopened.pending().entries.map(\.idemKey) == ["persisted"])
    }

    @Test func deletesOnlyWhatTheServerConfirmed() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try Outbox(directory: directory)

        try outbox.append(Self.entry("landed"))
        try outbox.append(Self.entry("already-applied"))
        try outbox.append(Self.entry("refused"))
        try outbox.append(Self.entry("never-mentioned"))

        // 决定 11: a batch can be partly accepted, so HTTP 200 is not permission
        // to clear the queue.
        let removed = try outbox.acknowledge([
            "landed": .landed,
            "already-applied": .duplicate,
            "refused": .rejected("未知的事件类型"),
        ])

        #expect(removed == 2)
        let left = try outbox.pending().entries.map(\.idemKey)
        #expect(left.sorted() == ["never-mentioned", "refused"],
                "被拒的和服务端没提到的都要留着——下次再试")
    }

    /// 阳性对照 (坑 §4.3): the rule above only means something if the careless
    /// version would fail. Clearing the queue on a 200 is the careless version.
    @Test func clearingEverythingOnASuccessfulBatchWouldLoseData() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try Outbox(directory: directory)
        try outbox.append(Self.entry("ok"))
        try outbox.append(Self.entry("failed-to-apply"))

        let verdicts: [String: OutboxVerdict] = [
            "ok": .landed,
            "failed-to-apply": .rejected("数据库出错"),
        ]
        try outbox.acknowledge(verdicts)
        #expect(outbox.count == 1, "只删确认过的那条")

        // What "HTTP 200 means delete everything" would have done:
        #expect(verdicts.count == 2, "整批看起来是成功的——所以才会有人想整批删")
    }

    @Test func aHalfWrittenFileIsSweptOnOpen() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

        // What a crash mid-write leaves behind. The learner was never told this
        // one was saved — the confirmation comes after the rename — so
        // discarding it loses nothing they believe they have.
        try Data("{ truncated".utf8).write(
            to: directory.appendingPathComponent("0000000000-halfway.json.writing"))
        try Data("{}".utf8).write(
            to: directory.appendingPathComponent("0000000001-stray.json.writing"))

        let outbox = try Outbox(directory: directory)
        #expect(outbox.count == 0)
        #expect(try outbox.pending().entries.isEmpty)
        #expect(try outbox.pending().damaged.isEmpty, "半截文件不该被当成损坏的记录")
    }

    @Test func aDamagedRecordDoesNotBlockTheRest() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try Outbox(directory: directory)
        try outbox.append(Self.entry("good-one"))
        // Corruption after the fact — a bad sector, a half-finished copy.
        try Data("not json at all".utf8).write(
            to: directory.appendingPathComponent("0000000009-broken.json"))
        try outbox.append(Self.entry("good-two"))

        let pending = try outbox.pending()
        #expect(pending.entries.map(\.idemKey) == ["good-one", "good-two"],
                "一条坏记录不能让整个队列发不出去——那会把丢一条变成丢全部")
        #expect(pending.damaged == ["0000000009-broken.json"])

        try outbox.discardDamaged(pending.damaged)
        #expect(try outbox.pending().damaged.isEmpty)
    }

    @Test func payloadsSurviveTheRoundTrip() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let outbox = try Outbox(directory: directory)

        let payload: [String: JSONValue] = [
            "headword": "account",
            "sense_id": .int(4821),
            "kind": "unknown",
            "article_id": .int(463),
            "percent": .double(62.5),
            "beyond": .bool(false),
            "note": .null,
            "spans": .array([.int(10), .int(17)]),
            "nested": .object(["item_type": "phrase"]),
        ]
        try outbox.append(Self.entry("round-trip", .reading, payload))
        let read = try outbox.pending().entries.first
        #expect(read?.payload == payload, "客户端不解释事件内容，只负责原样保管再原样交回")
    }

    @Test func timestampsUseTheSeparatorTheServerStores() throws {
        let entry = OutboxEntry(kind: .reading, payload: [:],
                                occurredAt: Date(timeIntervalSince1970: 1_789_000_000))
        #expect(entry.occurredAt.contains("T"),
                "ISO-8601 的 T 分隔符。后端为这个栽过：空格与 T 混用时，同一天的比较永远为假")
        #expect(!entry.occurredAt.contains(" "))
    }

    @Test func keysAreUniquePerEvent() {
        let first = OutboxEntry(kind: .reading, payload: [:])
        let second = OutboxEntry(kind: .reading, payload: [:])
        #expect(first.idemKey != second.idemKey,
                "幂等键由客户端生成——只有这台设备知道这是不是十分钟前没传上去的那一条")
    }
}
