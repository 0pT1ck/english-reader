import Foundation
import Observation
import ERCore
import ERContract

/// 复习的全部状态：日历、两个池子、一道题走到哪一步。
///
/// **一条学习规则都不在这里。**抽题、双向解锁、答错退回第一向、权重衰减，
/// 全在 Core 里，而 Core 那份是用服务端导出的向量校验过的。这个类做的是
/// 「现在该显示哪一屏」。
@MainActor
@Observable
final class ReviewModel {
    enum Phase: Equatable {
        case loading
        case ready
        case offline(String)
        case failed(String)
    }

    /// 一道题走到哪一步。
    enum Step: Equatable {
        /// 题面，三个按钮（认识 / 不确定 / 不认识）。
        case asking
        /// 点过「不确定」，提示展开了，按钮剩两个。
        case hinted
        /// 揭晓：答案与资料都在，底下可以改主意，按「下一个」才上报。
        case revealed(passed: Bool)
    }

    private(set) var phase: Phase = .loading
    private(set) var days: [Components.Schemas.CalendarDay] = []
    private(set) var streak: Int = 0

    /// 两张卡片的数字。
    private(set) var todayTotal = 0
    private(set) var todayDone = 0
    private(set) var dueTotal = 0
    private(set) var dueDone = 0

    private(set) var weightDecay: Double = 0.5

    /// 本地这一份状态机。服务端那份是权威，但离线时要照样走得下去。
    private var states: [Int: ReviewItemState] = [:]
    private var cards: [Int: Components.Schemas.ReviewItem] = [:]

    /// 拼写这一轮开没开（P3 决定 13）。两个标志位是两件事：
    /// `spellingEnabled` 是「这个功能开着」，`spellingAvailable` 是
    /// 「今天该复习的都走完了」——**后者只有在当天复习全部做完之后才为真**。
    /// P7 决定 21 当时不做拼写，代价写的就是「服务端这个字段会变 true 而没人读它」。
    private(set) var spellingEnabled = false
    private(set) var spellingAvailable = false

    /// 今天要拼的词。**一个词多个义项只拼一次**——照命令行客户端那套，
    /// 拼的是词形，跟义项无关。
    var spellingWords: [SpellingWord] {
        var seen = Set<String>()
        return cards.values
            .sorted { $0.queue_id < $1.queue_id }
            .filter { seen.insert($0.item_key).inserted }
            .map { SpellingWord(key: $0.item_key,
                                phonetic: $0.word?.phonetic,
                                gloss: Self.gloss(of: $0)) }
    }

    /// 提示给中文，不给英文概念——**拼写考的是「听到／想到这个意思，写得出这个词」**，
    /// 而英文概念里常常就含着这个词的同根词。
    private static func gloss(of item: Components.Schemas.ReviewItem) -> String {
        if let list = item.sense?.gloss_zh?.value1, !list.isEmpty {
            return list.joined(separator: "，")
        }
        if let single = item.sense?.gloss_zh?.value2 { return single }
        return item.word?.translation ?? ""
    }

    /// 正在做哪一个池子。nil＝在主界面。
    private(set) var bucket: String?
    private(set) var current: Int?
    private(set) var step: Step = .asking
    private(set) var asked: Components.Schemas.SentenceCard?
    private(set) var hint: Hint?
    /// 这道题开过提示没有。**开了就是成绩**——服务端据此把评级封顶到 Hard。
    private(set) var revealed = 0

    private let draw = ReviewDraw()

    var isFinished: Bool { current == nil && bucket != nil }

    // MARK: 载入

    func load(_ app: AppModel) async {
        guard let engine = app.engine else {
            phase = .failed("连接还没建好，先到设置里填地址和令牌")
            return
        }

        // **先用本地那一份把屏幕点亮**（2026-09-15 真机之后加）。
        // 今日包就在盘上，而原先每次进这一格都要等它重新下来——1 MB 过隧道
        // 0.85–1.4 秒，这一秒里屏幕上什么都没有，而答案其实一直在本地。
        //
        // **只认今天的**：昨天的包不是「旧一点」，是错的。日期对不上就老实等。
        if let cached = await engine.cachedDay(), cached.day == Self.localToday {
            apply(cached.reviews)
            phase = .ready
        }

        do {
            let package = try await engine.fetchDay()
            apply(package.reviews)
            phase = .ready
            // 日历单独一条请求，拿不到不影响复习本身。
            if let calendar = try? await engine.calendar(days: 7) {
                days = calendar.days
                streak = calendar.streak
            }
        } catch is CancellationError {
            // **取消不是故障。**`IOSTransport` 早就把它和「离线」分开了，
            // 注释写的是「用户划走了一屏就取消一次请求」——而这里原本把它
            // 兜进了最后那个 catch，于是屏幕上说「复习打不开」，
            // 后面跟一句 `Swift.CancellationError error 1`。
            //
            // 真机上一进复习就撞到了：`fetchDay` 要拉 1 MB 的今日包，
            // 经隧道约一秒，而这一秒里视图重算一次，task 就被取消。
            // 回到 loading 由 `.onAppear` 再试一次（见 `ReviewScreen`）。
            phase = .loading
        } catch let error as TransportError {
            if case .offline = error {
                phase = .offline("离线，连不上服务器")
            } else {
                phase = .failed(describe(error))
            }
        } catch {
            phase = .failed(error.localizedDescription)
        }
    }

    /// 载入中吗。取消之后 `.task` 不会自己重来，所以要有人看着它。
    var needsLoad: Bool { phase == .loading }

    /// 客户端眼里的今天。**服务端的模拟时钟可能跳着走**，所以这个判断只用来
    /// 决定「敢不敢先拿缓存顶一下」——跳过天之后本地日期对不上，
    /// 那就老实等服务端那一份，这正是我们要的保守方向。
    static var localToday: String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date())
    }

    private func apply(_ day: Components.Schemas.ReviewDayResponse) {
        weightDecay = day.weight_decay
        states.removeAll()
        cards.removeAll()
        for item in day.items {
            cards[item.queue_id] = item
            states[item.queue_id] = ReviewItemState(
                direction: ReviewDirection(rawValue: item.direction) ?? .wordToSense,
                asks: item.asks, misses: item.misses,
                weight: item.weight, done: item.done)
        }
        spellingEnabled = day.spelling_enabled
        spellingAvailable = day.progress.spelling_available
        let total = day.progress.buckets.additionalProperties
        // `buckets_done` has a default on the server, so it is optional on the
        // wire — a client built before it existed has to keep decoding (铁律 5).
        let done = day.progress.buckets_done?.additionalProperties ?? [:]
        todayTotal = total["today"] ?? 0
        todayDone = done["today"] ?? 0
        dueTotal = total["due"] ?? 0
        dueDone = done["due"] ?? 0
    }

    private func describe(_ error: TransportError) -> String {
        switch error {
        case .offline(let detail): "离线，连不上服务器（\(detail)）"
        case .server(let status, _) where status == 401: "令牌不对或已吊销，到设置里换一个"
        case .server(let status, _): "服务器出错了（HTTP \(status)）"
        case .malformed(let detail): "服务器回的东西看不懂：\(detail)"
        }
    }

    // MARK: 一道题

    /// 进某个池子，抽第一题。
    func begin(bucket name: String) {
        bucket = name
        next()
    }

    func leave() {
        bucket = nil
        current = nil
    }

    /// 抽下一题。**只从这个池子里抽**，权重和衰减率都来自服务端。
    private func next() {
        guard let name = bucket else { return }
        let pool = states.keys.filter { cards[$0]?.bucket == name }.sorted()
        current = draw.pick(
            from: pool,
            weight: { self.states[$0]?.weight ?? 1 },
            isOpen: { !(self.states[$0]?.done ?? true) },
            random: Double.random(in: 0..<1)
        )
        step = .asking
        revealed = 0
        hint = nil
        asked = nil
        if let id = current, let card = cards[id], let state = states[id] {
            asked = HintLadder.askedSentence(for: card, direction: state.direction)
        }
    }

    var card: Components.Schemas.ReviewItem? { current.flatMap { cards[$0] } }
    /// 这道题用的那一句，揭晓时还要拿它补另一面。
    var askedCard: Components.Schemas.SentenceCard? { asked }

    /// **两个池子都做完了。**决定 20 只在这时候才显示「今天的所有复习」那一屏——
    /// 只做完一张卡就这么说是句假话，那时直接回主界面，反馈是那张卡变绿。
    var allDone: Bool {
        states.values.allSatisfy { $0.done }
    }
    var direction: ReviewDirection { current.flatMap { states[$0]?.direction } ?? .wordToSense }

    /// 题面那一句（方向 2 是整句中文）。
    var prompt: String {
        guard let card else { return "" }
        return HintLadder.prompt(for: card, asked: asked, direction: direction)
    }

    /// 题面里要高亮的那一段。方向 1 高亮英文词，方向 2 高亮对应的中文。
    var promptHighlight: Range<Int>? {
        switch direction {
        case .senseToWord:
            return HintLadder.promptHighlight(asked, direction: .senseToWord)
        case .wordToSense:
            guard let asked else { return nil }
            let start = max(0, min(asked.blank_start, asked.text.count))
            let end = max(start, min(asked.blank_end, asked.text.count))
            return start < end ? start..<end : nil
        }
    }

    /// 点「不确定」：展开提示，按钮从三个变两个。**一级到底，没有第二级。**
    func reveal() {
        guard let card, step == .asking else { return }
        hint = HintLadder.hints(for: card, asked: asked, direction: direction).first
        // 提示给不出来时按钮也得变，否则「不确定」就成了点了没反应的按钮。
        revealed = hint == nil ? 0 : 1
        step = .hinted
    }

    var hintAvailable: Bool {
        guard let card, step == .asking else { return false }
        return !HintLadder.hints(for: card, asked: asked, direction: direction).isEmpty
    }

    /// 点「认识」或「不认识」→ 揭晓。**这里不上报**。
    func answer(passed: Bool) {
        guard current != nil else { return }
        step = .revealed(passed: passed)
    }

    /// 揭晓屏上改主意。
    func amend(passed: Bool) {
        guard case .revealed = step else { return }
        step = .revealed(passed: passed)
    }

    var answeredPassed: Bool {
        if case .revealed(let passed) = step { return passed }
        return false
    }

    /// 「这个词我已经会了，别再考」（P8 §10 第 2 条）。
    ///
    /// P7 决定 22 把 `···` 菜单留成了空壳，代价写得很清楚：**词一旦标了，
    /// 只能等 FSRS 慢慢放过它**。而整条路本来就是通的——`Events.unmarked`
    /// 在 Core 里、`word.unmarked` 在服务端、跨 Phase 不变量说明了它的语义：
    /// **条目退回 `new`，但遇见记录保留**（它确实被遇见过）。缺的只是一个菜单项。
    ///
    /// **当场从今天的池子里拿掉，不等服务端回话**：离线也要对，而这条事件
    /// 带着幂等键，重复上报算正常。
    func dismissCurrent(_ app: AppModel) {
        guard let id = current, let card = cards[id] else { return }
        app.record(.unmarked(card.item_key, senseId: card.sense_id,
                             itemType: card.item_type))
        app.log?.write(.info, "review.dismissed", "把一个词移出了复习",
                       fields: ["item": card.item_key])
        // 不计进「已复习」那个数：它没被复习，是被拿走了。
        states[id]?.done = true
        cards.removeValue(forKey: id)
        next()
    }

    /// 「下一个」。**作答在这一刻才落盘**——揭晓屏能改主意，点按钮那一下就发的话
    /// 改回来也追不回已经发出去的事。
    func advance(_ app: AppModel) {
        guard let id = current, let state = states[id], case .revealed(let passed) = step
        else { return }

        let answer = ReviewAnswer(passed: passed, revealed: revealed)
        states[id] = state.applying(answer, weightDecay: weightDecay)

        app.record(.answered(queueId: id, passed: passed, revealed: revealed,
                             sentenceId: asked?.id))

        // 两张卡的数字跟着本地状态走，不等服务端回话——离线也要对。
        if states[id]?.done == true, let name = cards[id]?.bucket {
            if name == "today" { todayDone += 1 } else { dueDone += 1 }
        }
        next()
    }
}

/// 拼写那一轮要的三样：词、音标、中文。
struct SpellingWord: Identifiable, Equatable {
    let key: String
    let phonetic: String?
    let gloss: String
    var id: String { key }
}
