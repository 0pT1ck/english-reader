import Foundation
import Testing
@testable import ERCore

/// 客户端日志的测试 tests for the client's own log
///
/// Two of these matter more than the rest. **Rotation is checked in both
/// directions** — what must be gone and what must still be there — because a
/// prune that deletes everything passes a one-sided test just as happily as a
/// correct one (坑 §4.3). And **the redaction test is the one that protects a
/// person**: this file gets exported and sent to someone.
struct LogTests {
    /// A clock the test can move. A plain `var` captured by the closure is not
    /// `Sendable`; a reference type that says it checks itself is.
    final class Clock: @unchecked Sendable {
        var now: Date
        init(_ now: Date) { self.now = now }
    }

    static func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("log-test-\(UUID().uuidString)")
    }

    static func day(_ text: String) -> Date {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        formatter.timeZone = TimeZone.current
        return formatter.date(from: text)!
    }

    @Test func writesAndReadsBackNewestFirst() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let clock = Clock(Self.day("2026-09-15 09:00:00"))
        let log = try FileLog(directory: directory, clock: { clock.now })

        log.write(.info, "app.launched", "起来了")
        clock.now = Self.day("2026-09-15 09:00:01")
        log.write(.error, "sync.drain.failed", "发件箱没送出去", traceId: "abc123")

        let entries = log.entries()
        #expect(entries.count == 2)
        #expect(entries.first?.event == "sync.drain.failed")
        #expect(entries.first?.traceId == "abc123")
        #expect(entries.last?.message == "起来了")
    }

    @Test func filtersByLevel() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try FileLog(directory: directory)
        log.minimumLevel = .debug

        log.write(.debug, "cache.hit", "命中")
        log.write(.info, "article.opened", "打开了一篇")
        log.write(.warn, "transport.offline", "联不上")

        #expect(log.entries(minimum: .debug).count == 3)
        #expect(log.entries(minimum: .warn).map(\.event) == ["transport.offline"])
    }

    @Test func debugIsOffUntilAskedFor() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try FileLog(directory: directory)

        // Default threshold is INFO: chasing something is a decision, not the
        // normal state of the app.
        log.write(.debug, "cache.hit", "命中")
        #expect(log.entries(minimum: .debug).isEmpty)
    }

    @Test func keepsThreeDaysAndDropsTheFourth() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let clock = Clock(Self.day("2026-09-12 10:00:00"))
        let log = try FileLog(directory: directory, retentionDays: 3, clock: { clock.now })

        for date in ["2026-09-12", "2026-09-13", "2026-09-14", "2026-09-15"] {
            clock.now = Self.day("\(date) 10:00:00")
            log.write(.info, "day.marker", date)
        }
        #expect(log.files.count == 4)

        // Standing on the 15th, three days means the 13th, 14th and 15th.
        let removed = try log.prune()
        #expect(removed == 1)

        let names = log.files.map { $0.deletingPathExtension().lastPathComponent }
        // Both halves on purpose: the 12th must be gone **and** the other three
        // must still be here. A prune that wiped the directory passes the first
        // check alone.
        #expect(!names.contains("2026-09-12"))
        #expect(names == ["2026-09-13", "2026-09-14", "2026-09-15"])
    }

    @Test func prunesOnLaunch() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let clock = Clock(Self.day("2026-09-10 10:00:00"))
        do {
            let log = try FileLog(directory: directory, retentionDays: 3, clock: { clock.now })
            log.write(.info, "day.marker", "old")
            #expect(log.files.count == 1)
        }

        // Reopening a week later is what a relaunch looks like. Nothing else
        // in this app runs on a timer, so launch is the only moment rotation
        // can happen.
        clock.now = Self.day("2026-09-17 10:00:00")
        let reopened = try FileLog(directory: directory, retentionDays: 3, clock: { clock.now })
        #expect(reopened.files.isEmpty)
    }

    @Test func neverWritesACredential() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try FileLog(directory: directory)

        log.write(.warn, "transport.rejected", "服务器拒收", fields: [
            "token": "abcdef0123456789",
            "Authorization": "Bearer abcdef0123456789",
            "admin_secret": "hunter2",
            "status": "401",
        ])

        let text = log.exportText()
        // The export is the artefact that leaves the device, so it is the one
        // that gets searched — not the in-memory entry.
        #expect(!text.contains("abcdef0123456789"))
        #expect(!text.contains("hunter2"))
        #expect(text.contains("status=401"))
        // Redacted rather than dropped: "withheld" and "there was nothing" are
        // different facts.
        #expect(text.contains("token=••••"))
    }

    @Test func survivesAHalfWrittenLine() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let clock = Clock(Self.day("2026-09-15 09:00:00"))
        let log = try FileLog(directory: directory, clock: { clock.now })
        log.write(.info, "app.launched", "起来了")

        // A crash mid-write leaves exactly this, and it is also exactly when
        // someone wants to read the log.
        let file = log.files[0]
        let handle = try FileHandle(forWritingTo: file)
        try handle.seekToEnd()
        try handle.write(contentsOf: Data(#"{"at":"2026-09-15T09:00"#.utf8))
        try handle.close()

        let entries = log.entries()
        #expect(entries.count == 1)
        #expect(entries.first?.event == "app.launched")
    }

    @Test func exportIsOldestFirstAndReadable() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let clock = Clock(Self.day("2026-09-15 08:00:00"))
        let log = try FileLog(directory: directory, clock: { clock.now })

        log.write(.info, "first", "一")
        clock.now = Self.day("2026-09-15 08:00:05")
        log.write(.info, "second", "二")

        let lines = log.exportText().split(separator: "\n")
        #expect(lines.count == 2)
        // Reading order, not query order: a document is read downwards.
        #expect(lines[0].contains("first"))
        #expect(lines[1].contains("second"))
        #expect(lines[0].contains("[INFO]"))
        #expect(lines[0].contains("2026-09-15 08:00:00"))
    }

    @Test func clearRemovesEverything() throws {
        let directory = Self.temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let log = try FileLog(directory: directory)
        log.write(.info, "app.launched", "起来了")
        try log.clear()
        #expect(log.files.isEmpty)
        #expect(log.entries().isEmpty)
    }
}
