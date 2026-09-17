import Foundation

/// 事件日志 the event log / 投影 projection / 断尾 torn tail
///
/// **这是 P9 的地基。** 那条线（服务端只管生文）把学习记录的第一副本搬到了设备上，
/// 而「状态是事件日志的纯函数」是它的脊梁：词池、队列、排期、进度、打卡，
/// 全都由重放这份日志算出来，没有一个是独立存着的事实。
///
/// ## 为什么是一个追加式文件，而不是数据库，也不是「一条一个文件」
///
/// **U2 原本定的是「系统 SQLite ＋ 薄封装」。2026-09-17 改掉，三条依据：**
///
/// 1. **量太小，数据库是多余的。** 实测全部历史 375 条事件，增速约 70 条/天
///    ＝ 2.5 万条/年，一年约 5 MB。重放两万五千条是毫秒级的事（§14 U3）。
///    投影整个放在内存里，启动时重放一次就有——这比任何查询都快。
/// 2. **Linux 那一课刚上过。** Core 必须在 Windows、Linux、iOS 上都编得过
///    （`Package.swift` 开头那段）。官方那个 FSRS 包就是栽在
///    `import JavaScriptCore` 上的；而从 Swift 用 SQLite 在 Linux 上要
///    systemLibrary ＋ modulemap ＋ 容器里装 `libsqlite3-dev`，
///    **同一个形状的风险，而且这次是自找的**。
/// 3. **有数据库会把人引回旧错误。** 一张 `study_states` 表摆在那儿，
///    下一个人就会把它当成事实来源直接改——而 P9 存在的全部理由就是那件事。
///    只有日志能改，投影只能算，这条规矩靠「没有别的东西可以写」来保证。
///
/// **发件箱那条「一条一个文件」的理由不适用这里**，它的原话是：
/// 「a log only grows, so it has to be compacted, and compaction rewrites the
/// whole file: crash in the middle of *that* and many un-sent events go at
/// once」。**这份日志永不压缩**——它是事实，只增不减，5 MB/年也没有压缩的必要，
/// 所以那个失败模式不存在。反过来，两万五千个文件会让每次 `append` 都去列一遍
/// 目录（发件箱现在就是这么取下一个序号的），越用越慢。
///
/// **唯一的崩溃损伤是断尾**：最后一行写了一半。而那一行按定义是「还没告诉用户
/// 已经保存」的那条事件——和发件箱依赖的是同一个性质。载入时把它丢掉并报出来。
public struct LoggedEvent: Codable, Equatable, Sendable {

    /// 五种事件（phase-9.html §5）。**别的都算得回来**，所以别往这里加。
    ///
    /// `marked` 与 `unmarked` 是同一件事的两个方向：标记让一个词进复习队列
    /// （跨 Phase 不变量：**进队列只有这一条路**），撤销让它退回 `new`
    /// **而遇见记录保留**——它确实被遇见过。
    public enum Kind: String, Codable, Sendable, CaseIterable {
        /// 标记一个词或词组的某个义项。
        case marked
        /// 撤销标记。
        case unmarked
        /// 读完一篇。**自带这篇里遇见了哪些词**（§9）：这样重放不需要文章内容，
        /// 而且更忠实——文章将来可能被重新分析过，而「我在那一句遇见过它」
        /// 是一件历史事实。
        case read
        /// 一次作答。
        case answered
        /// 一次拼写。**存的是拼成了什么**，不只是对错。
        case spelled
        /// 一次判断（决策日志）。**它不可重算**：抽卡用的随机数、当时货架上有
        /// 什么、配置是多少，过后都复原不出来。丢了就真的答不上「为什么」。
        case decided
    }

    /// 这台设备上的单调序号。**不是全序。**
    ///
    /// 全序由服务端收到时分配（§6 细则 ①），因为两台手机的时钟不一样。
    /// 这个号只保证同一台设备上的先后，用来记「报到哪儿了」。
    public let localSequence: Int

    /// 幂等键。**由设备生成**：只有这台机器知道这是不是十分钟前在火车上没传上去的
    /// 那一条。
    public let idemKey: String

    public let kind: Kind

    /// 设备时钟，可能是错的。**照样记下来**，而且只用来判「当天」——
    /// 排序用服务端那个序号，两个用途分开，别用同一个数。
    public let occurredAt: String

    public let payload: [String: JSONValue]

    public init(localSequence: Int, idemKey: String = UUID().uuidString,
                kind: Kind, occurredAt: String = ISO8601DateFormatter().string(from: Date()),
                payload: [String: JSONValue]) {
        self.localSequence = localSequence
        self.idemKey = idemKey
        self.kind = kind
        self.occurredAt = occurredAt
        self.payload = payload
    }
}

public enum EventLogError: Error, Equatable {
    case notADirectory(String)
    case cannotOpen(String)
}

/// 载入一次日志的结果。
public struct EventLogLoad: Sendable, Equatable {
    public var events: [LoggedEvent]
    /// 中间读不出来的行数。**平时是 0。** 非零意味着有一条事件既发不出去也读不
    /// 回来，而投影会因此少算一笔——没有这个数的话，没人知道为什么。
    public var damaged: Int
    /// 最后一行是不是断的。**它和 `damaged` 是两件事**：断尾是崩在写的那一刻，
    /// 那条事件从未被确认过；中间坏了是别的原因，要查。
    public var tornTail: Bool
}

/// 追加式的事件日志。一个文件，一行一条 JSON。
///
/// 纯 Foundation，Windows、Linux、iOS 行为一致——同发件箱的理由。
public final class EventLog: @unchecked Sendable {
    private let file: URL
    private let cursorFile: URL
    private let fileManager: FileManager
    private let lock = NSLock()
    /// 下一条用哪个号。载入时算出来，之后在内存里递增——**不再去数文件**，
    /// 那正是发件箱越用越慢的原因。
    private var nextSequence: Int

    public init(directory: URL, fileManager: FileManager = .default) throws {
        self.fileManager = fileManager
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: directory.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw EventLogError.notADirectory(directory.path)
        }
        self.file = directory.appendingPathComponent("events.jsonl")
        self.cursorFile = directory.appendingPathComponent("cursor.json")
        if !fileManager.fileExists(atPath: file.path) {
            fileManager.createFile(atPath: file.path, contents: Data())
        }
        self.nextSequence = (try? Self.read(file, fileManager: fileManager))
            .map { ($0.events.last?.localSequence ?? -1) + 1 } ?? 0
    }

    // MARK: 写

    /// 追加一条。**返回时它已经落盘。**
    ///
    /// 写完 `synchronize()` 才返回：反过来的话用户会看见「已标记」，
    /// 而它其实还在页缓存里，断电就没了。这是发件箱那条
    /// 「write to disk first, tell the learner second」的同一条规矩。
    @discardableResult
    public func append(kind: LoggedEvent.Kind, payload: [String: JSONValue],
                       idemKey: String = UUID().uuidString,
                       occurredAt: String? = nil) throws -> LoggedEvent {
        lock.lock()
        defer { lock.unlock() }

        let event = LoggedEvent(
            localSequence: nextSequence,
            idemKey: idemKey,
            kind: kind,
            occurredAt: occurredAt ?? ISO8601DateFormatter().string(from: Date()),
            payload: payload
        )
        var line = try JSONEncoder.contract.encode(event)
        line.append(0x0A)   // \n

        guard let handle = FileHandle(forWritingAtPath: file.path) else {
            throw EventLogError.cannotOpen(file.path)
        }
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: line)
        try handle.synchronize()

        nextSequence += 1
        return event
    }

    // MARK: 读

    /// 整份日志，按写入顺序。
    public func load() throws -> EventLogLoad {
        lock.lock()
        defer { lock.unlock() }
        return try Self.read(file, fileManager: fileManager)
    }

    /// **一行读不出来不让整份日志读不出来。** 同发件箱那条：
    /// 「one damaged record must not make the whole queue unsendable, which
    /// would turn a single lost event into every event lost」。
    private static func read(_ file: URL, fileManager: FileManager) throws -> EventLogLoad {
        guard let data = fileManager.contents(atPath: file.path) else {
            return EventLogLoad(events: [], damaged: 0, tornTail: false)
        }
        // 按换行切。最后一段为空表示文件正常地以换行结尾；非空就是断尾。
        var segments = data.split(separator: 0x0A, omittingEmptySubsequences: false)
        var tornTail = false
        if let last = segments.last {
            if last.isEmpty {
                segments.removeLast()
            } else {
                tornTail = true
                segments.removeLast()
            }
        }

        var events: [LoggedEvent] = []
        var damaged = 0
        let decoder = JSONDecoder()
        for segment in segments {
            if segment.isEmpty { continue }
            if let event = try? decoder.decode(LoggedEvent.self, from: Data(segment)) {
                events.append(event)
            } else {
                damaged += 1
            }
        }
        return EventLogLoad(events: events, damaged: damaged, tornTail: tornTail)
    }

    // MARK: 报到哪儿了

    /// 上报进度。**存在日志之外的一个小文件里，绝不回头改日志。**
    ///
    /// 改日志就意味着重写它，而重写是这份文件唯一没有的危险操作——
    /// 发件箱那段注释讲的正是这个。所以「报到哪儿了」是一个可以随便覆盖的
    /// 小状态，不是日志的一部分。
    public struct Cursor: Codable, Equatable, Sendable {
        /// 本地序号报到哪儿了（含）。-1 ＝ 一条都没报。
        public var reportedThrough: Int
        /// 从服务端拉到哪个全序序号了（含）。0 ＝ 还没拉过。
        public var pulledThrough: Int

        public init(reportedThrough: Int = -1, pulledThrough: Int = 0) {
            self.reportedThrough = reportedThrough
            self.pulledThrough = pulledThrough
        }
    }

    public func cursor() -> Cursor {
        lock.lock()
        defer { lock.unlock() }
        guard let data = fileManager.contents(atPath: cursorFile.path),
              let cursor = try? JSONDecoder().decode(Cursor.self, from: data) else {
            return Cursor()
        }
        return cursor
    }

    public func setCursor(_ cursor: Cursor) throws {
        lock.lock()
        defer { lock.unlock() }
        try JSONEncoder.contract.encode(cursor).write(to: cursorFile, options: .atomic)
    }

    /// 还没上报的那些，最旧的在前。
    public func unreported() throws -> [LoggedEvent] {
        let through = cursor().reportedThrough
        return try load().events.filter { $0.localSequence > through }
    }
}
