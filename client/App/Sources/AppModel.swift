import Foundation
import Observation
import ERCore
import ERContract

/// App 共用的那一份东西：本地存储、事件日志、发件箱、同步引擎、投影。
///
/// **哪些丢不得，P9 把答案改了。** P5 决定 8 说只有发件箱丢不得——因为别的
/// 都是服务端已有东西的副本。而 P9 之后 **事件日志是学习记录的第一副本**
/// （`phase-9.html` §5），它是这台设备上真正不可重建的那一份；发件箱降级成
/// 「还没送出去的那些」。
///
/// 每一类一个目录，就是为了让「清缓存不许碰记录」这条不变量**在文件系统上**
/// 成立，而不是靠纪律：清理动 `articles/`，日志在 `events/`，发件箱在
/// `outbox/`，三者没有交集。
@MainActor
@Observable
final class AppModel {
    let connection: Connection
    let preferences: Preferences

    private(set) var outbox: Outbox?
    private(set) var library: LibraryStore?
    private(set) var cache: ArticleCache?
    private(set) var dayCache: DayCache?
    /// 句子池的缓存（P9 §11）。**和今日包各自一个目录**——
    /// 两份内容不该互相覆盖，而 `DayCache` 实际上就是「一个文件 ＋ 一个版本号」。
    private(set) var sentenceCache: DayCache?
    /// 打卡日历算出来的那份结果，**存盘而不止存内存**（2026-09-19，见下面
    /// `calendar(days:)` 的注释）。复用 `DayCache` 现成的「一个文件 ＋ 一个
    /// 版本号」——版本号当缓存键用，不是给服务端比对的。
    private(set) var calendarCache: DayCache?
    private(set) var engine: SyncEngine?

    /// 事件日志（P9）。**设备上学习记录的第一副本**，屏幕上的每个数都由重放它算出来。
    private(set) var events: EventLog?

    /// 投影 projection: apply local facts incrementally; replay after a pull or
    /// changed scheduler settings. The log remains the only source of truth.
    private(set) var projection = Projection()

    /// 排期参数，今日包带下来的那一份。
    ///
    /// **nil ＝ 服务端还没下发。** 那时投影算不了排期，界面要说出来，
    /// 而不是回落到一份本地默认——两边各用自己的默认会跑出不同的间隔，
    /// 而且两边都不会报错（`phase-9.html` §16 ②那个形状）。
    private(set) var schedulerSettings: ReviewScheduler.Settings?
    /// 答错之后权重乘多少。规则是服务端的，客户端从袋子里抽。
    private(set) var weightDecay: Double = 0.5

    /// 客户端自己的日志（P8 §9）。**和发件箱、缓存并列建在同一个根下**，
    /// 但它是第四类：丢了不影响任何学习记录，三天之后本来也要自己删掉。
    private(set) var log: FileLog?

    /// 上一次同步的结果。`SyncReport` 早就返回这四个数，**至今没人显示过**——
    /// 设置页的存储那一区是它第一个读者。
    private(set) var lastSync: SyncReport?

    enum PreparationStatus: Equatable {
        case idle
        case running
        case waiting
        case failed(String)
        case offline
    }

    private(set) var preparationStatus: PreparationStatus = .idle
    private(set) var reviewPool: Components.Schemas.SentencePoolResponse?
    @ObservationIgnored private var reviewUpdate: (@MainActor () -> Void)?

    func observeReviewUpdates(_ update: @escaping @MainActor () -> Void) {
        reviewUpdate = update
    }

    func adoptSentences(_ pool: Components.Schemas.SentencePoolResponse) {
        guard reviewPool != pool else { return }
        reviewPool = pool
        reviewUpdate?()
    }

    var preparationMessage: String? {
        switch preparationStatus {
        case .idle: nil
        case .running: "正在上报标记并准备双语句子"
        case .waiting: "双语句子还在准备，稍后重试或回到前台会继续获取"
        case .failed(let message): message
        case .offline: "网络暂不可用；标记已保存在本机，联网后回到前台会继续"
        }
    }

    /// 起不来的原因。存储建不起来是唯一一种「App 没法用」的失败，
    /// 所以它要说得出话，而不是让每一屏各自转圈。
    private(set) var storageFailure: String?

    /// 发件箱里还有几条没上报。设置页显示它——那是「我标的东西到底传上去了没有」
    /// 的唯一出口，而这个 Phase 没有别的地方能回答这个问题。
    private(set) var pendingEvents: Int = 0

    /// 日志里一共多少条。**和待发数是两件事**：这一个是「这台设备知道的全部事实」，
    /// 那一个是「其中还没送出去的」。开发者选项要两个并排看——重拉之后
    /// 总数应该变（它是从会合点重新拉的），而待发数本来就该是 0。
    ///
    /// 按需刷新（`refreshEventCount()`），**不跟着待发数走**——理由在那个方法上。
    private(set) var eventCount: Int = 0

    /// 读不出来的事件文件有几条。**平时是 0**，非零意味着有一次标记既发不出去
    /// 也读不回来——那时待发数会永远卡着不动，而没有这个数的话，没人知道为什么。
    private(set) var damagedEvents: Int = 0

    init(connection: Connection = Connection(), preferences: Preferences = Preferences()) {
        self.connection = connection
        self.preferences = preferences
        buildStorage()
        rebuildEngine()
        log?.write(.info, "app.launched", "起来了",
                   fields: ["configured": connection.isConfigured ? "yes" : "no",
                            "pending": String(pendingEvents)])
    }

    /// 开发者选项那一页用的。**没填管理密码就是 nil**——一个「点了没反应」的
    /// 按钮比一个说得出「还没填密码」的界面难查得多，所以这里返回可选值，
    /// 由界面去解释它为什么没有。
    var admin: AdminClient? {
        guard let url = connection.adminURL, !connection.adminSecret.isEmpty else { return nil }
        return AdminClient(baseURL: url, secret: connection.adminSecret)
    }

    // MARK: 存储

    private func buildStorage() {
        do {
            let root = try FileManager.default.url(
                for: .applicationSupportDirectory, in: .userDomainMask,
                appropriateFor: nil, create: true
            ).appendingPathComponent("EnglishReader", isDirectory: true)

            outbox = try Outbox(directory: root.appendingPathComponent("outbox"))
            // **和发件箱并列、各自一个目录。** 清缓存动的是 `articles/`，
            // 发件箱在 `outbox/`，日志在 `events/`——三者在文件系统上就没有交集，
            // 所以「清缓存不许碰记录」这条不变量不靠纪律保证。
            events = try EventLog(directory: root.appendingPathComponent("events"))
            cache = try ArticleCache(directory: root.appendingPathComponent("articles"))
            dayCache = try DayCache(directory: root.appendingPathComponent("day"))
            sentenceCache = try DayCache(
                directory: root.appendingPathComponent("sentences"))
            calendarCache = try DayCache(
                directory: root.appendingPathComponent("calendar"))
            library = LibraryStore(directory: root.appendingPathComponent("library"))
            // 三天轮转在 init 里就发生（`FileLog` 自己 prune），所以这一行
            // 同时是「启动时清过期日志」那条决定的落点。
            let file = try FileLog(directory: root.appendingPathComponent("logs"),
                                   retentionDays: 3)
            file.minimumLevel = preferences.verboseLog ? .debug : .info
            log = file
            storageFailure = nil
            refreshProjection()
            refreshPendingCount()
        } catch {
            storageFailure = "本地存储建不起来：\(error.localizedDescription)"
            // 日志本身可能就是没建起来的那个，所以这条要两头都说：
            // 写进日志（如果还能写），也留在 `storageFailure` 上给界面看。
            log?.write(.error, "storage.unavailable", storageFailure ?? "")
        }
    }

    /// DEBUG 落不落盘，跟着偏好走。设置页改了开关之后调它。
    func applyLogLevel() {
        log?.minimumLevel = preferences.verboseLog ? .debug : .info
    }

    // MARK: 连接

    /// 换了地址或令牌就换一条传输。引擎是 actor，里面握着 transport，
    /// 所以换传输就是换引擎——没有「改一半」的中间状态。
    func rebuildEngine() {
        guard let outbox, let cache, let dayCache, let sentenceCache,
              let url = connection.url, !connection.token.isEmpty else {
            engine = nil
            return
        }
        engine = SyncEngine(
            // 契约版本那一层在 Core 里（P11 决定 ⑲）。**App 这一侧只有这一行**——
            // 界面这个 Phase 不动（决定 ⑦），但不发这个头的客户端会被服务端
            // 按「太旧」挡下来，那不是界面变化，是连得上连不上的问题。
            transport: VersionedTransport(
                IOSTransport(baseURL: url, token: connection.token, log: log)),
            outbox: outbox, events: events, articles: cache, day: dayCache,
            sentences: sentenceCache
        )
    }

    // MARK: 发件箱

    /// 记一条事件。**先落盘再说成功**——反过来的话用户会看见「已标记」
    /// 而它其实没写进去。
    @discardableResult
    func record(_ entry: OutboxEntry, expectingSequence: Int? = nil) -> Bool {
        guard let outbox, storageFailure == nil else { return false }
        do {
            // **五种事件只进日志，遥测只进发件箱——不再两处都写。**
            //
            // 上一版是先写日志再写发件箱，那中间有一个窄口子:崩在两次写之间，
            // 事件在日志里而发件箱里没有，**于是它永远发不出去**，
            // 而屏幕上什么都不会说（本机重放算得出它，服务端永远不知道）。
            // 现在 `drain()` 直接从日志取那五种（`SyncEngine.drainLog`），
            // 一条事件只有一个家。映射在 Core 里（`OutboxEntry.loggedKind`），
            // 和 `ercli` 共用一份。
            if let kind = entry.loggedKind, let events {
                let event = try events.append(kind: kind, payload: entry.payload,
                                              idemKey: entry.idemKey,
                                              occurredAt: entry.occurredAt,
                                              expectedLocalSequence: expectingSequence)
                // The durable append supplies the new fact. Do not decode the
                // whole log again on every slider movement or review answer.
                if projection.applyNext(event, weightDecay: weightDecay,
                                        settings: schedulerSettings ?? Self.unusableSettings) {
                    if lastLoad != nil { lastLoad?.events.append(event) }
                } else {
                    refreshProjection()
                }
            } else {
                try outbox.append(entry)
            }
            refreshPendingCount()
            log?.write(.debug, "outbox.recorded", "记下一条事件",
                       fields: ["kind": entry.kind.rawValue, "pending": String(pendingEvents)])
            reviewUpdate?()
            if entry.loggedKind == .marked || entry.loggedKind == .unmarked {
                requestPreparation()
            }
            return true
        } catch {
            if let logError = error as? EventLogError, case .sequenceChanged = logError {
                reconcileProjection()
                return false
            }
            // **这一条必须记。**写不进发件箱意味着那次标记消失了，而屏幕上
            // 什么都不会说——正是「静默失败」那一类里最贵的一种。
            log?.write(.error, "outbox.record.failed", "事件没能落盘",
                       fields: ["kind": entry.kind.rawValue,
                                "error": error.localizedDescription])
            return false
        }
    }

    /// Retain the decoded log for calendar calculation and pending counts.
    /// Local appends extend it; pulls and scheduler changes replay it once.
    private var lastLoad: EventLogLoad?

    func refreshProjection() {
        guard let events else { return }
        guard let load = try? events.load() else {
            log?.write(.error, "projection.unreadable", "日志读不出来")
            return
        }
        lastLoad = load
        projection = Projection.replay(
            load, weightDecay: weightDecay,
            settings: schedulerSettings ?? Self.unusableSettings
        )
        if load.damaged > 0 || load.tornTail {
            // **断尾和坏行是两件事。** 断尾是崩在写的那一刻，那条事件从未被确认过；
            // 坏行是别的原因，要查。
            log?.write(load.damaged > 0 ? .warn : .info, "projection.replayed",
                       "重放时有读不出来的行",
                       fields: ["damaged": String(load.damaged),
                                "torn_tail": load.tornTail ? "yes" : "no",
                                "events": String(load.events.count)])
        }
        reviewUpdate?()
    }

    func reconcileProjection() {
        guard events?.latestLocalSequence != projection.throughLocalSequence else { return }
        refreshProjection()
        refreshPendingCount()
    }

    /// 服务端还没下发排期参数时用的占位。
    ///
    /// **参数个数故意是错的**，所以 `ReviewScheduler.review` 会抛
    /// `wrongParameterCount` 而不是算出一个数来。投影因此拿不到记忆状态，
    /// 而界面据此说「服务端还没更新」——**宁可缺一半，也不要一半是编的**。
    private static let unusableSettings = ReviewScheduler.Settings(
        requestRetention: 0.9, maximumInterval: 180, parameters: [], enableFuzz: false)

    /// 今日包带来的那几个服务端参数。**每次取到包都调它**——但只在参数真的
    /// 变了的时候才重放。
    ///
    /// **这是「全盘筛查」揪出来的第三处同一形状**（2026-09-19，日历和
    /// 今日包/句子池解码之后）:`ReviewModel.load()` 每次进复习屏都会用
    /// 盘上缓存的今日包调一次 `adopt`,而这里原来无条件 `refreshProjection()`
    /// ——那是对全部事件日志的一次完整重放,而它排在 `phase = .ready` **之前**,
    /// 直接算在了「这一帧还没画出来」那段时间里。真正需要重放的唯一理由是
    /// 排期参数变了(权重衰减、FSRS 参数)——事件日志本身的变化已经由
    /// `record()`/`pull()` 各自触发过一次,不需要这里再兜底一遍。
    func adopt(_ package: DayPackage) {
        let newDecay = package.settings.weight_decay
        let newSettings = package.schedulerSettings
        let unchanged = newDecay == weightDecay && newSettings == schedulerSettings
        weightDecay = newDecay
        schedulerSettings = newSettings
        if schedulerSettings == nil {
            log?.write(.warn, "settings.scheduler.missing",
                       "今日包里没有排期参数——服务端还是旧镜像")
        }
        guard !unchanged else { return }
        refreshProjection()
    }

    /// 排期算不算得出来。界面据此决定要不要说话。
    var canSchedule: Bool { schedulerSettings != nil }

    /// 上一次算出来的日历，连同算它时用的那把钥匙。**答案没变就不用重算。**
    /// 这是内存那一层，进程活着的时候有效;跨次启动那一层在磁盘上，见下面。
    ///
    /// **答案只在两件事上会变**:事件日志前进了（钥匙用
    /// `projection.throughLocalSequence`，投影每次重放都会更新它），
    /// 或者日界跨过去了（钥匙用今天的日期）。两个都没变，直接把上次的结果
    /// 递出去。
    private var calendarMemoryCache: (day: String, through: Int, span: Int,
                                      result: (days: [ReviewCalendar.Day], streak: Int))?

    private struct PersistedCalendar: Codable {
        var days: [ReviewCalendar.Day]
        var streak: Int
    }

    /// 打卡日历与连续天数，**由重放算出来**（P9 §11）。
    ///
    /// nil ＝ 服务端还没下发排期参数，那时算不出「那天该做多少」——
    /// 而一个编出来的日历比没有日历糟得多:它会把没做的日子画成绿的。
    ///
    /// **两层缓存,不是一层**（2026-09-19，用户提的问题:「没有变更的话直接
    /// 用之前的数据不是很好吗」）。内存那层只在一次进程里有效——杀掉后台
    /// 重开,内存里什么都没有,哪怕算得再快也还是要算一遍。而真正不该算的是
    /// 「上次算过的答案,这次原样成立」这种情况,答案早就有了,只是没找地方放。
    ///
    /// 所以照着今日包、句子池那两份缓存的样子,**算完存盘,下次先读盘**。
    /// 存的钥匙就是内存那层同一把（`day|through|span`），当「记一条事件」这件事
    /// 本身就会触发 `refreshProjection()`（改了 `through`）时，旧的持久化缓存
    /// 自然对不上钥匙、下次读盘时自己判定过期——不需要另外一条「失效」的路，
    /// 失效和「钥匙对不上」是同一件事。
    func calendar(days span: Int = 7) -> (days: [ReviewCalendar.Day], streak: Int)? {
        guard let settings = schedulerSettings else { return nil }
        let today = ReviewCalendar.key(of: Date())
        let through = projection.throughLocalSequence
        let key = "\(today)|\(through)|\(span)"

        if let cache = calendarMemoryCache, cache.day == today, cache.through == through,
           cache.span == span {
            return cache.result
        }
        if let store = calendarCache, store.etag() == key,
           let data = store.load(),
           let persisted = try? JSONDecoder().decode(PersistedCalendar.self, from: data) {
            let result = (persisted.days, persisted.streak)
            calendarMemoryCache = (today, through, span, result)
            return result
        }

        // **复用重放时那一份，不再自己读一遍盘**（2026-09-19）。
        //
        // 原本这里自己 `events.load()`——于是进一次复习屏要把日志整份读两遍、
        // 解码两遍（一遍给投影、一遍给日历），而 `record()` 每记一条事件就触发
        // 一次投影刷新。真机上 799 条的时候，切一下选项卡就卡得看得出来。
        //
        // **这和 P8 §18 那个「越用越慢」是同一个形状的第三次**:
        // 贵的东西被放在了「每次都会走」的路上。
        guard let load = lastLoad ?? (try? events?.load()) else { return nil }
        let result = ReviewCalendar.build(load, weightDecay: weightDecay,
                                          settings: settings, today: today, span: span)
        calendarMemoryCache = (today, through, span, result)
        // **存盘失败就算了**——同发件箱那条纪律:这是一份缓存,不是事实,
        // 丢了的后果只是下次多算一遍,不该让复习屏因为一次写盘失败而打不开。
        if let store = calendarCache,
           let data = try? JSONEncoder().encode(
               PersistedCalendar(days: result.days, streak: result.streak)) {
            try? store.store(data, etag: key)
        }
        return result
    }

    /// 重置结果，给开发者选项那一屏显示。
    enum LocalLogReset: Equatable {
        /// 成功：丢掉了多少条、重新拉回来多少条。
        case done(discarded: Int, adopted: Int)
        /// **拒绝**：本地还有没上报的事实，丢掉它们就真的没了。
        case refusedPending(Int)
        case failed(String)
    }

    /// 把本地日志整份丢掉，从会合点重新拉一次。
    ///
    /// **为什么需要这个动作**（2026-09-18 加，开发者选项里）:日志从不回头改——
    /// 那是它的设计。而那天验收夹具经由拉取端点流进了设备的日志，
    /// 服务端清干净之后设备手里那份仍然是脏的。既然不许改，就得能整份丢掉重来。
    ///
    /// **先推，再看，才敢丢。** 设备是学习记录的第一副本，会合点只是会合点——
    /// 所以这个动作只在「本地没有任何还没上报的事实」时才无损。
    /// 推完还有待发就**拒绝并说出数目**，不给「要不要强制」那个选项:
    /// 一个能把自己的学习记录删掉的按钮，不该由一次点击决定。
    ///
    /// 放在开发者选项而不是设置里，同那一页其余五样的判据:它是开发期的工具，
    /// 而且它的后果需要看得懂日志的人来判断。
    func resetLocalLog() async -> LocalLogReset {
        guard let engine, let events else { return .failed("连接还没建好") }
        _ = await drain()
        do {
            let pending = try events.unreported(kinds: LoggedEvent.sendableKinds)
            guard pending.isEmpty else { return .refusedPending(pending.count) }

            let discarded = try events.load().events.count
            try events.reset()
            refreshProjection()
            let adopted = try await pullAndReconcile(engine)
            log?.write(.warn, "events.log.reset", "本地日志整份重拉了",
                       fields: ["discarded": String(discarded),
                                "adopted": String(adopted)])
            return .done(discarded: discarded, adopted: adopted)
        } catch {
            log?.write(.error, "events.log.reset.failed", "重拉没成功",
                       fields: ["error": String(describing: error)])
            return .failed(error.localizedDescription)
        }
    }

    /// Count telemetry files and the decoded log; never decode event history here.
    ///
    /// 2026-09-16 修：这里一度顺手调了 `outbox.pending()` 去数损坏条目，
    /// 而那个方法会**读取并解码发件箱里的每一个文件**——它是给上报用的。
    /// 而 `record()` 每记一条事件就调一次这里，于是每答一题就把待发的全部
    /// 事件重读重解一遍，答得越多越慢。真机上表现为「越用越卡」。
    ///
    /// 损坏条目改成按需算（`refreshDamagedCount`），它只有设置页要看。
    func refreshPendingCount() {
        // 发件箱只剩遥测了，**待发数要把日志里还没上报的那些算进去**——
        // 否则标了一个词、待发数显示 0，而它其实还没送出去。
        // `decided` 不算:它还没有去处（端点是 §6 的活），算进去就是永远非零的噪音。
        var unsent = 0
        if let events, let load = lastLoad {
            let cursor = events.cursor()
            unsent = load.events.count {
                LoggedEvent.sendableKinds.contains($0.kind)
                    && !cursor.isReported($0.localSequence)
            }
        }
        pendingEvents = (outbox?.count ?? 0) + unsent
    }

    /// 日志里一共多少条。**贵，所以只在有人要看的时候算**——它要把日志整个
    /// 读一遍解一遍。
    ///
    /// **不许塞进 `refreshPendingCount()`**:那个方法每记一条事件就被调一次
    /// （`record()` 里），而 2026-09-16 真机上「越用越慢」正是这么来的——
    /// 当时是往里塞了 `outbox.pending()`，它会读并解码发件箱里的每一个文件。
    /// 2026-09-18 我又往同一处塞了一次 `events.load()`，写下来免得有第三次。
    func refreshEventCount() {
        eventCount = (try? events?.load().events.count) ?? 0
    }

    /// 读不出来的事件文件有几条。**贵，所以只在有人要看的时候算**——
    /// 它要把发件箱整个读一遍。
    func refreshDamagedCount() {
        guard let outbox, let pending = try? outbox.pending() else {
            damagedEvents = 0
            return
        }
        damagedEvents = pending.damaged.count
    }

    /// 扔掉读不出来的那些事件文件。
    ///
    /// **这是个有损失的动作，所以只有人能按。**一条写坏的事件发不出去也读不出来，
    /// 于是待发数永远减不到零——而它代表的那次标记，扔掉就真的没了。
    /// 界面上必须把这句话说清楚，然后由人自己决定。
    @discardableResult
    func discardDamagedEvents() -> Int {
        guard let outbox, let damaged = try? outbox.pending().damaged, !damaged.isEmpty
        else { return 0 }
        try? outbox.discardDamaged(damaged)
        log?.write(.warn, "outbox.damaged.discarded", "扔掉了读不出来的事件",
                   fields: ["count": String(damaged.count)])
        refreshPendingCount()
        return damaged.count
    }

    @ObservationIgnored private var inFlightDrain: Task<SyncReport?, Never>?
    @ObservationIgnored private var drainRequested = false

    /// Owned by the app, not a reader/view task: leaving a screen must not
    /// cancel the already-durable mark's content request.
    func requestPreparation() {
        drainRequested = true
        guard inFlightDrain == nil else { return }
        Task { await drain() }
    }

    @discardableResult
    func drain() async -> SyncReport? {
        if let running = inFlightDrain { return await running.value }
        drainRequested = true
        let task = Task { await performRequestedDrains() }
        inFlightDrain = task
        let report = await task.value
        inFlightDrain = nil
        // A request can arrive after the task finishes but before this waiter
        // clears its handle. Give that request a new owner rather than lose it.
        if drainRequested { requestPreparation() }
        return report
    }

    private func performRequestedDrains() async -> SyncReport? {
        var report: SyncReport?
        repeat {
            drainRequested = false
            report = await performDrain()
        } while drainRequested
        return report
    }

    private func performDrain() async -> SyncReport? {
        guard let engine else { return nil }
        let before = pendingEvents
        preparationStatus = .running
        do {
            let report = try await engine.drain()
            lastSync = report
            refreshPendingCount()
            guard !report.offline else {
                preparationStatus = .offline
                return report
            }
            let adopted = try await pullAndReconcile(engine)
            if adopted > 0 {
                log?.write(.info, "sync.pulled", "收下了别处做的事",
                           fields: ["adopted": String(adopted)])
            }
            // A new local mark during push/pull must go through the next push
            // before we describe its latest snapshot to the content factory.
            guard !drainRequested else { return report }
            let snapshot = PoolSnapshot(projection: projection)
            if try await engine.reportPoolIfNeeded(
                snapshot, reportedAt: ISO8601DateFormatter().string(from: Date())) {
                log?.write(.info, "progress.pool.reported", "报了词池快照",
                           fields: ["entries": String(snapshot.entries.count),
                                    "through": String(snapshot.throughLocalSequence)])
            }
            if before > 0 || report.landed > 0 {
                log?.write(report.rejected > 0 ? .warn : .info, "sync.drained", "上报了一批",
                           fields: ["landed": String(report.landed),
                                    "duplicates": String(report.duplicates),
                                    "rejected": String(report.rejected),
                                    "remaining": String(report.remaining)])
            }
            guard !drainRequested else { return report }
            await acquirePreparedSentences(engine, snapshot: snapshot)
            return report
        } catch {
            notePreparationFailure(error)
            refreshPendingCount()
            return nil
        }
    }

    private func pullAndReconcile(_ engine: SyncEngine) async throws -> Int {
        defer {
            // Earlier pages remain durable when a later page fails. Reconcile
            // even on that failure, before any future local incremental append.
            reconcileProjection()
            refreshPendingCount()
        }
        return try await engine.pull()
    }

    private func acquirePreparedSentences(_ engine: SyncEngine,
                                          snapshot: PoolSnapshot) async {
        let required = Set(snapshot.entries.filter { $0.pool == .reviewing }.map {
            Projection.Key(itemType: $0.itemType, key: $0.key, senseId: $0.senseId)
        })
        let deadline = Date().addingTimeInterval(45)
        do {
            for attempt in 0..<24 {
                guard !drainRequested else { return }
                guard Date() < deadline else { break }
                let pool = try await engine.fetchSentences(allowCachedFallback: false)
                // The cache is already durable before observers see this pool.
                adoptSentences(pool)
                guard !drainRequested else { return }
                let items = pool.items.filter {
                    required.contains(Projection.Key(itemType: $0.item_type,
                                                     key: $0.item_key, senseId: $0.sense_id))
                }
                let ready = Set(items.filter {
                    $0.preparation_state == "ready" && SentencePreparation.isReady($0)
                }.map {
                    Projection.Key(itemType: $0.item_type, key: $0.item_key, senseId: $0.sense_id)
                })
                if required.isSubset(of: ready) {
                    preparationStatus = .idle
                    return
                }
                let failed = items.filter { $0.preparation_state == "failed" }
                let failedKeys = Set(failed.map {
                    Projection.Key(itemType: $0.item_type, key: $0.item_key, senseId: $0.sense_id)
                })
                if required.isSubset(of: ready.union(failedKeys)), let first = failed.first {
                    preparationStatus = .failed(
                        first.preparation_error ?? "双语句子准备失败，请稍后重试")
                    return
                }
                preparationStatus = .running
                if attempt < 23 {
                    try await Task.sleep(for: .seconds(2))
                }
            }
            preparationStatus = .waiting
        } catch {
            notePreparationFailure(error)
        }
    }

    private func notePreparationFailure(_ error: Error) {
        if let transport = error as? TransportError {
            switch transport {
            case .offline:
                preparationStatus = .offline
            case .server(let status, _):
                preparationStatus = .failed("同步或获取句子失败（HTTP \(status)），请稍后重试")
            case .upgradeRequired:
                preparationStatus = .failed("请更新 App 后继续同步")
            case .malformed:
                preparationStatus = .failed("服务器的句子或同步响应无法读取，请稍后重试")
            }
        } else {
            preparationStatus = .failed("同步或保存句子失败，请稍后重试")
        }
        log?.write(.warn, "review.preparation.failed", "同步或备句未完成",
                   fields: ["error": error.localizedDescription])
    }
}

/// 列表的离线退路。
///
/// Core 的 `SyncEngine.library()` 联不上就抛错，这是对的——它是个查询，不是
/// 今日包那种「离线形状」。但列表屏总得有东西显示，所以 App 自己把最后一次
/// 拿到的列表原样存一份。**存的是原始 JSON**，不是自己拆出来的结构：
/// 拆过一遍的数据在契约加字段之后会悄悄少东西，原始字节不会。
final class LibraryStore: @unchecked Sendable {
    private let directory: URL

    init(directory: URL) {
        self.directory = directory
        try? FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true)
    }

    private func file(_ key: String) -> URL {
        directory.appendingPathComponent("\(key).json")
    }

    func store(_ data: Data, key: String) {
        try? data.write(to: file(key), options: .atomic)
    }

    func load(key: String) -> Components.Schemas.LibraryResponse? {
        guard let data = try? Data(contentsOf: file(key)) else { return nil }
        return try? JSONDecoder().decode(
            Components.Schemas.LibraryResponse.self, from: data)
    }
}
