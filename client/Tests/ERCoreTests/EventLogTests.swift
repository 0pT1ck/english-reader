import Foundation
import Testing
@testable import ERCore

/// 日志是设备上唯一不可重算的那份东西（§5），所以这些测试全是关于**丢东西**的，
/// 不是关于存东西。
struct EventLogTests {
    static func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("eventlog-test-\(UUID().uuidString)")
    }

    @Test("写进去读得回来，顺序和序号都对")
    func roundTrips() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)

        for index in 0..<40 {
            try log.append(kind: .answered, payload: ["queue_id": .int(index)])
        }
        let loaded = try log.load()
        #expect(loaded.events.count == 40)
        #expect(loaded.damaged == 0)
        #expect(loaded.tornTail == false)
        #expect(loaded.events.map(\.localSequence) == Array(0..<40))
        #expect(loaded.events.allSatisfy { $0.kind == .answered })
    }

    @Test("重开之后序号接着往下走，不从 0 重来")
    func sequenceSurvivesReopening() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }

        let first = try EventLog(directory: directory)
        try first.append(kind: .marked, payload: ["item_key": .string("municipal")])
        try first.append(kind: .marked, payload: ["item_key": .string("rigorous")])

        // 换一个实例，等于 App 重启。序号从文件里算出来，不是记在内存里的。
        let second = try EventLog(directory: directory)
        let third = try second.append(kind: .spelled, payload: ["typed": .string("municiple")])
        #expect(third.localSequence == 2, "重开之后应当接着 2，实际 \(third.localSequence)")
        #expect(try second.load().events.count == 3)
    }

    /// **断尾是唯一的崩溃损伤**，而它按定义是「还没告诉用户已经保存」的那一条。
    /// 丢掉它是对的；悄悄丢掉不对，所以要报出来。
    @Test("最后一行写了一半：丢掉它，但要说出来，前面的全都还在")
    func tornTailIsDroppedAndReported() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)
        for index in 0..<5 {
            try log.append(kind: .read, payload: ["article_id": .int(index)])
        }

        // 模拟崩在写的一半：往文件末尾追一段不完整的 JSON，没有换行。
        let file = directory.appendingPathComponent("events.jsonl")
        let handle = try #require(FileHandle(forWritingAtPath: file.path))
        try handle.seekToEnd()
        try handle.write(contentsOf: Data(#"{"localSequence":5,"kind":"read","#.utf8))
        try handle.close()

        let loaded = try log.load()
        #expect(loaded.tornTail == true, "断尾要报出来")
        #expect(loaded.events.count == 5, "前五条一条都不许丢，实际 \(loaded.events.count)")
        #expect(loaded.damaged == 0, "断尾不算 damaged——那是两件事")
    }

    /// 同发件箱那条：一条读不出来不能让整份读不出来，
    /// 否则「丢了一条事件」就变成「丢了全部事件」。
    @Test("中间有一行坏了：跳过它、计数报出来，其余照读")
    func aDamagedLineDoesNotBlockTheRest() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("events.jsonl")

        let log = try EventLog(directory: directory)
        try log.append(kind: .marked, payload: ["item_key": .string("a")])
        // 手写三行：坏的夹在中间。
        var text = try #require(String(data: Data(contentsOf: file), encoding: .utf8))
        text += "{ 这不是 JSON\n"
        text += #"{"localSequence":2,"idemKey":"k2","kind":"marked","occurredAt":"2026-01-01T00:00:00Z","payload":{}}"# + "\n"
        try text.write(to: file, atomically: true, encoding: .utf8)

        let loaded = try log.load()
        #expect(loaded.damaged == 1, "坏行数应当是 1，实际 \(loaded.damaged)")
        #expect(loaded.events.count == 2, "好的两条都要读到，实际 \(loaded.events.count)")
        #expect(loaded.tornTail == false)
    }

    @Test("上报进度存在日志之外，日志本身从不回头改")
    func cursorLivesOutsideTheLog() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)
        for index in 0..<6 {
            try log.append(kind: .decided, payload: ["pick": .int(index)])
        }

        #expect(log.cursor().reportedThrough == -1, "还没报过应当是 -1")
        #expect(try log.unreported().count == 6)

        try log.setCursor(EventLog.Cursor(reportedThrough: 3, pulledThrough: 0))
        #expect(try log.unreported().map(\.localSequence) == [4, 5])

        // 日志的字节数不变——设游标没有回头改它。
        let file = directory.appendingPathComponent("events.jsonl")
        let before = try Data(contentsOf: file).count
        try log.setCursor(EventLog.Cursor(reportedThrough: 5, pulledThrough: 9))
        #expect(try Data(contentsOf: file).count == before, "设游标不许动日志")
        #expect(try log.unreported().isEmpty)

        // 游标也要活过重开。
        let reopened = try EventLog(directory: directory)
        #expect(reopened.cursor() == EventLog.Cursor(reportedThrough: 5, pulledThrough: 9))
    }

    @Test("五种事件，别的都算得回来——所以这个枚举就该只有这几个")
    func onlyFiveKindsOfFact() {
        // 这条是守卫，不是测试：往里加一个 case 就会红，然后你得去 §5 说明
        // 为什么它不可重算。marked／unmarked 是同一件事的两个方向。
        #expect(LoggedEvent.Kind.allCases.count == 6)
        #expect(Set(LoggedEvent.Kind.allCases.map(\.rawValue)) ==
                ["marked", "unmarked", "read", "answered", "spelled", "decided"])
    }
}
