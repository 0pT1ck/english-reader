import Foundation

/// 事件 the events a client reports.
///
/// **The client reports what happened; it never decides what that means.** No
/// study state is computed here — whether a word enters the review queue, how
/// often it has been met, whether an article counts as read, all of that is the
/// server's (架构前提 1). These constructors exist so that event names and field
/// names are written once rather than at every call site, which is the only
/// thing about them that could drift.
///
/// **Marking is the one observation this system has.** 跨 Phase 不变量: an item
/// enters the review queue through a mark and through nothing else. A word you
/// did not recognise and did not mark is one you did not want to learn, and
/// nothing here second-guesses that.
extension OutboxEntry {
    public static func articleOpened(_ articleId: Int) -> OutboxEntry {
        OutboxEntry(kind: .reading, eventType: "article.opened",
                    payload: ["article_id": .int(articleId)])
    }

    public static func articleProgress(_ articleId: Int, sentenceSeq: Int,
                                       percent: Double) -> OutboxEntry {
        OutboxEntry(kind: .reading, eventType: "article.progress",
                    payload: ["article_id": .int(articleId),
                              "sentence_seq": .int(sentenceSeq),
                              "percent": .double(percent)])
    }

    /// **The ledger is written here and nowhere else.** Finishing is what adds
    /// an encounter; opening an article, or having its words turn up in one you
    /// never read, changes nothing. The server refuses a second finish, so
    /// replaying this from the outbox is safe.
    /// - Parameter met: **这篇里遇见了哪些词、各几次**（P9 §9）。
    ///   带着走而不是让重放去查文章:重放因此不需要文章内容，
    ///   而且更忠实——文章将来可能被重新分析过，而「我在那一句遇见过它」
    ///   是一件历史事实。用 `Encounters.met(in:)` 算出来，别手搓。
    ///   传空数组＝这条事件来自一个还不带词表的旧客户端；
    ///   服务端照旧自己数，所以老路径不受影响（铁律 5）。
    public static func articleFinished(_ articleId: Int, sentenceSeq: Int,
                                       met: [Encounters.Met] = []) -> OutboxEntry {
        var payload: [String: JSONValue] = [
            "article_id": .int(articleId),
            "sentence_seq": .int(sentenceSeq),
        ]
        if !met.isEmpty { payload["met"] = .array(met.map(\.wire)) }
        return OutboxEntry(kind: .reading, eventType: "article.finished",
                           payload: payload)
    }

    public static func wordTapped(_ headword: String, articleId: Int) -> OutboxEntry {
        OutboxEntry(kind: .reading, eventType: "word.tapped",
                    payload: ["headword": .string(headword),
                              "article_id": .int(articleId)])
    }

    /// Mark a word's sense, or a phrase.
    ///
    /// A phrase is an item in its own right and carries sense 0: not knowing
    /// `account for` says nothing about whether `account` is known, and letting
    /// one mark stand for both would drop a word the reader understands
    /// perfectly well into the review queue.
    public static func marked(_ key: String, senseId: Int = 0, kind: MarkKind,
                              itemType: String = "word",
                              articleId: Int? = nil, sentenceId: Int? = nil,
                              tokenId: Int? = nil) -> OutboxEntry {
        var payload: [String: JSONValue] = [
            "headword": .string(key),
            "item_key": .string(key),
            "item_type": .string(itemType),
            "sense_id": .int(senseId),
            "kind": .string(kind.rawValue),
        ]
        if let articleId { payload["article_id"] = .int(articleId) }
        if let sentenceId { payload["sentence_id"] = .int(sentenceId) }
        if let tokenId { payload["token_id"] = .int(tokenId) }
        return OutboxEntry(kind: .reading, eventType: "word.marked", payload: payload)
    }

    /// Take a mark back. The item leaves the review pool and returns to `new`,
    /// **but the encounter record stays** — it really was met, and when.
    public static func unmarked(_ key: String, senseId: Int = 0,
                                itemType: String = "word") -> OutboxEntry {
        OutboxEntry(kind: .reading, eventType: "word.unmarked",
                    payload: ["headword": .string(key),
                              "item_key": .string(key),
                              "item_type": .string(itemType),
                              "sense_id": .int(senseId)])
    }

    /// One review answer.
    ///
    /// **`queueId` is today's queue row, not a sense id** — and from P9 it is no
    /// longer enough on its own, so the item's identity travels with it.
    ///
    /// The queue row is a number in a table on the server that is rebuilt every
    /// morning, so last year's row 7 and this year's row 7 are different words.
    /// That was fine while the server owned the ledger and the client only
    /// posted to it. P9 makes the event log the device's first copy and derives
    /// every state from replaying it — and **a log that refers to something only
    /// another party can resolve is not a replayable log**: restore onto a new
    /// device and every answer points at nothing.
    ///
    /// So `itemType` / `itemKey` / `senseId` are added, and `queueId` stays
    /// (铁律 5 — add, never remove, and the server still keys on it today).
    /// Same shape as 「内容 id 发出去就不许变」: no identifier in the log that
    /// needs someone else to explain it.
    public static func answered(queueId: Int, passed: Bool, revealed: Int = 0,
                                sentenceId: Int? = nil, easy: Bool = false,
                                itemType: String? = nil, itemKey: String? = nil,
                                senseId: Int? = nil) -> OutboxEntry {
        var payload: [String: JSONValue] = [
            "queue_id": .int(queueId),
            "passed": .bool(passed),
            "revealed": .int(revealed),
            "easy": .bool(easy),
        ]
        if let sentenceId { payload["sentence_id"] = .int(sentenceId) }
        if let itemType { payload["item_type"] = .string(itemType) }
        if let itemKey { payload["item_key"] = .string(itemKey) }
        if let senseId { payload["sense_id"] = .int(senseId) }
        return OutboxEntry(kind: .answer, payload: payload)
    }

    /// One spelling attempt. **What was typed is stored, not just whether it
    /// was right** — that is the whole value of the record, and it is the data
    /// a future spelling feature would be built from.
    public static func spelled(_ itemKey: String, typed: String) -> OutboxEntry {
        OutboxEntry(kind: .spelling,
                    payload: ["item_key": .string(itemKey), "typed": .string(typed)])
    }
}
