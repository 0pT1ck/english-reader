import Foundation

/// 发件箱 the outbox
///
/// **The one thing on the device that cannot be lost.** Everything else the
/// client stores is a copy of something the server already has — today's
/// package, the article text, the glossary, all of it can be fetched again. An
/// event that has not reached the server exists nowhere else: you marked a
/// word, the screen said so, and if that file disappears the word simply never
/// enters the review queue, with nothing to report and nothing to notice.
///
/// **One event, one file.** The alternative — a single append-only log with a
/// cursor — was rejected for one reason. Both lose the same thing on a crash:
/// whatever was mid-write, which the learner had not yet been told about. But a
/// log only grows, so it has to be compacted, and compaction rewrites the whole
/// file: crash in the middle of *that* and many un-sent events go at once. A
/// file per event has no such operation.
///
/// **Write to disk first, tell the learner second.** Reversing those two is how
/// a mark can be shown as saved and then vanish.
public struct OutboxEntry: Codable, Equatable, Sendable {
    /// Generated here, not by the server: this device is the only party that
    /// knows whether this is the same event it failed to upload ten minutes ago
    /// on a train.
    public let idemKey: String
    public let kind: Kind
    /// The event body, exactly as the endpoint expects it.
    public let payload: [String: JSONValue]
    /// The device's clock, which may be wrong. Sent anyway — the server keeps
    /// both and a disagreement stays visible rather than being resolved away.
    public let occurredAt: String

    public enum Kind: String, Codable, Sendable {
        /// Goes to `/v1/client/events`.
        case reading
        /// Goes to `/v1/client/reviews/answers`. **Order matters for these**:
        /// passing 看词想义 unlocks 看义想词, so the server replays them in the
        /// sequence the client sends.
        case answer
        /// Goes to `/v1/client/reviews/spellings`.
        case spelling
    }

    public init(idemKey: String = UUID().uuidString,
                kind: Kind,
                payload: [String: JSONValue],
                occurredAt: Date = Date()) {
        self.idemKey = idemKey
        self.kind = kind
        self.payload = payload
        self.occurredAt = ISO8601DateFormatter.contract.string(from: occurredAt)
    }
}

/// What the server said about one entry.
public enum OutboxVerdict: Sendable, Equatable {
    /// Applied. Safe to delete.
    case landed
    /// Already on file **and already applied**. Safe to delete.
    ///
    /// The server only answers this when the earlier attempt actually took
    /// effect; one that failed comes back to be applied again, which is why
    /// deleting on this verdict is safe. Before that fix a duplicate could mean
    /// "stored but never applied", and deleting on it lost the event for good.
    case duplicate
    /// Refused or errored. **Keep the file.**
    case rejected(String)
}

public enum OutboxError: Error, Equatable {
    case notADirectory(String)
    case unreadable(String, String)
}

/// A durable queue of events waiting to reach the server.
///
/// Plain files through Foundation, which behaves the same on Windows, Linux and
/// iOS — the three places this has to run. No database: the requirement here is
/// "do not lose a small append-only set", not "query it".
public final class Outbox: @unchecked Sendable {
    private let directory: URL
    private let lock = NSLock()
    private let fileManager: FileManager

    /// Files are named `<sequence>-<key>.json`, zero-padded so a plain
    /// lexicographic sort is the send order. Ten digits is more events than
    /// this app can produce in a human lifetime.
    private static let sequenceWidth = 10
    private static let temporarySuffix = ".writing"

    public init(directory: URL, fileManager: FileManager = .default) throws {
        self.directory = directory
        self.fileManager = fileManager
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: directory.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw OutboxError.notADirectory(directory.path)
        }
        try sweepInterrupted()
    }

    /// Remove leftovers from a write that never finished.
    ///
    /// A `.writing` file is by definition an event the learner was never told
    /// had been saved — the confirmation comes after the rename. Leaving them
    /// would mean a directory that slowly fills with things that will never be
    /// sent and never be read.
    private func sweepInterrupted() throws {
        let entries = try fileManager.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: nil)
        for url in entries where url.lastPathComponent.hasSuffix(Self.temporarySuffix) {
            try? fileManager.removeItem(at: url)
        }
    }

    private func fileNames() throws -> [String] {
        try fileManager.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .map(\.lastPathComponent)
            .filter { $0.hasSuffix(".json") }
            .sorted()
    }

    private func nextSequence() throws -> Int {
        let last = try fileNames().last
        guard let last, let number = Int(last.prefix(Self.sequenceWidth)) else { return 0 }
        return number + 1
    }

    /// Append one event. Returns only once it is on disk under its final name.
    ///
    /// Written to a temporary name and then renamed, because a rename is atomic
    /// where a write is not: the file is either wholly there or not there at
    /// all, and a half-written record can never be read back as a whole one.
    @discardableResult
    public func append(_ entry: OutboxEntry) throws -> String {
        lock.lock()
        defer { lock.unlock() }

        let sequence = try nextSequence()
        let name = String(format: "%0\(Self.sequenceWidth)d-%@.json", sequence, entry.idemKey)
        let temporary = directory.appendingPathComponent(name + Self.temporarySuffix)
        let final = directory.appendingPathComponent(name)

        let data = try JSONEncoder.contract.encode(entry)
        try data.write(to: temporary, options: .atomic)
        if fileManager.fileExists(atPath: final.path) {
            try fileManager.removeItem(at: final)
        }
        try fileManager.moveItem(at: temporary, to: final)
        return name
    }

    /// Everything still waiting, oldest first.
    ///
    /// A file that will not parse is skipped and reported rather than throwing:
    /// one damaged record must not make the whole queue unsendable, which would
    /// turn a single lost event into every event lost.
    public func pending() throws -> (entries: [OutboxEntry], damaged: [String]) {
        lock.lock()
        defer { lock.unlock() }

        var entries: [OutboxEntry] = []
        var damaged: [String] = []
        for name in try fileNames() {
            let url = directory.appendingPathComponent(name)
            do {
                let data = try Data(contentsOf: url)
                entries.append(try JSONDecoder.contract.decode(OutboxEntry.self, from: data))
            } catch {
                damaged.append(name)
            }
        }
        return (entries, damaged)
    }

    /// Delete the entries the server confirmed, **one key at a time**.
    ///
    /// 决定 11: a batch may be partly accepted, so an HTTP 200 is not permission
    /// to clear the queue. Only a key this list reports as landed or as an
    /// already-applied duplicate is removed; anything rejected, and anything the
    /// server did not mention at all, stays for the next attempt.
    ///
    /// Returns how many files were removed.
    @discardableResult
    public func acknowledge(_ verdicts: [String: OutboxVerdict]) throws -> Int {
        lock.lock()
        defer { lock.unlock() }

        let removable = Set(verdicts.compactMap { key, verdict -> String? in
            switch verdict {
            case .landed, .duplicate: return key
            case .rejected: return nil
            }
        })

        var removed = 0
        for name in try fileNames() {
            // `<sequence>-<key>.json`
            let key = name
                .dropFirst(Self.sequenceWidth + 1)
                .dropLast(".json".count)
            if removable.contains(String(key)) {
                try fileManager.removeItem(at: directory.appendingPathComponent(name))
                removed += 1
            }
        }
        return removed
    }

    /// Throw away a record that cannot be parsed. Separate from `acknowledge`
    /// on purpose — discarding an event is a loss, and it should read like one
    /// at the call site.
    public func discardDamaged(_ names: [String]) throws {
        lock.lock()
        defer { lock.unlock() }
        for name in names {
            try? fileManager.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    public var count: Int {
        lock.lock()
        defer { lock.unlock() }
        return (try? fileNames().count) ?? 0
    }
}

// MARK: - JSON plumbing

extension JSONEncoder {
    /// Sorted keys so two encodings of the same event are byte-identical, which
    /// makes a file comparison in a test mean something.
    static var contract: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

extension JSONDecoder {
    static var contract: JSONDecoder { JSONDecoder() }
}

extension ISO8601DateFormatter {
    /// The format the server stores. **Both sides must agree on the separator**
    /// — the backend once compared a `T`-separated timestamp against a
    /// space-separated one in SQL, and because `'T'` sorts after `' '` the
    /// comparison was false for anything on the same day. It recovered only
    /// interruptions from a previous date, and looked fine in every test.
    static var contract: ISO8601DateFormatter {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }
}

/// A JSON value that survives a round trip without knowing its own shape.
///
/// Event payloads differ per event type and the client does not need to
/// interpret them — it stores them and hands them back. `[String: Any]` is not
/// Codable and not Sendable, so this is the smallest thing that is both.
public enum JSONValue: Codable, Equatable, Sendable {
    case string(String)
    case int(Int)
    case double(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null; return }
        if let value = try? container.decode(Bool.self) { self = .bool(value); return }
        if let value = try? container.decode(Int.self) { self = .int(value); return }
        if let value = try? container.decode(Double.self) { self = .double(value); return }
        if let value = try? container.decode(String.self) { self = .string(value); return }
        if let value = try? container.decode([JSONValue].self) { self = .array(value); return }
        self = .object(try container.decode([String: JSONValue].self))
    }

    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .int(let value): try container.encode(value)
        case .double(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }
}

extension JSONValue: ExpressibleByStringLiteral, ExpressibleByIntegerLiteral,
                     ExpressibleByBooleanLiteral {
    public init(stringLiteral value: String) { self = .string(value) }
    public init(integerLiteral value: Int) { self = .int(value) }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
}
