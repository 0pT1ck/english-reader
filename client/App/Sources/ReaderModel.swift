import Foundation
import Observation
import ERCore
import ERContract

/// 一次标记指向的东西：哪个词（或词组）的哪个义项。
///
/// **标记是按义项记的**（决定 19），词池里存的是「`estimate` 的第 2 个义项」，
/// 不是「`estimate`」。所以本地这份覆盖也得按义项来，否则滑一个义项会把
/// 同一个词别的义项一起改掉。**条目类型也是键的一部分**（P12）：
/// 词组 `account for` 和单词 `account` 是两个条目。
struct MarkKey: Hashable {
    let itemType: String
    let key: String
    let senseId: Int
}

/// 正文里要画记号的一段：从哪个 token 到哪个 token。
///
/// **一个词是长度为一的一段，一个词组是连着中间空格的一整段**——跨 Phase 不变量：
/// 「一处词组渲染成一个元素，两个词连同中间的空格都在里面」。
struct MarkSpan: Equatable {
    let seqs: ClosedRange<Int>
    let kind: MarkKind
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
    /// 这篇里的词组，按起点 token 取——面板要它的全部义项与标记。
    private var phrasesByStart: [Int: Components.Schemas.Phrase] = [:]
    /// 句子，按序号取——搜索结果要显示「它在哪一句」。
    private var sentencesBySeq: [Int: Components.Schemas.Sentence] = [:]

    /// 读「现在标着什么」要用设备上的重放（P12，见 `currentMark`）。
    private weak var app: AppModel?

    /// 正在点开的那一段：一个词，或者一整个词组（点任一半都选中整体）。
    private(set) var selectedSpan: ClosedRange<Int>?
    private(set) var lookup: LookupItem?

    /// 顶部那一段。跳回上次位置、报进度都读它。
    var topParagraph: Int?

    private var articleId: Int = 0

    /// 这篇里遇见了哪些词、各几次（P9 §9）。**载入时算一次**，读完那一刻
    /// 把它塞进事件——让日志自带这份，重放才不需要文章内容。
    /// 算法在 `Encounters.met(in:)`，逐条镜像服务端读完时那句 SQL。
    private var metWords: [Encounters.Met] = []
    private var savedSentenceSeq: Int = 0
    private var reportedOpen = false
    private(set) var finished = false

    // MARK: 载入

    func load(card: ArticleCard, app: AppModel) async {
        self.app = app
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
            case .upgradeRequired:
                // **不重试。** 版本不够在重新装一版之前不会变，
                // 而重试就是 P9 §17 那个一直转的圈（P11 决定 ⑲）。
                phase = .failed("这份 App 太旧了，服务端不收——请更新到最新版本")
            }
        } catch is CancellationError {
            // 同 `ReviewModel`：取消不是故障，回到 loading 让上层再试。
            phase = .loading
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

        metWords = Encounters.met(in: response)

        let tokens = response.tokens ?? []
        tokensBySeq = Dictionary(uniqueKeysWithValues: tokens.map { ($0.seq, $0) })
        phrasesByStart = Dictionary((response.phrases ?? []).map { ($0.start_seq, $0) },
                                    uniquingKeysWith: { first, _ in first })
        sentencesBySeq = Dictionary((response.sentences ?? []).map { ($0.seq, $0) },
                                    uniquingKeysWith: { first, _ in first })
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

    /// 正文上要画记号的那些段，给正文画虚线用。**只画自己标过的**（P6 决定 11、12）。
    ///
    /// 标记从设备上的重放里读（见 `currentMark`）——你刚滑完滑块，正文当场就该有反应，
    /// 不该等上报成功。「标记后正文毫无反应」在 P2 是被当成 bug 记下来的。
    ///
    /// **三种 token 三种画法**（P12）：
    /// * 在词组里的：不画它自己的，画整个词组（Core 的规则，见 `TokenDisplay.inPhrase`）；
    /// * 本句中有可信义项的：那条义项标过才画——「这一处的这个意思」；
    /// * 这一处没判出义项的（功能词、标注判「都不贴合」的）：这个词**任何一条**义项标过就画。
    ///   它是在列表里挑着标的，而这一处到底是哪条说不准；一处都不亮的话，
    ///   标完正文毫无反应，就是 P2 那个老 bug。
    /// 专有名词和没有义项的词不画——它们标不了（决定 ⑯）。
    var markSpans: [MarkSpan] {
        guard let display else { return [] }
        var spans: [MarkSpan] = []

        for phrase in display.phrases {
            let key = MarkKey(itemType: "phrase", key: phrase.phrase, senseId: phrase.senseId)
            if let kind = currentMark(key) {
                spans.append(MarkSpan(seqs: phrase.startSeq...phrase.endSeq, kind: kind))
            }
        }

        for (seq, token) in tokensBySeq {
            guard seq < display.tokens.count, !display.tokens[seq].inPhrase,
                  let headword = token.headword, let entry = glossary[headword] else { continue }
            let target = MarkTarget.resolve(kind: token.kind, senseId: token.sense_id,
                                            senseIds: entry.senses.map(\.id), inPhrase: false)
            let kind: MarkKind?
            switch target {
            case .contextSense(let senseId):
                kind = currentMark(MarkKey(itemType: "word", key: headword, senseId: senseId))
            case .pickFromList:
                kind = wordMarks(headword).values.first
            case .properNoun, .noSenses:
                kind = nil
            }
            if let kind { spans.append(MarkSpan(seqs: seq...seq, kind: kind)) }
        }
        return spans
    }

    /// 现在标着什么。**先问设备上的重放，它没见过这个条目才看文章里带的那份。**
    ///
    /// 2026-09-23 模拟器上撞出来的：文章是整篇缓存的（`SyncEngine.article` 有缓存就不再拉），
    /// 它带着的「你标过什么」是**下载那一刻**的服务端快照；原先刚标的那一下只存在这一屏的
    /// 内存覆盖层里，**退出文章就没了**——标一个词、退出、再进来，虚线不见了。
    /// 而学习记录的第一副本本来就在设备上（P9 架构铁律），`record()` 写完事件当场重放，
    /// 所以问它就是对的，覆盖层也就不需要了（它还会在事件没写进去时照样显示「标上了」）。
    ///
    /// 重放**有这个条目**（标过、哪怕后来撤了）就以它为准；没有才退回文章快照——
    /// 那是刚装好、还没同步过的情形。
    private func currentMark(_ key: MarkKey) -> MarkKind? {
        if let item = app?.projection.items[
            Projection.Key(itemType: key.itemType, key: key.key, senseId: key.senseId)] {
            if item.marks.contains(.unknown) { return .unknown }
            return item.marks.contains(.fuzzy) ? .fuzzy : nil
        }
        let raw: [String: String]?
        if key.itemType == "phrase" {
            raw = phrasesByStart.values.first { $0.phrase == key.key }?.marks?.additionalProperties
        } else {
            raw = glossary[key.key]?.marks.additionalProperties
        }
        return raw?[String(key.senseId)].flatMap(MarkKind.init(rawValue:))
    }

    /// 一个词在各条**真**义项上的标记（服务端那份 ＋ 本地覆盖）。
    /// 义项号 ≤ 0 的老标记不算——它们指不到任何一行，也进不了复习（决定 ⑯）。
    private func wordMarks(_ headword: String) -> [Int: MarkKind] {
        guard let entry = glossary[headword] else { return [:] }
        var out: [Int: MarkKind] = [:]
        for sense in entry.senses {
            if let kind = currentMark(MarkKey(itemType: "word", key: headword, senseId: sense.id)) {
                out[sense.id] = kind
            }
        }
        return out
    }

    // MARK: 点词

    /// 点一个 token。**走 Core 的 `target(at:)`**（P12 起）：点词组的任一半，
    /// 选中的都是整个词组——跨 Phase 不变量。P6 起这里绕开了它（「界面初版不认词组」，
    /// P6 §7），那笔账这个 Phase 还掉。
    func tap(seq: Int, app: AppModel) {
        guard let display, let target = display.target(at: seq),
              let token = tokensBySeq[seq] else { return }
        let role = seq < display.tokens.count ? display.tokens[seq].role : nil

        switch target {
        case .phrase(let phrase):
            guard let payload = phrasesByStart[phrase.startSeq],
                  let subject = phraseSubject(payload, contextSenseId: phrase.senseId) else { return }
            selectedSpan = phrase.startSeq...phrase.endSeq
            lookup = LookupItem(
                subject: subject,
                // 「这个词本身」：词组里被点的那个词，**永远是挑着标**——
                // 它自己的标注是在不知道自己在词组里的情况下做的（决定 ②）。
                inner: wordSubject(token, inPhrase: true),
                isBeyondSyllabus: false,
                selection: subject.contextSenseId.map { .init(inner: false, senseId: $0) }
            )
        case .word:
            guard let subject = wordSubject(token, inPhrase: false) else { return }
            selectedSpan = seq...seq
            lookup = LookupItem(
                subject: subject,
                inner: nil,
                // 超纲词不在正文做记号，改在面板里单词右边一行小字（决定 28）。
                isBeyondSyllabus: role == .beyondSyllabus,
                selection: subject.contextSenseId.map { .init(inner: false, senseId: $0) }
            )
        }
        if let headword = token.headword {
            app.record(.wordTapped(headword, articleId: articleId))
        }
    }

    private func wordSubject(_ token: Components.Schemas.Token, inPhrase: Bool) -> LookupSubject? {
        guard let headword = token.headword, let entry = glossary[headword] else { return nil }
        let target = MarkTarget.resolve(kind: token.kind, senseId: token.sense_id,
                                        senseIds: entry.senses.map(\.id), inPhrase: inPhrase)
        return LookupSubject(
            kind: .word,
            key: headword,
            title: target == .properNoun ? token.surface : headword,
            phonetic: entry.phonetic,
            // **专有名词不给普通词的意思**（决定 ⑯）：David Green 的 Green 不是「绿色的」。
            senses: target == .properNoun ? [] : SenseRow.ordered(entry.senses.map(SenseRow.init)),
            fallbackGloss: target == .properNoun ? nil : entry.translation,
            target: target,
            marks: wordMarks(headword)
        )
    }

    private func phraseSubject(_ phrase: Components.Schemas.Phrase,
                               contextSenseId: Int) -> LookupSubject? {
        let senses = SenseRow.ordered((phrase.senses ?? []).map(SenseRow.init))
        guard !senses.isEmpty else { return nil }
        let ids = senses.map(\.id)
        // 词组的「这一处是哪条」是标注器看着句子判的（P11 §7，语境判定折进标注），可信；
        // 不在列表里的号照样按「挑着标」处理，同一条规则。
        let target: MarkTarget = ids.contains(contextSenseId) ? .contextSense(contextSenseId)
                                                               : .pickFromList
        var marks: [Int: MarkKind] = [:]
        for id in ids {
            if let kind = currentMark(MarkKey(itemType: "phrase", key: phrase.phrase, senseId: id)) {
                marks[id] = kind
            }
        }
        return LookupSubject(kind: .phrase, key: phrase.phrase, title: phrase.phrase,
                             phonetic: nil, senses: senses,
                             fallbackGloss: nil, target: target, marks: marks)
    }

    /// 在列表里点了一行：滑块改标这一条（决定 ⑯）。
    func select(_ selection: LookupItem.Selection) {
        guard var item = lookup else { return }
        let owner = selection.inner ? item.inner : item.subject
        guard owner?.isMarkable == true else { return }
        item.selection = selection
        lookup = item
    }

    func closeLookup() {
        lookup = nil
        selectedSpan = nil
    }

    /// 滑块动了。三档：认识（＝没有标记）/ 模糊 / 不认识。标的是**被选中的那一条**。
    ///
    /// 「认识」不是一种记录，是**撤销标记**——撤销之后条目退回 `new`，
    /// 但遇见记录保留（它确实被遇见过）。这是跨 Phase 不变量。
    func setMark(_ kind: MarkKind?, app: AppModel) {
        guard var item = lookup, let selection = item.selection,
              let owner = item.selectedSubject, owner.isMarkable,
              owner.senses.contains(where: { $0.id == selection.senseId }) else { return }
        // 上面那一行守着决定 ⑯：**标记必须落在一条真实的义项上**——
        // 专有名词、没有义项的、没选中任何一行的，走不到这里。
        let key = MarkKey(itemType: owner.itemType, key: owner.key, senseId: selection.senseId)
        guard currentMark(key) != kind else { return }

        if let kind {
            app.record(.marked(owner.key, senseId: selection.senseId, kind: kind,
                               itemType: owner.itemType, articleId: articleId))
        } else {
            app.record(.unmarked(owner.key, senseId: selection.senseId, itemType: owner.itemType))
        }
        // **面板上显示的是记下之后重放出来的结果，不是想要的结果**——
        // 事件没写进去的话，这里照实显示没标上。
        let now = currentMark(key)
        if selection.inner { item.inner?.marks[selection.senseId] = now }
        else { item.subject.marks[selection.senseId] = now }
        lookup = item
    }

    // MARK: 文内搜索（P12 决定 ㉑）

    /// 一处搜到的地方。
    struct SearchHit: Identifiable, Equatable {
        /// 起点 token 的序号，同时当 id——一处一行。
        let id: Int
        let seqs: ClosedRange<Int>
        /// 在第几段（跳过去用）。
        let paragraph: Int
        /// 它所在的整句，以及它在这句里的位置（加粗用，按字符算）。
        let sentence: String
        let highlight: Range<Int>?
    }

    /// 在这篇里找一个词。
    ///
    /// **两条都算搜到**：原文里的写法以它开头（边打边出结果），或者词形还原之后
    /// 正好是它——所以搜 `go` 找得到 `went`、搜 `child` 找得到 `children`。
    /// **带空格就当词组搜**，对的是这篇里已确认的词组。大小写不分。
    func search(_ raw: String) -> [SearchHit] {
        let query = raw.trimmingCharacters(in: .whitespaces).lowercased()
        guard !query.isEmpty else { return [] }

        var spans: [ClosedRange<Int>] = []
        if query.contains(" ") {
            for phrase in display?.phrases ?? [] where phrase.phrase.lowercased().hasPrefix(query) {
                spans.append(phrase.startSeq...phrase.endSeq)
            }
        } else {
            for (seq, token) in tokensBySeq where token.kind != "nonword" {
                if token.surface.lowercased().hasPrefix(query)
                    || token.headword?.lowercased() == query {
                    spans.append(seq...seq)
                }
            }
        }
        return spans.sorted { $0.lowerBound < $1.lowerBound }.compactMap(hit(for:))
    }

    private func hit(for seqs: ClosedRange<Int>) -> SearchHit? {
        guard let first = tokensBySeq[seqs.lowerBound], let last = tokensBySeq[seqs.upperBound],
              let paragraph = paragraphs.first(where: { p in
                  p.tokens.contains { $0.seq == seqs.lowerBound }
              }) else { return nil }
        let sentence = sentencesBySeq[first.sentence_seq]
        let text = sentence?.text ?? ""
        var highlight: Range<Int>?
        if let sentence {
            // 服务端的下标是 Unicode 码位（同 `ArticleLayout`），这里也按码位算，
            // 再交给视图按 `Character` 用——英文里两者一致。
            let lower = first.char_start - sentence.char_start
            let upper = last.char_end - sentence.char_start
            if lower >= 0, upper <= text.unicodeScalars.count, lower < upper {
                highlight = lower..<upper
            }
        }
        return SearchHit(id: seqs.lowerBound, seqs: seqs, paragraph: paragraph.id,
                         sentence: text, highlight: highlight)
    }

    /// 某个 token 在原文里的写法。
    func surface(at seq: Int) -> String? { tokensBySeq[seq]?.surface }

    /// 跳过去之后给那一处加底色。**留到下一次点别处为止**——
    /// 跟点词的底色是同一种，不另发明一种记号（决定 ⑮：新东西照现有样式画）。
    func highlight(_ seqs: ClosedRange<Int>) {
        lookup = nil
        selectedSpan = seqs
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
        app.record(.articleFinished(articleId, sentenceSeq: last, met: metWords))
    }
}
