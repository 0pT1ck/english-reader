import Foundation
import Observation
import ERCore
import ERContract

/// App 共用的那一份东西：三类本地存储、发件箱、同步引擎。
///
/// **本地存三类，只有一类不能丢**（P5 决定 8）：发件箱是客户端唯一独有的数据，
/// 元信息和正文缓存丢了都能重新拿。这里把三个目录分开建，就是为了让
/// 「清缓存不许碰发件箱」这条不变量在文件系统上就成立——清理动的是
/// `bodies/`，发件箱在 `outbox/`，两者没有交集。
@MainActor
@Observable
final class AppModel {
    let connection: Connection
    let preferences: Preferences

    private(set) var outbox: Outbox?
    private(set) var library: LibraryStore?
    private(set) var cache: ArticleCache?
    private(set) var dayCache: DayCache?
    private(set) var engine: SyncEngine?

    /// 客户端自己的日志（P8 §9）。**和发件箱、缓存并列建在同一个根下**，
    /// 但它是第四类：丢了不影响任何学习记录，三天之后本来也要自己删掉。
    private(set) var log: FileLog?

    /// 上一次同步的结果。`SyncReport` 早就返回这四个数，**至今没人显示过**——
    /// 设置页的存储那一区是它第一个读者。
    private(set) var lastSync: SyncReport?

    /// 起不来的原因。存储建不起来是唯一一种「App 没法用」的失败，
    /// 所以它要说得出话，而不是让每一屏各自转圈。
    private(set) var storageFailure: String?

    /// 发件箱里还有几条没上报。设置页显示它——那是「我标的东西到底传上去了没有」
    /// 的唯一出口，而这个 Phase 没有别的地方能回答这个问题。
    private(set) var pendingEvents: Int = 0

    /// 读不出来的事件文件有几条。**平时是 0**，非零意味着有一次标记既发不出去
    /// 也读不回来——那时待发数会永远卡着不动，而没有这个数的话，没人知道为什么。
    private(set) var damagedEvents: Int = 0

    init(connection: Connection = Connection(), preferences: Preferences = Preferences()) {
        self.connection = connection
        self.preferences = preferences
        buildStorage()
        rebuildEngine()
        log?.write(.info, "app.launched", "起来了",
                   fields: ["configured": connection.isConfigured ? "yes" : "no",
                            "pending": String(pendingEvents)])
    }

    /// 开发者选项那一页用的。**没填管理密码就是 nil**——一个「点了没反应」的
    /// 按钮比一个说得出「还没填密码」的界面难查得多，所以这里返回可选值，
    /// 由界面去解释它为什么没有。
    var admin: AdminClient? {
        guard let url = connection.adminURL, !connection.adminSecret.isEmpty else { return nil }
        return AdminClient(baseURL: url, secret: connection.adminSecret)
    }

    // MARK: 存储

    private func buildStorage() {
        do {
            let root = try FileManager.default.url(
                for: .applicationSupportDirectory, in: .userDomainMask,
                appropriateFor: nil, create: true
            ).appendingPathComponent("EnglishReader", isDirectory: true)

            outbox = try Outbox(directory: root.appendingPathComponent("outbox"))
            cache = try ArticleCache(directory: root.appendingPathComponent("articles"))
            dayCache = try DayCache(directory: root.appendingPathComponent("day"))
            library = LibraryStore(directory: root.appendingPathComponent("library"))
            // 三天轮转在 init 里就发生（`FileLog` 自己 prune），所以这一行
            // 同时是「启动时清过期日志」那条决定的落点。
            let file = try FileLog(directory: root.appendingPathComponent("logs"),
                                   retentionDays: 3)
            file.minimumLevel = preferences.verboseLog ? .debug : .info
            log = file
            storageFailure = nil
            refreshPendingCount()
        } catch {
            storageFailure = "本地存储建不起来：\(error.localizedDescription)"
            // 日志本身可能就是没建起来的那个，所以这条要两头都说：
            // 写进日志（如果还能写），也留在 `storageFailure` 上给界面看。
            log?.write(.error, "storage.unavailable", storageFailure ?? "")
        }
    }

    /// DEBUG 落不落盘，跟着偏好走。设置页改了开关之后调它。
    func applyLogLevel() {
        log?.minimumLevel = preferences.verboseLog ? .debug : .info
    }

    // MARK: 连接

    /// 换了地址或令牌就换一条传输。引擎是 actor，里面握着 transport，
    /// 所以换传输就是换引擎——没有「改一半」的中间状态。
    func rebuildEngine() {
        guard let outbox, let cache, let dayCache,
              let url = connection.url, !connection.token.isEmpty else {
            engine = nil
            return
        }
        engine = SyncEngine(
            transport: IOSTransport(baseURL: url, token: connection.token, log: log),
            outbox: outbox, articles: cache, day: dayCache
        )
    }

    // MARK: 发件箱

    /// 记一条事件。**先落盘再说成功**——反过来的话用户会看见「已标记」
    /// 而它其实没写进去。
    @discardableResult
    func record(_ entry: OutboxEntry) -> Bool {
        guard let outbox else { return false }
        do {
            try outbox.append(entry)
            refreshPendingCount()
            log?.write(.debug, "outbox.recorded", "记下一条事件",
                       fields: ["kind": entry.kind.rawValue, "pending": String(pendingEvents)])
            return true
        } catch {
            // **这一条必须记。**写不进发件箱意味着那次标记消失了，而屏幕上
            // 什么都不会说——正是「静默失败」那一类里最贵的一种。
            log?.write(.error, "outbox.record.failed", "事件没能落盘",
                       fields: ["kind": entry.kind.rawValue,
                                "error": error.localizedDescription])
            return false
        }
    }

    /// 待发条数。**只数文件，不读文件。**
    ///
    /// 2026-09-16 修：这里一度顺手调了 `outbox.pending()` 去数损坏条目，
    /// 而那个方法会**读取并解码发件箱里的每一个文件**——它是给上报用的。
    /// 而 `record()` 每记一条事件就调一次这里，于是每答一题就把待发的全部
    /// 事件重读重解一遍，答得越多越慢。真机上表现为「越用越卡」。
    ///
    /// 损坏条目改成按需算（`refreshDamagedCount`），它只有设置页要看。
    func refreshPendingCount() {
        pendingEvents = outbox?.count ?? 0
    }

    /// 读不出来的事件文件有几条。**贵，所以只在有人要看的时候算**——
    /// 它要把发件箱整个读一遍。
    func refreshDamagedCount() {
        guard let outbox, let pending = try? outbox.pending() else {
            damagedEvents = 0
            return
        }
        damagedEvents = pending.damaged.count
    }

    /// 扔掉读不出来的那些事件文件。
    ///
    /// **这是个有损失的动作，所以只有人能按。**一条写坏的事件发不出去也读不出来，
    /// 于是待发数永远减不到零——而它代表的那次标记，扔掉就真的没了。
    /// 界面上必须把这句话说清楚，然后由人自己决定。
    @discardableResult
    func discardDamagedEvents() -> Int {
        guard let outbox, let damaged = try? outbox.pending().damaged, !damaged.isEmpty
        else { return 0 }
        try? outbox.discardDamaged(damaged)
        log?.write(.warn, "outbox.damaged.discarded", "扔掉了读不出来的事件",
                   fields: ["count": String(damaged.count)])
        refreshPendingCount()
        return damaged.count
    }

    /// 把攒着的事件发出去。**界面上失败不作声**——发件箱的全部意义就是失败了
    /// 可以再来；但日志里要说，那是「我标的东西传上去了没有」唯一查得到的地方。
    @discardableResult
    func drain() async -> SyncReport? {
        guard let engine else { return nil }
        let before = pendingEvents
        do {
            let report = try await engine.drain()
            lastSync = report
            refreshPendingCount()
            if before > 0 || report.landed > 0 {
                log?.write(report.rejected > 0 ? .warn : .info, "sync.drained", "上报了一批",
                           fields: ["landed": String(report.landed),
                                    "duplicates": String(report.duplicates),
                                    "rejected": String(report.rejected),
                                    "remaining": String(report.remaining),
                                    "offline": report.offline ? "yes" : "no"])
            }
            return report
        } catch {
            log?.write(.warn, "sync.drain.failed", "这一批没送出去",
                       fields: ["pending": String(before),
                                "error": error.localizedDescription])
            refreshPendingCount()
            return nil
        }
    }
}

/// 列表的离线退路。
///
/// Core 的 `SyncEngine.library()` 联不上就抛错，这是对的——它是个查询，不是
/// 今日包那种「离线形状」。但列表屏总得有东西显示，所以 App 自己把最后一次
/// 拿到的列表原样存一份。**存的是原始 JSON**，不是自己拆出来的结构：
/// 拆过一遍的数据在契约加字段之后会悄悄少东西，原始字节不会。
final class LibraryStore: @unchecked Sendable {
    private let directory: URL

    init(directory: URL) {
        self.directory = directory
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
    }

    private func file(_ key: String) -> URL {
        directory.appendingPathComponent("\(key).json")
    }

    func store(_ data: Data, key: String) {
        try? data.write(to: file(key), options: .atomic)
    }

    func load(key: String) -> Components.Schemas.LibraryResponse? {
        guard let data = try? Data(contentsOf: file(key)) else { return nil }
        return try? JSONDecoder().decode(
            Components.Schemas.LibraryResponse.self, from: data)
    }
}
