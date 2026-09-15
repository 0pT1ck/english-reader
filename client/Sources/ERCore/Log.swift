import Foundation

/// 客户端日志 the client's own log
///
/// **This is the second log, and it answers a different question.** The server
/// has had one since P0 — `logs.db`, 30 days, queried from the admin console.
/// It records what happened *on the server*. Nothing has ever recorded what
/// happened *on the phone*: whether the request left at all, whether the mark
/// reached the disk, how many bodies a clear actually removed.
///
/// The two are joined by `trace_id`: an entry here carries the id the server
/// put in its response, so one thread can be followed from the phone into
/// `logs.db`. That is what the logging rules meant by "trace_id 贯穿" — it just
/// never had a client end until now.
///
/// **Design, and the reason for each:**
///
/// * **One file per local day** (`2026-09-15.log`). Rotation is then a matter
///   of deleting files, with no half-deleted state to get wrong, and no
///   database for a few hundred lines a day.
/// * **Three days** (决定 32). The server keeps 30 because it is diagnosing
///   the past; the phone keeps enough to answer "what just happened", and the
///   storage belongs to the learner.
/// * **JSON per line.** The screen has to filter by level and the export has
///   to be readable — a line that needs parsing with a regular expression is a
///   line that will be parsed wrongly. `exportText` renders these for humans;
///   the file itself stays machine-readable.
/// * **A write never throws.** A log that can fail the operation it is
///   describing is worse than no log. Failures land in `lastFailure` so the
///   diagnostics screen can say so instead of the whole thing being silent.
///
/// **What must never be written: the device token and the admin password.**
/// The server's rule is 「API 密钥一律不入日志」; here it matters more, because
/// **this log is meant to be sent to someone** — shared, exported, pasted into
/// a conversation. `redacting` is the backstop, not the rule: callers do not
/// pass credentials in the first place.
public enum LogLevel: Int, Codable, Sendable, CaseIterable, Comparable {
    case debug = 0
    case info = 1
    case warn = 2
    case error = 3

    public static func < (lhs: LogLevel, rhs: LogLevel) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    /// The same four names the server uses, so a person reading both does not
    /// have to translate.
    public var label: String {
        switch self {
        case .debug: return "DEBUG"
        case .info: return "INFO"
        case .warn: return "WARN"
        case .error: return "ERROR"
        }
    }

    public init?(label: String) {
        switch label.uppercased() {
        case "DEBUG": self = .debug
        case "INFO": self = .info
        case "WARN": self = .warn
        case "ERROR": self = .error
        default: return nil
        }
    }
}

/// One line. `event` is an identifier (`sync.drain.failed`), `message` is
/// Chinese prose — the same split the server uses, and for the same reason:
/// you can filter on the first and read the second.
public struct LogEntry: Codable, Sendable, Equatable {
    public let at: Date
    public let level: LogLevel
    public let event: String
    public let message: String
    public let traceId: String?
    public let fields: [String: String]

    public init(at: Date, level: LogLevel, event: String, message: String,
                traceId: String? = nil, fields: [String: String] = [:]) {
        self.at = at
        self.level = level
        self.event = event
        self.message = message
        self.traceId = traceId
        self.fields = fields
    }

    enum CodingKeys: String, CodingKey {
        case at, level, event, message
        case traceId = "trace_id"
        case fields
    }

    /// Field names whose values never belong in a file that gets shared.
    /// Matched loosely on purpose: `authorization`, `Authorization` and
    /// `admin_secret` should all be caught without anyone maintaining a list.
    static let secretHints = ["token", "secret", "password", "authorization", "bearer", "key"]

    static func redacting(_ fields: [String: String]) -> [String: String] {
        var safe: [String: String] = [:]
        for (name, value) in fields {
            let lowered = name.lowercased()
            // Replaced rather than dropped: a missing field reads as "nothing
            // was there", and that is a different fact from "it was withheld".
            safe[name] = secretHints.contains(where: lowered.contains) ? "••••" : value
        }
        return safe
    }
}

public final class FileLog: @unchecked Sendable {
    private let directory: URL
    private let fileManager: FileManager
    private let retentionDays: Int
    private let clock: @Sendable () -> Date
    private let lock = NSLock()

    /// Both of these are read and written from whatever thread happens to be
    /// logging, so they live behind the same lock as the files. `@unchecked`
    /// means the checking is mine to do.
    private var failure: String?
    private var threshold: LogLevel = .info

    /// Set when writing failed. Read by the diagnostics screen — a log that
    /// stopped recording must be able to say so, since by definition it cannot
    /// log the fact.
    public var lastFailure: String? {
        lock.lock()
        defer { lock.unlock() }
        return failure
    }

    /// The lowest level that reaches the disk. DEBUG is off by default: it is
    /// useful when chasing something, and noise the rest of the time.
    public var minimumLevel: LogLevel {
        get {
            lock.lock()
            defer { lock.unlock() }
            return threshold
        }
        set {
            lock.lock()
            defer { lock.unlock() }
            threshold = newValue
        }
    }

    private static let fileSuffix = ".log"

    public init(directory: URL, retentionDays: Int = 3,
                fileManager: FileManager = .default,
                clock: @escaping @Sendable () -> Date = { Date() }) throws {
        self.directory = directory
        self.fileManager = fileManager
        self.retentionDays = max(1, retentionDays)
        self.clock = clock
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        _ = try? prune()
    }

    // MARK: 写

    /// Record one line. Never throws, never blocks on anything but the lock.
    public func write(_ level: LogLevel, _ event: String, _ message: String,
                      traceId: String? = nil, fields: [String: String] = [:]) {
        guard level >= minimumLevel else { return }
        let entry = LogEntry(at: clock(), level: level, event: event, message: message,
                             traceId: traceId, fields: LogEntry.redacting(fields))
        append(entry)
    }

    private func append(_ entry: LogEntry) {
        lock.lock()
        defer { lock.unlock() }
        do {
            let data = try Self.encoder.encode(entry)
            var line = data
            line.append(0x0A)
            let url = fileURL(for: entry.at)
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                try handle.seekToEnd()
                try handle.write(contentsOf: line)
            } else {
                try line.write(to: url, options: .atomic)
            }
            failure = nil
        } catch {
            failure = "写日志失败：\(error.localizedDescription)"
        }
    }

    // MARK: 读

    /// Most recent first, because that is the order anyone reads a log in.
    ///
    /// A line that will not decode is skipped rather than aborting the read:
    /// a log truncated by a crash is exactly when someone is trying to read it.
    public func entries(minimum: LogLevel = .debug, limit: Int = 500) -> [LogEntry] {
        lock.lock()
        defer { lock.unlock() }
        var found: [LogEntry] = []
        for url in existingFiles().reversed() {
            guard let text = try? String(contentsOf: url, encoding: .utf8) else { continue }
            for line in text.split(separator: "\n").reversed() {
                guard let data = line.data(using: .utf8),
                      let entry = try? Self.decoder.decode(LogEntry.self, from: data) else { continue }
                guard entry.level >= minimum else { continue }
                found.append(entry)
                if found.count >= limit { return found }
            }
        }
        return found
    }

    /// The whole log as one plain-text document, oldest first — what gets
    /// copied, shared or saved to Files. Rendered for a person rather than
    /// handing over the JSONL, which is unreadable in a message.
    public func exportText() -> String {
        let all = entries(minimum: .debug, limit: Int.max).reversed()
        var lines: [String] = []
        for entry in all {
            var line = "\(Self.stamp.string(from: entry.at)) [\(entry.level.label)] "
                + "\(entry.event) \(entry.message)"
            if let traceId = entry.traceId { line += " trace=\(traceId)" }
            for name in entry.fields.keys.sorted() {
                line += " \(name)=\(entry.fields[name] ?? "")"
            }
            lines.append(line)
        }
        return lines.joined(separator: "\n") + (lines.isEmpty ? "" : "\n")
    }

    public var files: [URL] {
        lock.lock()
        defer { lock.unlock() }
        return existingFiles()
    }

    // MARK: 轮转

    /// Delete days older than the retention window. Returns how many files went.
    ///
    /// Called on every launch rather than on a timer: the app is not running
    /// most of the time, and a timer that only fires while someone is reading
    /// would keep yesterday's file around for exactly as long as it is unread.
    @discardableResult
    public func prune() throws -> Int {
        lock.lock()
        defer { lock.unlock() }
        let cutoff = Calendar.current.startOfDay(for: clock())
            .addingTimeInterval(Double(-(retentionDays - 1) * 86_400))
        var removed = 0
        for url in existingFiles() {
            let name = url.deletingPathExtension().lastPathComponent
            // A file whose name is not a date is not ours to delete.
            guard let day = Self.day.date(from: name) else { continue }
            if day < cutoff {
                try fileManager.removeItem(at: url)
                removed += 1
            }
        }
        return removed
    }

    public func clear() throws {
        lock.lock()
        defer { lock.unlock() }
        for url in existingFiles() { try fileManager.removeItem(at: url) }
    }

    // MARK: 内部

    private func existingFiles() -> [URL] {
        let entries = (try? fileManager.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil)) ?? []
        return entries
            .filter { $0.lastPathComponent.hasSuffix(Self.fileSuffix) }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    private func fileURL(for date: Date) -> URL {
        directory.appendingPathComponent(Self.day.string(from: date) + Self.fileSuffix)
    }

    /// **Computed, not stored** — the same shape `Outbox` uses. `DateFormatter`
    /// and `JSONEncoder` are not `Sendable`, so a `static let` of either does
    /// not compile under strict concurrency. Building one per call costs
    /// nothing at a few hundred lines a day.
    ///
    /// Local dates, not UTC. The learner asking "what happened this morning"
    /// means their morning; a file boundary at 08:00 local time would put one
    /// morning into two files.
    private static var day: DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter
    }

    private static var stamp: DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter
    }

    private static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }

    private static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
