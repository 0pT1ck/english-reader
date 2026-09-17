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

    // MARK: 上报水位线

    /// **一个水位线不够用。** 服务端的回应是逐条的，所以一批里可能第 5 条落了、
    /// 第 6 条被拒、第 7 条又落了。只有水位线的话，被拒的那一条会把后面全挡住。
    @Test("被拒的那一条只挡住自己，不挡它后面的")
    func aRejectedEventBlocksOnlyItself() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)
        for index in 0..<8 {
            try log.append(kind: .answered, payload: ["n": .int(index)])
        }

        // 0–4 落了，5 被拒，6、7 落了。
        try log.markReported([0, 1, 2, 3, 4, 6, 7])
        let cursor = log.cursor()
        #expect(cursor.reportedThrough == 4, "水位线推到 4，实际 \(cursor.reportedThrough)")
        #expect(cursor.reportedAbove == [6, 7], "6、7 单独记着")
        #expect(try log.unreported().map(\.localSequence) == [5],
                "只剩被拒那一条要重发")

        // 补上 5 之后水位线一口气推到底，集合清空。
        try log.markReported([5])
        #expect(log.cursor().reportedThrough == 7)
        #expect(log.cursor().reportedAbove.isEmpty, "追上之后集合要清干净")
        #expect(try log.unreported().isEmpty)
    }

    /// `decided` 还没有去处（§5 定了它存服务端，端点是 §6 的活）。
    /// **不过滤的话它会永远堆在「待发」里，让那个数字变成噪音。**
    @Test("按种类过滤:决策事件不算在待发里")
    func decisionsAreNotWaitingToBeSent() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)
        try log.append(kind: .marked, payload: ["item_key": .string("a")])
        try log.append(kind: .decided, payload: ["pick": .int(1)])
        try log.append(kind: .answered, payload: ["passed": .bool(true)])

        #expect(try log.unreported().count == 3, "全部三条都还没报")
        #expect(try log.unreported(kinds: LoggedEvent.sendableKinds).count == 2,
                "有去处的只有两条")
        #expect(LoggedEvent.Kind.decided.envelopeIsMissing,
                "决策事件现在没有信封")
    }

    @Test("拉来的事件落盘之后立刻算已上报——它本来就来自服务端")
    func pulledEventsAreNotPushedBack() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try EventLog(directory: directory)

        try log.append(kind: .marked, payload: ["item_key": .string("mine")])
        try log.appendPulled(kind: .marked, payload: ["item_key": .string("theirs")],
                             idemKey: "from-other-device",
                             occurredAt: "2026-01-01T09:00:00Z")

        #expect(try log.load().events.count == 2, "两条都在日志里——它是「知道的全部」")
        #expect(try log.unreported().map(\.localSequence) == [0],
                "只有自己那条要上报")
        #expect(try log.knownIdemKeys().contains("from-other-device"))
    }

    @Test("线上那个 type 到 kind 的映射只有一份，遥测映射成 nil")
    func wireTypesMapToKinds() {
        let cases: [(String, LoggedEvent.Kind?)] = [
            ("word.marked", .marked),
            ("word.unmarked", .unmarked),
            ("article.finished", .read),
            ("review.answered", .answered),
            ("review.spelled", .spelled),
            // 遥测:不改变状态，所以不进日志。
            ("article.opened", nil),
            ("article.progress", nil),
            ("word.tapped", nil),
            ("something.new", nil),
        ]
        for (type, expected) in cases {
            #expect(LoggedEvent.kind(forWireType: type) == expected, "\(type) 映射不对")
        }
    }

    @Test("每一种事件的去处和名字都是推导出来的，不是存着的第二个字段")
    func destinationsAreDerived() {
        let cases: [(LoggedEvent.Kind, OutboxEntry.Kind?, String?)] = [
            (.marked, .reading, "word.marked"),
            (.unmarked, .reading, "word.unmarked"),
            (.read, .reading, "article.finished"),
            (.answered, .answer, nil),
            (.spelled, .spelling, nil),
            (.decided, nil, nil),
        ]
        for (kind, destination, type) in cases {
            let event = LoggedEvent(localSequence: 0, kind: kind, payload: [:])
            #expect(event.envelope?.kind == destination, "\(kind) 的去处不对")
            #expect(event.eventType == type, "\(kind) 的事件名不对")
        }
    }
}

private extension LoggedEvent.Kind {
    /// 这一种现在有没有去处。写成扩展只为让上面那条断言读得通顺。
    var envelopeIsMissing: Bool {
        LoggedEvent(localSequence: 0, kind: self, payload: [:]).envelope == nil
    }
}
