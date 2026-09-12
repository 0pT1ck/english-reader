import Foundation
import Testing
@testable import ERCore

struct CacheTests {
    static func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("cache-test-\(UUID().uuidString)")
    }

    static func meta(_ id: Int, read: Bool = false, percent: Double = 0) -> ArticleMeta {
        ArticleMeta(id: id, title: "第 \(id) 篇", source: "generated",
                    wordCount: 430, sentenceCount: 20,
                    readAt: read ? "2026-09-12T00:00:00+00:00" : nil,
                    percent: percent)
    }

    @Test func clearingKeepsTheMetadataAndTheProgress() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = try ArticleCache(directory: directory)

        try cache.remember([Self.meta(7, read: true, percent: 100), Self.meta(8, percent: 40)])
        try cache.storeBody(7, Data("{\"body\":\"七\"}".utf8))
        try cache.storeBody(8, Data("{\"body\":\"八\"}".utf8))
        #expect(cache.articles().allSatisfy { $0.hasBody })

        // 读完的、没读完的都留着，由用户指定清理哪些。
        let removed = try cache.clearBodies([7, 8])
        #expect(removed == 2)

        let after = cache.articles()
        #expect(after.count == 2, "清理之后条目还在——标题、字数、进度都不该消失")
        let noBodies = after.allSatisfy { !$0.hasBody }
        #expect(noBodies)
        #expect(after.first { $0.id == 8 }?.percent == 40, "读到哪儿也要留着")
        #expect(after.first { $0.id == 7 }?.readAt != nil)
    }

    /// The invariant worth its own test, because breaking it is silent: clearing
    /// the cache must not touch the outbox. A mark that vanishes with a cached
    /// article shows nothing on screen and logs nothing — the word simply never
    /// enters the review queue.
    @Test func clearingDoesNotTouchTheOutbox() throws {
        let root = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }

        let outbox = try Outbox(directory: root.appendingPathComponent("outbox"))
        let cache = try ArticleCache(directory: root.appendingPathComponent("articles"))

        try outbox.append(OutboxEntry(idemKey: "unsent-mark", kind: .reading,
                                      payload: ["headword": "account"]))
        try cache.remember([Self.meta(7)])
        try cache.storeBody(7, Data("{}".utf8))

        try cache.clearAllBodies()

        #expect(outbox.count == 1, "**清理缓存绝不能碰发件箱**——只删能重新拿回来的东西")
        #expect(try outbox.pending().entries.first?.idemKey == "unsent-mark")
        #expect(cache.articles().count == 1, "元信息也不在清理范围内")
    }

    @Test func availabilitySeparatesClearedFromUnreachable() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = try ArticleCache(directory: directory)
        try cache.remember([Self.meta(7)])
        try cache.storeBody(7, Data("{}".utf8))

        #expect(cache.availability(7, online: true) == .cached)
        #expect(cache.availability(7, online: false) == .cached, "缓存着的离线照样能读")

        try cache.clearBodies([7])
        #expect(cache.availability(7, online: true) == .metadataOnly, "点开会重新拉")
        // 清理过的文章在离线时点开会打不开。这是用户自己选的清理，代价合理，
        // 但要能表达出来——否则界面只能显示一个永远转不完的圈。
        #expect(cache.availability(7, online: false) == .unavailableOffline)
    }

    @Test func sizesLetTheLearnerChooseWhatToClear() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = try ArticleCache(directory: directory)
        try cache.storeBody(1, Data(repeating: 0x20, count: 300_000))
        try cache.storeBody(2, Data(repeating: 0x20, count: 1_000))

        let sizes = cache.bodySizes()
        #expect(sizes[1] == 300_000)
        #expect(sizes[2] == 1_000)
    }

    @Test func localProgressIsNotOverwrittenByAStalePackage() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let cache = try ArticleCache(directory: directory)

        try cache.remember([Self.meta(7, percent: 0)])
        try cache.remember([Self.meta(7, percent: 62)])   // 本地读到 62%
        // 今日包是早上拉的，它不知道后来读了多少。
        try cache.remember([Self.meta(7, percent: 0)])

        #expect(cache.articles().first?.percent == 62,
                "服务端那份是旧的，别把本地的进度抹掉——它还在发件箱里等着上报")
    }

    @Test func theDayPackageIsStoredAsItArrived() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let day = try DayCache(directory: directory)

        #expect(!day.hasPackage)
        // 原样存字节，不经过模型：这个版本的客户端不认识的字段也要留给下一个版本，
        // 那正是铁律 5 要保的东西。
        let payload = Data("{\"day\":\"2026-09-12\",\"未来才有的字段\":123}".utf8)
        try day.store(payload)
        #expect(day.load() == payload)
    }
}
