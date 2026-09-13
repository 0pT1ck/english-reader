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

    private(set) var outbox: Outbox?
    private(set) var library: LibraryStore?
    private(set) var cache: ArticleCache?
    private(set) var dayCache: DayCache?
    private(set) var engine: SyncEngine?

    /// 起不来的原因。存储建不起来是唯一一种「App 没法用」的失败，
    /// 所以它要说得出话，而不是让每一屏各自转圈。
    private(set) var storageFailure: String?

    /// 发件箱里还有几条没上报。设置页显示它——那是「我标的东西到底传上去了没有」
    /// 的唯一出口，而这个 Phase 没有别的地方能回答这个问题。
    private(set) var pendingEvents: Int = 0

    init(connection: Connection = Connection()) {
        self.connection = connection
        buildStorage()
        rebuildEngine()
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
            storageFailure = nil
            refreshPendingCount()
        } catch {
            storageFailure = "本地存储建不起来：\(error.localizedDescription)"
        }
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
            transport: IOSTransport(baseURL: url, token: connection.token),
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
            return true
        } catch {
            return false
        }
    }

    func refreshPendingCount() {
        pendingEvents = outbox?.count ?? 0
    }

    /// 把攒着的事件发出去。失败不作声——发件箱的全部意义就是失败了可以再来。
    func drain() async {
        guard let engine else { return }
        _ = try? await engine.drain()
        refreshPendingCount()
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
