import Foundation
import ERContract

/// 遇见 encounters / 记账 the ledger
///
/// **记账只在读完那一刻发生。** 打开一篇文章、或者它的词出现在你没读的那一篇里，
/// 什么都不改。这是跨 Phase 不变量，服务端那边由 `verify_phase2.py` 7.1／7.4 守着。
///
/// **这里算的是「这篇里遇见了哪些词、各几次」，然后让「读完」那条事件带着它走。**
/// P9 §9 定的。两个好处，第二个比第一个重要：
///
/// * 重放不需要文章内容——不然换台设备从备份恢复，得把读过的文章全下回来才算得出
///   遇见次数；
/// * **更忠实。** 「我在第 480 篇第 12 句遇见过 `municipal`」是一件历史事实，
///   而文章将来可能被重新分析过。把它冻在事件里，比事后拿一篇可能已经变了的文章
///   去推它要准确。不变量那句「撤销标记后遇见记录保留——它确实被遇见过」
///   说的就是这个意思。
public enum Encounters {

    /// 一个词在这篇文章里被遇见了几次。
    public struct Met: Sendable, Equatable {
        public let itemKey: String
        public let senseId: Int
        /// 出现次数。**加的是这个数，不是加一**——同服务端那句 `COUNT(*) AS n`。
        public let count: Int
        /// 最早出现在第几句（篇内序号）。
        ///
        /// **不是服务端的 `sentence_id`。** token 上带的是篇内序号
        /// （`sentence_seq`），而服务端那张表用的是行号——客户端给不出行号，
        /// 所以这里给序号，并且**不拿它去填「首次出现在哪一句」**：
        /// 那个值是标记那一刻从标记事件里来的，不是读完时推的。
        public let firstSentenceSeq: Int?

        public init(itemKey: String, senseId: Int, count: Int,
                    firstSentenceSeq: Int? = nil) {
            self.itemKey = itemKey
            self.senseId = senseId
            self.count = count
            self.firstSentenceSeq = firstSentenceSeq
        }
    }

    /// 这篇文章里遇见了哪些词。
    ///
    /// **逐条镜像服务端那句 SQL**，一条都不是这里新定的：
    ///
    /// * `kind == "content"` **或** `is_target` —— 后半句不能省。
    ///   目标词可能落在一个被 spaCy 标成 PROPN 的 token 上（要的是 `latin`，
    ///   模型写了 `Latin`），而「它是这篇要教的词」是更强的信号。
    ///   **只按 kind 过滤的话，二十五个目标词里会静默丢一个。**
    /// * `headword` 非空。
    /// * 按 `(headword, sense_id ?? 0)` 分组。**NULL 折成 0**，也就是词一级那个槽；
    ///   这只在文章已经标注完时才安全（`ready` 的文章上每个实词都标过了，
    ///   剩下的 NULL 属于标注本来不适用的 token），而客户端拿到的文章只会是
    ///   `ready` 的——`preparing` 的那些根本不入缓存。
    /// * 次数是出现次数，句号取最小。
    ///
    /// **词组不在里面。** 服务端那句 SQL 查的是 `reading_tokens`，
    /// 而词组在另一张表、读完那一刻不记它。
    public static func met(in article: Components.Schemas.ArticleResponse) -> [Met] {
        var counts: [ArticleKey: (count: Int, firstSentence: Int?)] = [:]
        for token in article.tokens ?? [] {
            guard token.kind == "content" || token.is_target else { continue }
            guard let headword = token.headword, !headword.isEmpty else { continue }
            let key = ArticleKey(headword: headword, senseId: token.sense_id ?? 0)
            let sentence = token.sentence_seq
            let existing = counts[key]
            counts[key] = (
                count: (existing?.count ?? 0) + 1,
                firstSentence: [existing?.firstSentence, sentence]
                    .compactMap { $0 }.min()
            )
        }
        return counts
            .map { Met(itemKey: $0.key.headword, senseId: $0.key.senseId,
                       count: $0.value.count, firstSentenceSeq: $0.value.firstSentence) }
            // 稳定的顺序，事件的字节才是可复现的——同一次阅读产生同一条事件。
            .sorted { ($0.itemKey, $0.senseId) < ($1.itemKey, $1.senseId) }
    }

    private struct ArticleKey: Hashable {
        let headword: String
        let senseId: Int
    }
}

extension Encounters.Met {
    /// 放进事件 payload 的那一份。
    var wire: JSONValue {
        var fields: [String: JSONValue] = [
            "item_key": .string(itemKey),
            "sense_id": .int(senseId),
            "n": .int(count),
        ]
        if let firstSentenceSeq { fields["first_sentence_seq"] = .int(firstSentenceSeq) }
        return .object(fields)
    }
}
