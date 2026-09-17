import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import ERContract
import ERCore

/// `ercli` — P5 的验收工具，不是产品。
///
/// **Why a command-line client exists at all.** The core is a library, and a
/// library cannot be *used*. 主文档 §M records what happens without one:
/// `/today` was verified to return the right package and was never walked by a
/// real flow, so nobody knew whether the fields were enough. This is the flow.
///
/// **It renders the annotations rather than printing plain text**, because
/// plain text would exercise less than half the core — token positions, phrases
/// as single things, and which gloss a word inside a phrase gets are the parts
/// most likely to be wrong, and they are invisible unless drawn.
///
/// Three jobs, from the plan: walk a day for acceptance, walk one with the
/// network deliberately gone, and compare what the client computed against what
/// the server says while online.

// MARK: - Setup

let arguments = Array(CommandLine.arguments.dropFirst())

func flag(_ name: String) -> Bool { arguments.contains("--\(name)") }

func value(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: "--\(name)"),
          index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}

/// The positional arguments — the command and whatever follows it.
///
/// **An option's value is not a positional.** `ercli library --state ./.ercli`
/// read `./.ercli` as the source and asked the server for articles from a
/// shelf by that name, which came back empty and looked like an empty shelf
/// rather than a parsing mistake. Caught by running it; nothing about the
/// output said "bad argument".
let positionals: [String] = {
    var out: [String] = []
    var skipNext = false
    for argument in arguments {
        if skipNext { skipNext = false; continue }
        if argument.hasPrefix("--") {
            // These take a value; the rest are plain flags.
            skipNext = ["--server", "--token", "--state", "--answers", "--clear"]
                .contains(argument)
            continue
        }
        out.append(argument)
    }
    return out
}()

if flag("no-color") { Ink.enabled = false }

let stateDirectory = URL(fileURLWithPath:
    value("state") ?? FileManager.default.currentDirectoryPath + "/.ercli")

let defaultBase = "http://127.0.0.1:8000"
let baseURL = URL(string: value("server") ?? defaultBase)!
let token = value("token")
    ?? ProcessInfo.processInfo.environment["ER_DEVICE_TOKEN"]
    ?? ""

func usage() {
    print("""
    \(Ink.bold("ercli")) — English Reader 的命令行客户端（P5 的验收工具）

    \(Ink.bold("用法"))
      ercli day                拉今日包并列出今天要读、要复习的东西
      ercli library [来源]     书架：不带参数看生成文，cet4 / cet6 / kaoyan 看真题
      ercli read <编号>        读一篇：正文带标注，可以查词、标记、读完
      ercli look <编号> <序号> 查一个词（序号就是正文里 ⟨n⟩ 那个数字）
      ercli mark <编号> <序号> [--fuzzy]   标记；对词组会整体标记
      ercli unmark <编号> <序号>   撤销标记
      ercli finish <编号>      读完，记账
      ercli review             走一遍今天的复习
      ercli spell              复习走完之后的拼写强化
      ercli sync               把发件箱里的东西报上去
      ercli cache              看缓存占了多少，清理
      ercli walk               \(Ink.bold("从头到尾走完一天"))——验收用

    \(Ink.bold("选项"))
      --server <地址>   默认 \(defaultBase)
      --token <令牌>    也可以用环境变量 ER_DEVICE_TOKEN
      --state <目录>    本地数据放哪，默认 ./.ercli
      --offline         \(Ink.bold("不连服务器"))，只用缓存——验离线那一半
      --no-color        不上色
      --compare         在线作答时把客户端算的和服务端回的对一遍
      --answers 1,3,2   脚本喂作答，让 walk 能无人值守地把复习也走完
    """)
}

// MARK: - Wiring

let outbox = try Outbox(directory: stateDirectory.appendingPathComponent("outbox"))
// 事件日志（P9）。**这五种事件从这儿上报，不从发件箱**——发件箱只剩遥测。
let events = try EventLog(directory: stateDirectory.appendingPathComponent("events"))

/// 记一条事件。**一条事件只有一个家**：五种进日志，遥测进发件箱。
/// 映射那一份在 Core 里（`OutboxEntry.loggedKind`），和 App 共用——
/// 两份判断早晚会在某一种事件上分家，而那时两边都不会报错。
func record(_ entry: OutboxEntry) throws {
    if let kind = entry.loggedKind {
        try events.append(kind: kind, payload: entry.payload,
                          idemKey: entry.idemKey, occurredAt: entry.occurredAt)
    } else {
        try record(entry)
    }
}
let articleCache = try ArticleCache(directory: stateDirectory.appendingPathComponent("articles"))
let dayCache = try DayCache(directory: stateDirectory.appendingPathComponent("day"))

let transport: any Transport = flag("offline")
    ? OfflineTransport()
    : URLSessionTransport(baseURL: baseURL, token: token)
let engine = SyncEngine(transport: transport, outbox: outbox, events: events,
                        articles: articleCache, day: dayCache)

if let warning = ProxyEnvironment.warning() { print(Ink.yellow(warning)) }

func requireToken() {
    guard token.isEmpty, !flag("offline") else { return }
    print(Ink.red("没有设备令牌。在管理控制台注册一台设备，然后 --token 或者 ER_DEVICE_TOKEN。"))
    exit(2)
}

// MARK: - Commands

func showDay() async throws {
    requireToken()
    let package = try await engine.fetchDay()
    print(Ink.bold("今天") + Ink.dim("（\(package.decoded.day ?? "—")）"))
    for article in package.articles {
        let meta = article.article
        let read = meta.read_at != nil ? Ink.green("已读") : Ink.dim("未读")
        print("  \(Ink.bold("#\(meta.id)")) \(meta.title)"
            + Ink.dim("  \(meta.word_count ?? 0) 词  \(read)"))
    }
    if package.decoded.extra_articles.isEmpty {
        // 加餐已于 2026-09-12 取消——往期本身就是加餐。字段还在是因为铁律 5 不删字段。
        print(Ink.dim("  （没有加餐。想再读就翻往期：ercli day --shelf archive）"))
    }
    let reviews = package.reviews
    let open = reviews.items.filter { !$0.done }.count
    print(Ink.bold("复习") + Ink.dim("  \(open) 条待做 / 共 \(reviews.items.count) 条"))
    if !package.excludesExamPapers {
        print(Ink.yellow("  服务端说今日包含真题了？契约变了，客户端要跟着改"))
    } else {
        print(Ink.dim("  今日包不含真题——真题在 /library?source=cet4|cet6|kaoyan"))
    }
    print(Ink.dim("发件箱里还有 \(outbox.count) 条没报上去"))
}

func read(_ id: Int) async throws {
    let article = try await engine.article(id)
    if let preparing = article.preparing {
        // 服务端对没标注完的文章回的是进度而不是错误——问它是正常的，不是犯错。
        print(Ink.yellow("这篇还在准备：\(preparing.status)"
            + "（\(preparing.annotated)/\(preparing.total)）"))
        return
    }
    let renderer = ArticleRenderer(article)
    print(Ink.bold(article.article.title))
    print(Ink.dim(String(repeating: "─", count: 60)))
    print(ArticleRenderer.legend())
    print(Ink.dim(String(repeating: "─", count: 60)))
    print(renderer.body())
    print(Ink.dim(String(repeating: "─", count: 60)))
    let display = renderer.display
    print(Ink.dim("目标词 \(display.tokens.filter { $0.role == .target }.count) 个，"
        + "超纲 \(display.tokens.filter { $0.role == .beyondSyllabus }.count) 处，"
        + "词组 \(display.phrases.count) 处"))
    try record(.articleOpened(id))
}

func look(_ id: Int, _ seq: Int) async throws {
    let article = try await engine.article(id)
    let renderer = ArticleRenderer(article)
    print(renderer.panel(at: seq))
    if case .word(let headword, _)? = renderer.display.target(at: seq) {
        try record(.wordTapped(headword, articleId: id))
    }
}

func mark(_ id: Int, _ seq: Int, fuzzy: Bool) async throws {
    let article = try await engine.article(id)
    let display = ArticleDisplay(article: article)
    let kind: MarkKind = fuzzy ? .fuzzy : .unknown

    switch display.target(at: seq) {
    case .phrase(let phrase):
        // 词组是一个整体，就按一个整体标记——点它的任何一半都一样。
        try record(.marked(phrase.phrase, kind: kind, itemType: "phrase",
                                  articleId: id))
        print(Ink.red("标记词组：\(phrase.phrase)") + Ink.dim("（\(kind.rawValue)）"))
    case .word(let headword, _):
        let senseId = (article.tokens ?? [])
            .first { $0.seq == seq }?.sense_id ?? 0
        try record(.marked(headword, senseId: senseId, kind: kind, articleId: id))
        print(Ink.red("标记：\(headword)") + Ink.dim("（义项 \(senseId)，\(kind.rawValue)）"))
    case nil:
        print(Ink.dim("⟨\(seq)⟩ 这里没有可标记的东西"))
    }
}

/// Take a mark back.
///
/// 跨 Phase 不变量: the item drops out of the review pool and returns to `new`,
/// **but the encounter record stays** — it really was met, and when. Those are
/// two different facts and only one of them is being withdrawn.
func unmark(_ id: Int, _ seq: Int) async throws {
    let article = try await engine.article(id)
    let display = ArticleDisplay(article: article)
    switch display.target(at: seq) {
    case .phrase(let phrase):
        try record(.unmarked(phrase.phrase, itemType: "phrase"))
        print(Ink.green("撤销词组标记：\(phrase.phrase)"))
    case .word(let headword, _):
        let senseId = (article.tokens ?? []).first { $0.seq == seq }?.sense_id ?? 0
        try record(.unmarked(headword, senseId: senseId))
        print(Ink.green("撤销标记：\(headword)") + Ink.dim("（义项 \(senseId)）"))
    case nil:
        print(Ink.dim("⟨\(seq)⟩ 这里没有可撤销的东西"))
    }
    print(Ink.dim("条目退回 new，但遇见记录保留——它确实被遇见过"))
}

func finish(_ id: Int) async throws {
    let article = try await engine.article(id)
    let sentences = article.sentences?.count ?? 0
    // 记账只在读完这一刻发生，而且只发生一次——服务端也会拒绝第二次。
    try record(.articleFinished(id, sentenceSeq: max(0, sentences - 1)))
    print(Ink.green("读完 #\(id)")
        + Ink.dim("——遇见次数 +1，词池位置不动；标记过的才进复习队列"))
}

func sync() async throws {
    requireToken()
    let report = try await engine.drain()
    if report.offline {
        print(Ink.yellow("没连上服务器。发件箱留着 \(report.remaining) 条，联网了再报。"))
        return
    }
    print(Ink.green("上报完成")
        + "：收下 \(report.landed)，重复 \(report.duplicates)，"
        + "失败 \(report.rejected)，还剩 \(report.remaining)")
    if report.remaining > 0 {
        print(Ink.dim("剩下的是服务端没确认的——只删它明确说收下的那几条（决定 11）"))
    }
}

/// 书架。**今日包不是今天能读的全部**——真题走这条路（决定 19），
/// 而往期生成文也在这里：备好没读的会一直堆着，往下翻就是「还想读」时的供给。
func library(_ source: String?) async throws {
    requireToken()
    let shelf = source == nil ? "fresh" : "all"
    let list = try await engine.library(shelf: shelf, source: source)
    let label = source.map { ["cet4": "四级真题", "cet6": "六级真题",
                              "kaoyan": "考研真题"][$0] ?? $0 } ?? "新备的生成文"
    print(Ink.bold(label) + Ink.dim("  \(list.articles.count) 篇"))
    for item in list.articles.prefix(30) {
        let read = item.read_at != nil ? Ink.green("已读") : Ink.dim("未读")
        let difficulty = item.difficulty.map {
            Ink.dim("超四级 \(String(format: "%.1f", $0.beyond_cet4_pct))%")
        } ?? ""
        print("  \(Ink.bold("#\(item.id)")) \(item.title)"
            + Ink.dim("  \(item.word_count) 词  ") + difficulty + "  " + read)
    }
    if list.articles.count > 30 {
        print(Ink.dim("  …还有 \(list.articles.count - 30) 篇"))
    }
    print(Ink.dim("排序方式：" + list.sortable.additionalProperties.keys.sorted()
        .joined(separator: " / ")))
}

func showCache() throws {
    let sizes = articleCache.bodySizes()
    let total = sizes.values.reduce(0, +)
    print(Ink.bold("缓存") + Ink.dim("  \(sizes.count) 篇正文，共 \(total / 1024) KB"))
    for meta in articleCache.articles() {
        let body = meta.hasBody
            ? Ink.green("有正文 \((sizes[meta.id] ?? 0) / 1024) KB")
            : Ink.dim("只有元信息")
        print("  #\(meta.id) \(meta.title)  \(body)"
            + Ink.dim("  读到 \(Int(meta.percent))%"))
    }
    print(Ink.dim("发件箱 \(outbox.count) 条——\(Ink.bold("清理缓存不会碰它"))"))

    if let target = value("clear") {
        let removed = target == "all"
            ? try articleCache.clearAllBodies()
            : try articleCache.clearBodies(target.split(separator: ",").compactMap { Int($0) })
        print(Ink.green("清掉 \(removed) 篇的正文")
            + Ink.dim("。标题、字数、进度都还在；点开会重新拉。发件箱 \(outbox.count) 条，一条没动。"))
    }
}

// MARK: - Review

func review() async throws {
    let package = try await engine.fetchDay()
    let day = package.reviews
    var states: [Int: ReviewItemState] = [:]
    var cards: [Int: Components.Schemas.ReviewItem] = [:]
    for item in day.items {
        cards[item.queue_id] = item
        states[item.queue_id] = ReviewItemState(
            direction: ReviewDirection(rawValue: item.direction) ?? .wordToSense,
            asks: item.asks, misses: item.misses, weight: item.weight, done: item.done)
    }

    let draw = ReviewDraw()
    var generator = SystemRandomNumberGenerator()
    var asked = 0
    let compare = flag("compare")
    var disagreements = 0

    print(Ink.bold("今天的复习") + Ink.dim("  \(states.values.filter { !$0.done }.count) 条"))

    while let queueId = draw.pick(
        from: Array(states.keys).sorted(),
        weight: { states[$0]!.weight },
        isOpen: { !states[$0]!.done },
        random: Double.random(in: 0..<1, using: &generator)
    ) {
        guard let card = cards[queueId], let state = states[queueId] else { break }
        asked += 1
        let asked = askedSentence(card, state.direction)
        print(Ink.dim(String(repeating: "─", count: 50)))
        print(Ink.dim("⟨\(queueId)⟩ ") + question(card, asked, state.direction))

        let answer = promptAnswer(card, asked, state.direction)
        let next = state.applying(answer, weightDecay: day.weight_decay)
        states[queueId] = next

        try record(.answered(queueId: queueId, passed: answer.passed,
                                    revealed: answer.revealed,
                                    sentenceId: asked?.id, easy: answer.easy))

        // 在线对拍（决定 14）。它抓的是向量抓不到的那一类：状态机算得对，
        // 但上报时接错了线——传错排队号，或者把作答顺序发拧了。
        if compare, !flag("offline") {
            if let server = try? await reportOne(queueId: queueId, answer: answer) {
                let ours = (next.direction.rawValue, next.asks, next.misses, next.done)
                let theirs = (server.direction, server.asks, server.misses, server.done)
                if ours != theirs {
                    disagreements += 1
                    print(Ink.red("  对拍不一致：客户端 \(ours)，服务端 \(theirs)"))
                } else {
                    print(Ink.dim("  对拍一致"))
                }
            }
        }
        print(next.done ? Ink.green("  这条过了") : Ink.dim("  方向 \(next.direction.rawValue)，"
            + "问过 \(next.asks) 次，权重 \(String(format: "%.4f", next.weight))"))
    }

    print(Ink.green("今天的复习做完了") + Ink.dim("，一共问了 \(asked) 次"))
    if compare {
        print(disagreements == 0
            ? Ink.green("对拍全程一致")
            : Ink.red("对拍有 \(disagreements) 处不一致——客户端那份镜像要改"))
    }
    if day.spelling_enabled {
        print(Ink.dim("可以做拼写强化了：ercli spell"))
    }
}

/// Which sentence to ask with.
///
/// **Not a free choice.** The sentences you already read are hints; the ones you
/// have not are questions — so asking with a hint would be showing the answer.
/// The server has already split them, and this only picks within the right pile.
/// Delegates: which sentence a direction asks with is a rule, and it now lives
/// in Core so the phone and this cannot disagree about it.
func askedSentence(_ card: Components.Schemas.ReviewItem,
                   _ direction: ReviewDirection) -> Components.Schemas.SentenceCard? {
    HintLadder.askedSentence(for: card, direction: direction)
}

func question(_ card: Components.Schemas.ReviewItem,
              _ asked: Components.Schemas.SentenceCard?,
              _ direction: ReviewDirection) -> String {
    let text = HintLadder.prompt(for: card, asked: asked, direction: direction)
    switch direction {
    case .wordToSense:
        return Ink.bold(text) + "\n" + Ink.dim("   —— 这里的 \(card.item_key) 是什么意思？")
    case .senseToWord:
        return Ink.bold(text) + "\n" + Ink.dim("   —— 空里填哪个词？")
    }
}

/// Ask one question, letting the learner open hints one at a time.
///
/// **Opening a hint fails the round, whatever happens next** — that is the
/// whole point of `revealed` being the grade: it measures what it took, not
/// what you claim afterwards. So the count is tracked here and reported, rather
/// than being a number the learner picks off a menu (which is what the first
/// version did, hard-coding 2).
/// Scripted input, so the unattended walk can drive the review.
///
/// `--answers 1,3,2` feeds one keystroke at a time. Not a convenience: a path
/// that can only be driven by hand is a path that does not get driven, and the
/// review half of this client sat unexercised for exactly that reason until it
/// was noticed. With this, `walk` covers the whole day rather than half of it.
nonisolated(unsafe) var scriptedInput: [String] = (value("answers") ?? "")
    .split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }
    .filter { !$0.isEmpty }

func readAnswer(_ fallback: String = "1") -> String {
    if !scriptedInput.isEmpty {
        let next = scriptedInput.removeFirst()
        print(next + Ink.dim("  ←脚本"))
        return next
    }
    return readLine()?.trimmingCharacters(in: .whitespaces) ?? fallback
}

func promptAnswer(_ card: Components.Schemas.ReviewItem,
                  _ asked: Components.Schemas.SentenceCard?,
                  _ direction: ReviewDirection) -> ReviewAnswer {
    let ladder = HintLadder.hints(for: card, asked: asked, direction: direction)
    var revealed = 0

    while true {
        var options = ["[1] 想起来了", "[2] 没想起来"]
        if revealed < ladder.count { options.append("[3] 给点提示") }
        if revealed == 0 { options.append("[4] 太简单了") }
        print(Ink.dim("  " + options.joined(separator: "   ")))
        print("  > ", terminator: "")
        let line = readAnswer()

        switch line {
        case "2":
            return ReviewAnswer(passed: false, revealed: revealed)
        case "3" where revealed < ladder.count:
            let hint = ladder[revealed]
            revealed += 1
            print(Ink.yellow("  提示 \(hint.level)/\(HintLadder.maxReveal) ")
                + Ink.dim(hint.label + "：") + hint.body)
        case "4" where revealed == 0:
            return ReviewAnswer(passed: true, easy: true)
        default:
            return ReviewAnswer(passed: true, revealed: revealed)
        }
    }
}

struct ServerAnswerState {
    let direction: Int
    let asks: Int
    let misses: Int
    let done: Bool
}

/// Report one answer on its own and read back what the server made of it. Only
/// used by `--compare`: the normal path queues answers and reports them in a
/// batch, which is what an offline client does.
func reportOne(queueId: Int, answer: ReviewAnswer) async throws -> ServerAnswerState? {
    let body = try JSONEncoder().encode([
        "queue_id": JSONValue.int(queueId),
        "passed": .bool(answer.passed),
        "revealed": .int(answer.revealed),
        "easy": .bool(answer.easy),
    ])
    let response = try await transport.send(
        HTTPRequest(method: .post, path: "/v1/client/reviews/answer", body: body))
    guard response.isOK else { return nil }
    let decoded = try JSONDecoder().decode(
        Components.Schemas.AnswerResponse.self, from: response.body)
    // The server does not echo the direction, so derive it the way it does:
    // done items are finished, everything else went back to or stayed at one of
    // the two directions the payload already describes.
    return ServerAnswerState(
        direction: decoded.done ? 2 : (decoded.misses > answer.revealed ? 1 : 2),
        asks: decoded.asks, misses: decoded.misses, done: decoded.done)
}

func spell() async throws {
    let package = try await engine.fetchDay()
    // 一个词多个义项只拼一次。
    var seen = Set<String>()
    let words = package.reviews.items.map(\.item_key).filter { seen.insert($0).inserted }
    guard !words.isEmpty else { print(Ink.dim("今天没有要拼的词")); return }

    print(Ink.bold("拼写强化") + Ink.dim("  \(words.count) 个词。拼错不影响复习安排，只记一笔。"))
    for word in words {
        if let sense = package.reviews.items.first(where: { $0.item_key == word })?.sense {
            print(Ink.dim("  提示：") + (sense.concept_en ?? "—"))
        }
        print("  > ", terminator: "")
        let typed = readAnswer("")
        try record(.spelled(word, typed: typed))
        print(typed.lowercased() == word.lowercased()
            ? Ink.green("  对了") : Ink.red("  应该是 \(word)"))
    }
}

// MARK: - The whole day

/// 走完一天。This is the acceptance run: fetch, read, look up, mark, finish,
/// review, spell, report — and say at the end what actually happened, because a
/// walk that prints nothing but "done" proves nothing.
func walk() async throws {
    print(Ink.bold("=== 走完一天 ===")
        + (flag("offline") ? Ink.yellow("（离线）") : ""))

    let package = try await engine.fetchDay()
    guard let first = package.articles.first else {
        print(Ink.red("今日包里没有文章")); return
    }
    let id = first.article.id
    let renderer = ArticleRenderer(first)
    let display = renderer.display

    print("\n\(Ink.bold("① 拉今日包"))：\(package.articles.count) 篇，"
        + "复习 \(package.reviews.items.count) 条")
    print("\(Ink.bold("② 打开 #\(id)"))：\(first.article.title)")
    print("   \(display.tokens.count) 个 token，"
        + "目标词 \(display.tokens.filter { $0.role == .target }.count)，"
        + "词组 \(display.phrases.count)")
    try record(.articleOpened(id))

    // Look one word up and, if the article has a phrase, look that up too —
    // the phrase branch is the one where a wrong answer is a wrong meaning
    // rather than a missing one.
    if let target = display.tokens.enumerated().first(where: { $0.element.role == .target }) {
        print("\n\(Ink.bold("③ 查一个目标词"))")
        print(renderer.panel(at: target.offset))
        try record(.wordTapped(target.element.headword ?? "", articleId: id))
    }
    if let phrase = display.phrases.first {
        print("\n\(Ink.bold("④ 查一个词组"))——点它的任何一半都该到同一个地方")
        print(renderer.panel(at: phrase.startSeq))
        let sameFromOtherHalf = renderer.display.target(at: phrase.endSeq)
        print(sameFromOtherHalf == .phrase(phrase)
            ? Ink.green("   两半都指向整个词组")
            : Ink.red("   两半指向的不是同一个东西——词组渲染坏了"))
        try record(.marked(phrase.phrase, kind: .unknown, itemType: "phrase",
                                  articleId: id))
        print(Ink.dim("   标记了这个词组（整体）"))
    }

    if let target = display.tokens.enumerated().first(where: { $0.element.role == .target }),
       let headword = target.element.headword {
        let senseId = (first.tokens ?? []).first { $0.seq == target.offset }?.sense_id ?? 0
        try record(.marked(headword, senseId: senseId, kind: .unknown, articleId: id))
        print("\n\(Ink.bold("⑤ 标记一个生词"))：\(headword)（义项 \(senseId)）")
    }

    try record(.articleFinished(id, sentenceSeq: (first.sentences?.count ?? 1) - 1))
    print("\n\(Ink.bold("⑥ 读完"))——记账只在这一刻发生")

    // ⑦⑧ 复习与拼写。**这一半原先不在 walk 里**——它走到读完就停了，于是状态机、
    // 提示分级、作答上报这条链只有单元测试见过。一天不是读完就结束的。
    try await walkReview(package.reviews)

    print("\n\(Ink.bold("⑨ 发件箱"))：\(outbox.count) 条待报")
    let report = try await engine.drain()
    if report.offline {
        print(Ink.yellow("   离线，一条没发出去——全都留着，这正是要验的"))
        print(Ink.green("\n离线也把一天走完了：读完 + 复习 + 拼写，"
            + "发件箱 \(outbox.count) 条等着联网。"))
        return
    }
    print(Ink.green("   收下 \(report.landed)，重复 \(report.duplicates)，"
        + "失败 \(report.rejected)，剩 \(report.remaining)"))
    print(Ink.green("\n一天走完了：读完 + 复习 + 拼写，全部上报。"))
}

/// The review and spelling half of `walk`.
///
/// Bounded on purpose: `walk` runs unattended, and an item that keeps being
/// answered wrong would otherwise loop forever. Stopping early is honest — the
/// point is to prove the path works, not to finish someone's day for them.
func walkReview(_ day: Components.Schemas.ReviewDayResponse) async throws {
    let open = day.items.filter { !$0.done }
    guard !open.isEmpty else {
        print("\n\(Ink.bold("⑦ 复习"))：今天没有到期的，跳过")
        return
    }

    print("\n\(Ink.bold("⑦ 复习"))：\(open.count) 条待做"
        + Ink.dim(scriptedInput.isEmpty ? "（手动作答）" : "（脚本作答）"))

    var states: [Int: ReviewItemState] = [:]
    for item in open {
        states[item.queue_id] = ReviewItemState(
            direction: ReviewDirection(rawValue: item.direction) ?? .wordToSense,
            asks: item.asks, misses: item.misses, weight: item.weight, done: item.done)
    }
    let cards = Dictionary(uniqueKeysWithValues: open.map { ($0.queue_id, $0) })
    let draw = ReviewDraw()
    var generator = SystemRandomNumberGenerator()
    var asked = 0
    let budget = scriptedInput.isEmpty ? 2 : 40

    while asked < budget, let queueId = draw.pick(
        from: states.keys.sorted(),
        weight: { states[$0]!.weight },
        isOpen: { !states[$0]!.done },
        random: Double.random(in: 0..<1, using: &generator)
    ) {
        guard let card = cards[queueId], let state = states[queueId] else { break }
        asked += 1
        let sentence = askedSentence(card, state.direction)
        print(Ink.dim("  ⟨\(queueId)⟩ ") + question(card, sentence, state.direction))
        let answer = promptAnswer(card, sentence, state.direction)
        states[queueId] = state.applying(answer, weightDecay: day.weight_decay)
        try record(.answered(queueId: queueId, passed: answer.passed,
                                    revealed: answer.revealed,
                                    sentenceId: sentence?.id, easy: answer.easy))
    }

    let done = states.values.filter { $0.done }.count
    print("   问了 \(asked) 次，过了 \(done) 条，还剩 \(states.count - done) 条")

    // 拼写：当天复习走完之后的强化选项。**拼错不影响调度，只单独记一笔。**
    guard day.spelling_enabled else { return }
    var seen = Set<String>()
    let words = open.map { $0.item_key }.filter { seen.insert($0).inserted }.prefix(2)
    guard !words.isEmpty else { return }

    print("\n\(Ink.bold("⑧ 拼写"))：\(words.count) 个词")
    for word in words {
        // **不读 `--answers`。** 那个开关是喂复习的，而复习要问多少次事先不知道，
        // 所以剩下多少也不知道——第一次跑就把 `copper` 拼成了「3」。
        // walk 是无人值守的，这里故意拼错，好让「拼错了会怎样」这条路真的被走过，
        // 而不是每次都走对的那一支。要手动做拼写就用 `ercli spell`。
        let typed = String(word.dropLast())
        try record(.spelled(word, typed: typed))
        print("   \(word) ← 打成「\(typed)」"
            + (typed == word ? Ink.green("  对")
                             : Ink.red("  错——只记录，不影响复习安排")))
    }
}

// MARK: - Dispatch

do {
    switch positionals.first {
    case "day": try await showDay()
    case "read":
        guard let id = positionals.dropFirst().first.flatMap(Int.init) else { usage(); exit(2) }
        try await read(id)
    case "look":
        let rest = positionals.dropFirst().compactMap(Int.init)
        guard rest.count >= 2 else { usage(); exit(2) }
        try await look(rest[0], rest[1])
    case "mark":
        let rest = positionals.dropFirst().compactMap(Int.init)
        guard rest.count >= 2 else { usage(); exit(2) }
        try await mark(rest[0], rest[1], fuzzy: flag("fuzzy"))
    case "unmark":
        let rest = positionals.dropFirst().compactMap(Int.init)
        guard rest.count >= 2 else { usage(); exit(2) }
        try await unmark(rest[0], rest[1])
    case "finish":
        guard let id = positionals.dropFirst().first.flatMap(Int.init) else { usage(); exit(2) }
        try await finish(id)
    case "library":
        try await library(positionals.dropFirst().first)
    case "review": try await review()
    case "spell": try await spell()
    case "sync": try await sync()
    case "cache": try showCache()
    case "walk": try await walk()
    default: usage()
    }
} catch let error as TransportError {
    switch error {
    case .offline(let detail):
        // 断线不是错误，是这台设备的常态——所以它跟别的失败要分开说。
        print(Ink.yellow("连不上服务器：\(detail)"))
        print(Ink.dim("做过的事都在发件箱里（\(outbox.count) 条），联网后 ercli sync。"))
        exit(0)
    case .server(let status, let body):
        print(Ink.red("服务端拒绝了：HTTP \(status)"))
        print(Ink.dim(body))
        exit(1)
    case .malformed(let detail):
        print(Ink.red("回应跟契约对不上：\(detail)"))
        exit(1)
    }
} catch {
    print(Ink.red("出错了：\(error)"))
    exit(1)
}
