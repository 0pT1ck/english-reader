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
    /// 七天打卡条。**类型是 Core 的，不是契约的**（P9 §11）:
    /// 日历现在由重放算出来，服务端那个端点和它的契约类型一起删了，
    /// 再往一个不存在的形状里搬一次只是多一层翻译。
    private(set) var days: [ReviewCalendar.Day] = []
    private(set) var streak: Int = 0

    /// 两张卡片的数字。
    private(set) var todayTotal = 0
    private(set) var todayDone = 0
    private(set) var dueTotal = 0
    private(set) var dueDone = 0


    /// 今天这一份复习，**由 Core 拼出来**（`ReviewDay.assemble`）。
    ///
    /// **在这之前这几个数来自服务端快照里的 `progress.buckets_done`**，而那是
    /// 它上次收到上报时的样子——于是复习完一池子、回主界面一刷新就变回 0/13，
    /// 刷几次才回来。病根不是哪一行写错了，是「我做了多少」有两个来源而旧的那个赢
    /// （`phase-9.html` §1）。现在只有一个来源。
    ///
    /// **拼装在 Core 而不在这里**:命令行客户端也要同一份，
    /// 而同一条规则写两遍就会漂、漂了不报错（P5 那条纪律）。
    private(set) var today = ReviewDay()

    /// 服务端手上那份词池快照是什么时候的。nil ＝ 它还没收到过。
    var poolReportedAt: String? { today.poolReportedAt }

    /// 投影说该问、而句子池里还没有它，有几个。**平时是 0。**
    var awaitingContent: Int { today.awaitingContent }

    /// 拼写这一轮开没开（P3 决定 13）。两个标志位是两件事：
    /// `spellingEnabled` 是「这个功能开着」，`spellingAvailable` 是
    /// 「今天该复习的都走完了」——**后者只有在当天复习全部做完之后才为真**。
    /// P7 决定 21 当时不做拼写，代价写的就是「服务端这个字段会变 true 而没人读它」。
    private(set) var spellingEnabled = false
    private(set) var spellingAvailable = false

    /// 今天要拼的词。算在 Core 里（`ReviewDay.spellingWords()`）。
    var spellingWords: [SpellingWord] { today.spellingWords() }

    /// 正在做哪一个池子。nil＝在主界面。
    private(set) var bucket: Projection.Bucket?
    private(set) var current: Projection.Key?
    private(set) var step: Step = .asking
    private(set) var asked: Components.Schemas.SentenceCard?
    private(set) var hint: Hint?
    /// 这道题开过提示没有。**开了就是成绩**——服务端据此把评级降一档。
    private(set) var revealed = 0

    /// 这一轮你说过「太简单了」没有。
    ///
    /// **它是学习者的判断，系统从不推断。** FSRS 把「我会了」和「这太简单」分开，
    /// 而分开它们要的是「多快、多确定」这类度量，这个项目不收集；把每次干净通过都
    /// 判成简单，正是当年间隔跑成 8→66→180 的原因。
    ///
    /// **放在 `···` 菜单里，不放在作答那一排按钮里**（2026-09-17 用户定）：
    /// 它要的是「我确实是特意点它的」，而三个并排的按钮做不到这一点。
    /// P7 决定 22 留了那个菜单、P8 填了一项，这是第二项。
    private(set) var claimedEasy = false

    private let draw = ReviewDraw()

    var isFinished: Bool { current == nil && bucket != nil }

    // MARK: 载入

    /// 正在载入。**`needsLoad` 靠不住，所以另立一个**（2026-09-19）。
    ///
    /// `needsLoad` 读的是 `phase == .loading`，而 `phase` 要到第一个 `await`
    /// 之后才变——于是 `.task` 和 `.onAppear` 两个入口的检查**都会通过**，
    /// 两趟 `load()` 并发跑起来:两次 `fetchDay()`、两次句子池。
    /// 09-17 的日志里那些成对的 `/v1/client/today` 超时就是它。
    ///
    /// 两个入口都留着（那是 P7 真机上逼出来的:`.task` 会在切走时被取消，
    /// 而取消之后没有东西再叫它）。**要护的是「同时只跑一趟」，不是「只有一个入口」。**
    private var loading = false

    func load(_ app: AppModel) async {
        guard !loading else { return }
        loading = true
        defer { loading = false }

        guard let engine = app.engine else {
            phase = .failed("连接还没建好，先到设置里填地址和令牌")
            return
        }

        // **先用本地那两份把屏幕点亮**（2026-09-15 真机之后加）。
        // 内容就在盘上，而原先每次进这一格都要等它重新下来——过隧道 0.85–1.4 秒，
        // 这一秒里屏幕上什么都没有，而答案其实一直在本地。
        //
        // **今日包只为那几个服务端参数**（排期参数、权重衰减），
        // 而题目的内容来自句子池那一份。**只认今天的今日包**：
        // 昨天的包不是「旧一点」，是错的——而句子池没有这个问题:
        // 它是「你在学哪些词」的函数，不是「今天是哪天」的函数。
        if let cached = await engine.cachedDay(), cached.day == Self.localToday {
            app.adopt(cached)
            spellingEnabled = cached.settings.spelling_enabled
        }
        if let cachedPool = await engine.cachedSentences() {
            pool = cachedPool
            sync(app)
            phase = .ready
        }

        do {
            let package = try await engine.fetchDay()
            app.adopt(package)
            // 拼写这个功能开没开是服务端的配置，随今日包下发（P8 §7）。
            spellingEnabled = package.settings.spelling_enabled

            // **顺序仍然有意义，但屏幕不再等它**（2026-09-19 真机之后改）。
            //
            // 原文是「先把事件推上去、把词池快照报上去，句子池才是对的那一批」——
            // 那个理由今天照样成立。**错的是让屏幕等在上面**:
            // `drain()` 要走网络，而它失败的时候要走好几秒，
            // 于是「切一下选项卡」变成「等一趟正在失败的同步」，屏幕上一直转圈。
            //
            // 所以:有待发才等（那时句子池确实可能不对），没有就直接取。
            // **常态是 0 条**，也就是常态下这一格立刻就亮。
            if app.pendingEvents > 0 {
                await app.drain()
            } else {
                // 没有待发也还是要报一次快照／拉一次别处做的事，
                // 只是不占着屏幕——它回来之后 `sync` 会再刷一遍数。
                Task { [weak app] in
                    guard let app else { return }
                    await app.drain()
                    sync(app)
                }
            }
            pool = try await engine.fetchSentences()
            sync(app)
            phase = .ready
        } catch is CancellationError {
            // **取消不是故障。**`IOSTransport` 早就把它和「离线」分开了，
            // 注释写的是「用户划走了一屏就取消一次请求」——而这里原本把它
            // 兜进了最后那个 catch，于是屏幕上说「复习打不开」，
            // 后面跟一句 `Swift.CancellationError error 1`。
            //
            // 真机上一进复习就撞到了：`fetchDay` 要拉 1 MB 的今日包，
            // 经隧道约一秒，而这一秒里视图重算一次，task 就被取消。
            // 那时 `phase` 还是 `.loading`（缓存优先那条路还没加），
            // 退回 `.loading` 无伤大雅，`.onAppear` 会再试一次。
            //
            // **不要把已经点亮的屏幕拉回转圈**（2026-09-19，用户实测揪出来的:
            // 「立马切走再切回」比「放一会儿再切」更容易闪）。缓存优先加了之后，
            // 被取消时 `phase` 通常已经是 `.ready`——缓存已经把内容点亮，
            // 被取消的只是后台那次刷新。这时候退回 `.loading` 是在把一个
            // 好端端的画面往回撤，下次进来又要重新走一遍这整条路径、
            // 再闪一次。**只在还没显示过任何内容时才退**。
            if phase != .ready { phase = .loading }
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

    /// 句子池那一份，原样存着。**内容归服务端，状态归重放**——
    /// 对到一起的活在 Core 的 `ReviewDay` 里。
    private var pool: Components.Schemas.SentencePoolResponse?

    /// 把内容与投影对到一起。**记完一条事件就调它。**
    private func sync(_ app: AppModel) {
        today = ReviewDay.assemble(pool: pool, projection: app.projection,
                                   day: Self.localToday, now: Date())
        // **日历也由重放算出来**（P9 §11）。在这之前它是单独一条请求
        // （`/reviews/calendar`），而那条路读的是服务端的会话与队列——
        // 也就是学习记录。现在它和那两个数同源，所以**答完一题当场变色**，
        // 而不是等下一次联网。
        if let calendar = app.calendar(days: 7) {
            days = calendar.days
            streak = calendar.streak
        }
        todayTotal = today.total(.today)
        todayDone = today.done(.today)
        dueTotal = today.total(.due)
        dueDone = today.done(.due)
        spellingAvailable = today.spellingAvailable
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
    func begin(bucket name: Projection.Bucket) {
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
        let open = today.entries.filter { $0.bucket == name }
        current = draw.pick(
            from: open,
            weight: { $0.state.weight },
            isOpen: { !$0.state.done },
            random: Double.random(in: 0..<1)
        )?.key
        step = .asking
        revealed = 0
        claimedEasy = false
        hint = nil
        asked = nil
        if let card, let entry = entry(of: current) {
            asked = HintLadder.askedSentence(for: card, direction: entry.state.direction)
        }
    }

    /// 队列里那一条。
    private func entry(of key: Projection.Key?) -> ReviewDay.Entry? {
        guard let key else { return nil }
        return today.entries.first { $0.key == key }
    }

    var card: Components.Schemas.ReviewItem? { entry(of: current)?.item }
    /// 这道题用的那一句，揭晓时还要拿它补另一面。
    var askedCard: Components.Schemas.SentenceCard? { asked }

    /// **两个池子都做完了。**决定 20 只在这时候才显示「今天的所有复习」那一屏——
    /// 只做完一张卡就这么说是句假话，那时直接回主界面，反馈是那张卡变绿。
    var allDone: Bool { today.allDone }
    var direction: ReviewDirection { entry(of: current)?.state.direction ?? .wordToSense }

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

    /// 点「太简单了」。
    ///
    /// **只在这一轮一次没错时才给点**：在磕过之后说「太简单」不是关于任何事情的
    /// 断言，而 Core 与服务端都会当场把这个声明撤回（`ReviewItemState.applying`
    /// 里那句 `&& next.misses == 0`）。一个点了会被静默撤回的菜单项，
    /// 比一个点不动的菜单项难查得多——所以这里由 `canClaimEasy` 把它关掉。
    func claimEasy() {
        guard canClaimEasy else { return }
        claimedEasy = true
    }

    /// 现在能不能说「太简单了」。
    var canClaimEasy: Bool {
        guard let entry = entry(of: current) else { return false }
        return entry.state.misses == 0 && !claimedEasy
    }

    /// 这一轮已经磕过了，所以那一项是灰的——菜单上要说得出为什么。
    var easyWithdrawn: Bool {
        guard let entry = entry(of: current) else { return false }
        return entry.state.misses > 0
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
        guard let card else { return }
        app.record(.unmarked(card.item_key, senseId: card.sense_id,
                             itemType: card.item_type))
        app.log?.write(.info, "review.dismissed", "把一个词移出了复习",
                       fields: ["item": card.item_key])
        // **不用手动把它从名单上拿掉。** 撤销标记让它退回 `new`，
        // 而投影只收 `reviewing` 那一档——重放一次它自己就不在了。
        // 「不计进已复习」也因此自动成立:它没被复习，是被拿走了。
        sync(app)
        next()
    }

    /// 「下一个」。**作答在这一刻才落盘**——揭晓屏能改主意，点按钮那一下就发的话
    /// 改回来也追不回已经发出去的事。
    /// 「下一个」。**作答在这一刻才落盘**——揭晓屏能改主意，点按钮那一下就发的话
    /// 改回来也追不回已经发出去的事。
    ///
    /// **落盘之后重放，不自己改数。** 上一版在这里手动 `todayDone += 1`，
    /// 那是「同一个数有两处在写」——而这个 Phase 的全部内容就是把那种情况消掉。
    /// 现在写完日志重放一次，数字是算出来的。
    func advance(_ app: AppModel) {
        guard let key = current, let card, case .revealed(let passed) = step
        else { return }

        // **事件自带条目身份，不只带 `queue_id`**（P9，phase-9.html §16）。
        // 那个号是服务端队列表的行号，而那张表每天重建、编号天天不一样——
        // 日志里引一个只有别处才解释得了的标识符，就不是可重放的日志：
        // 换台设备重放它，这条作答指不到任何词。`queue_id` 照旧带着（铁律 5，
        // 只增不减），身份是新加的那几个字段。
        // `card.queue_id` 现在恒为 0（题目是现拼的，见 `sync`）。
        // 服务端认得「0 ＋ 身份」那一支:它只把事件记下来，不再算一遍（§11）。
        app.record(.answered(queueId: card.queue_id, passed: passed,
                             revealed: revealed, sentenceId: asked?.id,
                             easy: claimedEasy,
                             itemType: key.itemType, itemKey: key.key,
                             senseId: key.senseId))
        sync(app)
        next()
    }
}
