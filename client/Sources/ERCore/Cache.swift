import Foundation

/// 本地缓存 the local cache
///
/// **Three kinds of data, and only one of them matters.** The outbox holds
/// events that exist nowhere else. Everything here is a copy of something the
/// server still has, and the two kinds differ only in how expensive they are to
/// fetch again:
///
/// | | 内容 | 大小 | 丢了会怎样 |
/// |---|---|---|---|
/// | 元信息 | 标题、字数、读到哪 | 几百字节 | 重拉一次，但留着很便宜 |
/// | 正文缓存 | 全文、token、释义、词组 | 约 300 KB / 篇 | 重拉一次 |
///
/// **The cache is never cleared automatically.** Read and unread alike stay
/// until the learner says otherwise. Clearing removes the body and keeps the
/// metadata, so a cleared article still shows its title and how far you got,
/// and opening it fetches the body again.
///
/// **The invariant that would break silently:** clearing must never touch the
/// outbox or the metadata. Delete an un-sent mark along with a cached article
/// and nothing appears on screen, nothing is logged, and the word never enters
/// the review queue. Hence `clearBodies` can only see the bodies directory —
/// it is not given a path it could get wrong.
public struct ArticleMeta: Codable, Equatable, Sendable {
    public let id: Int
    public let title: String
    public let source: String
    public let wordCount: Int
    public let sentenceCount: Int
    /// Non-nil once the article has been read to the end.
    public var readAt: String?
    public var percent: Double
    /// Whether this article's body is in the cache right now. Derived from the
    /// file system rather than stored, so it cannot drift from the truth.
    public var hasBody: Bool = false

    public init(id: Int, title: String, source: String, wordCount: Int,
                sentenceCount: Int, readAt: String? = nil, percent: Double = 0) {
        self.id = id
        self.title = title
        self.source = source
        self.wordCount = wordCount
        self.sentenceCount = sentenceCount
        self.readAt = readAt
        self.percent = percent
    }
}

/// What the client can do with an article right now.
///
/// Four states, not two. A cleared article that the device cannot reach is a
/// different situation from one still downloading, and a UI that could not tell
/// them apart would show a spinner in both cases — one of which will never end.
public enum ArticleAvailability: Equatable, Sendable {
    /// Body in the cache. Readable with no network.
    case cached
    /// Metadata only — cleared, or never opened. Tapping fetches it.
    case metadataOnly
    /// Being fetched, or being annotated on the server (which answers with a
    /// `preparing` block rather than an error, because asking for an article
    /// that is still being prepared is normal, not a mistake).
    case fetching(done: Int, total: Int)
    /// Metadata only, and no way to reach the server. **This is the one worth
    /// saying out loud**: the learner cleared it, went offline, and tapped it.
    case unavailableOffline
}

public enum CacheError: Error, Equatable {
    case notFound(Int)
}

/// Article bodies and metadata on disk.
public final class ArticleCache: @unchecked Sendable {
    private let root: URL
    private let bodies: URL
    /// One file per body holding the server's `ETag` for it. **A directory of
    /// its own**, not `<id>.etag` beside the body: `bodySizes()` reads any file
    /// whose stem is a number as a body, and would count these too.
    private let etags: URL
    private let metaFile: URL
    private let fileManager: FileManager
    private let lock = NSLock()

    public init(directory: URL, fileManager: FileManager = .default) throws {
        self.root = directory
        self.bodies = directory.appendingPathComponent("bodies", isDirectory: true)
        self.etags = directory.appendingPathComponent("etags", isDirectory: true)
        self.metaFile = directory.appendingPathComponent("articles.json")
        self.fileManager = fileManager
        try fileManager.createDirectory(at: bodies, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: etags, withIntermediateDirectories: true)
    }

    // MARK: Metadata

    private func loadMeta() -> [Int: ArticleMeta] {
        guard let data = try? Data(contentsOf: metaFile),
              let list = try? JSONDecoder.contract.decode([ArticleMeta].self, from: data)
        else { return [:] }
        return Dictionary(uniqueKeysWithValues: list.map { ($0.id, $0) })
    }

    private func saveMeta(_ meta: [Int: ArticleMeta]) throws {
        let list = meta.values.sorted { $0.id < $1.id }
        try JSONEncoder.contract.encode(list).write(to: metaFile, options: .atomic)
    }

    /// Record what is known about an article without touching its body.
    public func remember(_ entries: [ArticleMeta]) throws {
        lock.lock()
        defer { lock.unlock() }
        var meta = loadMeta()
        for entry in entries {
            // Keep the local reading position when the server has none: the
            // learner may have read further since the package was fetched and
            // not yet reported it.
            var merged = entry
            if let existing = meta[entry.id] {
                merged.percent = max(entry.percent, existing.percent)
                merged.readAt = entry.readAt ?? existing.readAt
            }
            meta[entry.id] = merged
        }
        try saveMeta(meta)
    }

    /// Everything known, with `hasBody` filled in from the file system.
    public func articles() -> [ArticleMeta] {
        lock.lock()
        defer { lock.unlock() }
        return loadMeta().values
            .map { entry in
                var copy = entry
                copy.hasBody = fileManager.fileExists(atPath: bodyURL(entry.id).path)
                return copy
            }
            .sorted { $0.id > $1.id }
    }

    // MARK: Bodies

    private func bodyURL(_ id: Int) -> URL {
        bodies.appendingPathComponent("\(id).json")
    }

    /// Store one article's full payload, exactly as it arrived.
    ///
    /// Kept as raw bytes rather than a decoded model: the client must be able to
    /// hand the server's own shape back to the decoder, and re-encoding through
    /// a model would quietly drop any field this version of the client does not
    /// know about — which is the thing 架构铁律 5 exists to prevent.
    ///
    /// `etag` is the version the server gave it, sent back on the next open to
    /// ask "has this changed" (2026-09-24). **Written or removed together with
    /// the body**: an etag left pointing at older content would make the server
    /// answer 304 for bytes the device does not have. A body that came inside
    /// the day's package has none, so the first revalidation fetches it whole.
    public func storeBody(_ id: Int, _ data: Data, etag: String? = nil) throws {
        lock.lock()
        defer { lock.unlock() }
        try data.write(to: bodyURL(id), options: .atomic)
        if let etag, let encoded = etag.data(using: .utf8) {
            try? encoded.write(to: etagURL(id), options: .atomic)
        } else {
            try? fileManager.removeItem(at: etagURL(id))
        }
    }

    /// The server's version of the cached body, if it gave one.
    public func etag(_ id: Int) -> String? {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? Data(contentsOf: etagURL(id)),
              let text = String(data: data, encoding: .utf8), !text.isEmpty
        else { return nil }
        return text
    }

    private func etagURL(_ id: Int) -> URL {
        etags.appendingPathComponent("\(id)")
    }

    public func body(_ id: Int) throws -> Data {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? Data(contentsOf: bodyURL(id)) else {
            throw CacheError.notFound(id)
        }
        return data
    }

    public func availability(_ id: Int, online: Bool) -> ArticleAvailability {
        lock.lock()
        defer { lock.unlock() }
        if fileManager.fileExists(atPath: bodyURL(id).path) { return .cached }
        return online ? .metadataOnly : .unavailableOffline
    }

    // MARK: Clearing

    /// How much disk each cached article is using, so the learner can choose.
    public func bodySizes() -> [Int: Int] {
        lock.lock()
        defer { lock.unlock() }
        var sizes: [Int: Int] = [:]
        let urls = (try? fileManager.contentsOfDirectory(
            at: bodies, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        for url in urls {
            guard let id = Int(url.deletingPathExtension().lastPathComponent) else { continue }
            sizes[id] = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        }
        return sizes
    }

    /// Remove the bodies of the given articles. Metadata stays; the outbox is
    /// not reachable from here at all.
    ///
    /// Returns how many were removed. Read and unread are treated the same —
    /// the learner said which ones, and second-guessing that would be the app
    /// deciding what is worth keeping.
    @discardableResult
    public func clearBodies(_ ids: [Int]) throws -> Int {
        lock.lock()
        defer { lock.unlock() }
        var removed = 0
        for id in ids where fileManager.fileExists(atPath: bodyURL(id).path) {
            try fileManager.removeItem(at: bodyURL(id))
            try? fileManager.removeItem(at: etagURL(id))
            removed += 1
        }
        return removed
    }

    /// Every cached body. Still only bodies — the name says "bodies" for the
    /// same reason the method takes no path.
    @discardableResult
    public func clearAllBodies() throws -> Int {
        try clearBodies(Array(bodySizes().keys))
    }
}

/// The day's package, cached whole.
///
/// Stored as the bytes that arrived, for the same reason article bodies are:
/// a field this client does not know about must survive to the next one.
public final class DayCache: @unchecked Sendable {
    private let file: URL
    private let fileManager: FileManager
    private let lock = NSLock()

    public init(directory: URL, fileManager: FileManager = .default) throws {
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        self.file = directory.appendingPathComponent("today.json")
        self.fileManager = fileManager
    }

    public func store(_ data: Data, etag: String? = nil) throws {
        lock.lock()
        defer { lock.unlock() }
        try data.write(to: file, options: .atomic)
        // **版本号和内容一起写、一起没。**留下一个指向旧内容的 etag，
        // 服务端会说「没变」，而客户端手里已经不是那一份了。
        if let etag, let encoded = etag.data(using: .utf8) {
            try? encoded.write(to: etagFile, options: .atomic)
        } else {
            try? fileManager.removeItem(at: etagFile)
        }
    }

    public func load() -> Data? {
        lock.lock()
        defer { lock.unlock() }
        return try? Data(contentsOf: file)
    }

    /// 本地这一份的版本号，随请求带上去给服务端比。
    public func etag() -> String? {
        lock.lock()
        defer { lock.unlock() }
        guard let data = try? Data(contentsOf: etagFile),
              let text = String(data: data, encoding: .utf8), !text.isEmpty else { return nil }
        return text
    }

    public var hasPackage: Bool { load() != nil }

    private var etagFile: URL { file.deletingPathExtension().appendingPathExtension("etag") }
}
