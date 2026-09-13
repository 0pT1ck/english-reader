import Foundation
import Observation
import ERCore
import ERContract

/// 一次标记指向的东西：哪个词的哪个义项。
///
/// **标记是按义项记的**（决定 19），词池里存的是「`estimate` 的第 2 个义项」，
/// 不是「`estimate`」。所以本地这份覆盖也得按义项来，否则滑一个义项会把
/// 同一个词别的义项一起改掉。
struct MarkKey: Hashable {
    let headword: String
    let senseId: Int
}

@MainActor
@Observable
final class ReaderModel {
    enum Phase: Equatable {
        case loading
        case ready
        /// 离线：转圈继续转，旁边写原因（决定 33）。
        case offline(String)
        case failed(String)
        /// 服务端说这篇还没准备好。列表本来就只给准备好的（决定 32），
        /// 所以走到这里说明是缓存里那份过期了——照实说，别转圈。
        case preparing(String)
    }

    private(set) var phase: Phase = .loading
    private(set) var paragraphs: [ReaderParagraph] = []
    private(set) var display: ArticleDisplay?
    private(set) var body: Components.Schemas.ArticleBody?
    private(set) var glossary: [String: Components.Schemas.GlossaryEntry] = [:]

    /// 按 seq 取原始 token——`ArticleDisplay` 不带义项号，而标记要用它。
    private var tokensBySeq: [Int: Components.Schemas.Token] = [:]

    /// 本地还没上报的标记改动。**服务端是唯一事实来源**，这份只是覆盖层：
    /// 值为 nil 表示「这里明确取消了标记」，跟「没有本地改动」是两回事。
    private var localMarks: [MarkKey: MarkKind?] = [:]

    /// 正在点开的那个 token。
    private(set) var selectedSeq: Int?
    private(set) var lookup: LookupItem?

    /// 顶部那一段。跳回上次位置、报进度都读它。
    var topParagraph: Int?

    private var articleId: Int = 0
    private var savedSentenceSeq: Int = 0
    private var reportedOpen = false
    private(set) var finished = false

    // MARK: 载入

    func load(card: ArticleCard, app: AppModel) async {
        articleId = card.id
        guard let engine = app.engine else {
            phase = .failed("连接还没建好，先到设置里填地址和令牌")
            return
        }
        do {
            let response = try await engine.article(card.id)
            apply(response)
            if !reportedOpen {
                reportedOpen = true
                app.record(.articleOpened(card.id))
            }
        } catch let error as TransportError {
            switch error {
            case .offline:
                phase = .offline("离线，连不上服务器")
            case .server(let status, _) where status == 401:
                phase = .failed("令牌不对或已吊销，到设置里换一个")
            case .server(let status, _):
                phase = .failed("服务器出错了（HTTP \(status)）")
            case .malformed(let detail):
                phase = .failed("服务器回的东西看不懂：\(detail)")
            }
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    private func apply(_ response: Components.Schemas.ArticleResponse) {
        if let preparing = response.preparing {
            phase = .preparing("还在准备：已标注 \(preparing.annotated) / \(preparing.total)")
            return
        }
        guard let text = response.article.body else {
            phase = .failed("这篇没有正文")
            return
        }
        body = response.article
        display = ArticleDisplay(article: response)
        glossary = response.glossary?.additionalProperties ?? [:]

        let tokens = response.tokens ?? []
        tokensBySeq = Dictionary(uniqueKeysWithValues: tokens.map { ($0.seq, $0) })
        paragraphs = ArticleLayout.paragraphs(
            body: text, tokens: tokens, sentences: response.sentences ?? [])

        savedSentenceSeq = response.progress?.sentence_seq ?? 0
        finished = response.article.read_at != nil
        phase = .ready
    }

    /// 上次读到哪一段（决定 15）。没有记录就是第一段。
    var resumeParagraph: Int? {
        guard savedSentenceSeq > 0 else { return nil }
        return paragraphs.last { $0.firstSentenceSeq <= savedSentenceSeq }?.id
            ?? paragraphs.first?.id
    }

    // MARK: 标记

    /// 每个 token 当前的标记，给正文画虚线用。
    ///
    /// 服务端那份打底，本地覆盖在上面——你刚滑完滑块，正文当场就该有反应，
    /// 不该等上报成功。「标记后正文毫无反应」在 P2 是被当成 bug 记下来的。
    var marks: [Int: MarkKind] {
        guard let display else { return [:] }
        var result: [Int: MarkKind] = [:]
        for (seq, token) in tokensBySeq {
            guard let headword = token.headword else { continue }
            let key = MarkKey(headword: headword, senseId: token.sense_id ?? 0)
            if let override = localMarks[key] {
                if let kind = override { result[seq] = kind }
                continue
            }
            guard seq < display.tokens.count, let kind = display.tokens[seq].mark else {
                continue
            }
            result[seq] = kind
        }
        return result
    }

    private func currentMark(_ key: MarkKey) -> MarkKind? {
        if let override = localMarks[key] { return override }
        guard let entry = glossary[key.headword] else { return nil }
        return entry.marks.additionalProperties[String(key.senseId)]
            .flatMap(MarkKind.init(rawValue:))
    }

    // MARK: 点词

    /// 点一个词。
    ///
    /// **这个 Phase 不认词组**（方案 §6），所以这里不走 `display.target(at:)`——
    /// 那条路会把点击解析成整条词组。代价照单认下：`account for` 里的
    /// `account` 会按普通单词处理，释义可能不贴合。Core 那半边原样留着，
    /// 打开就是把这一处换回 `target(at:)`。
    func tap(seq: Int, app: AppModel) {
        guard let token = tokensBySeq[seq], let headword = token.headword,
              let entry = glossary[headword] else { return }

        selectedSeq = seq
        let senseId = token.sense_id ?? 0
        let role = (seq < (display?.tokens.count ?? 0)) ? display?.tokens[seq].role : nil

        lookup = LookupItem(
            headword: headword,
            phonetic: entry.phonetic,
            fallbackGloss: entry.translation,
            senses: LookupItem.ordered(entry.senses),
            contextSenseId: senseId,
            // 超纲词不在正文做记号，改在面板里单词右边一行小字（决定 28）。
            isBeyondSyllabus: role == .beyondSyllabus,
            mark: currentMark(MarkKey(headword: headword, senseId: senseId))
        )
        app.record(.wordTapped(headword, articleId: articleId))
    }

    func closeLookup() {
        lookup = nil
        selectedSeq = nil
    }

    /// 滑块动了。三档：认识（＝没有标记）/ 模糊 / 不认识。
    ///
    /// 「认识」不是一种记录，是**撤销标记**——撤销之后条目退回 `new`，
    /// 但遇见记录保留（它确实被遇见过）。这是跨 Phase 不变量。
    func setMark(_ kind: MarkKind?, app: AppModel) {
        guard var item = lookup else { return }
        let key = MarkKey(headword: item.headword, senseId: item.contextSenseId)
        guard currentMark(key) != kind else { return }

        localMarks[key] = .some(kind)
        item.mark = kind
        lookup = item

        if let kind {
            app.record(.marked(item.headword, senseId: item.contextSenseId, kind: kind,
                               articleId: articleId))
        } else {
            app.record(.unmarked(item.headword, senseId: item.contextSenseId))
        }
    }

    // MARK: 进度与读完

    /// 离开这一屏时报一次读到哪儿。
    ///
    /// **不每滚一段报一次**：发件箱是一条事件一个文件，滚一篇文章会写出几十个
    /// 文件，而这些只有最后一条有意义。
    func reportProgress(app: AppModel) {
        guard phase == .ready, !finished, let top = topParagraph,
              let paragraph = paragraphs.first(where: { $0.id == top }) else { return }
        let percent = paragraphs.isEmpty
            ? 0 : Double(top + 1) / Double(paragraphs.count) * 100
        app.record(.articleProgress(articleId,
                                    sentenceSeq: paragraph.firstSentenceSeq,
                                    percent: percent))
    }

    /// 「读完了」。
    ///
    /// **记账只在这一刻发生**（跨 Phase 不变量）：遇见次数 +1、文章变已学习，
    /// 全卡在这一条事件上。所以它是按钮，不是「滚到底自动算」——读完是要
    /// 记账的动作，不该由滚动手势代劳（决定 13）。
    func finish(app: AppModel) {
        guard !finished else { return }
        finished = true
        let last = paragraphs.last?.firstSentenceSeq ?? 0
        app.record(.articleFinished(articleId, sentenceSeq: last))
    }
}
