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

    /// 重放出来的投影。记一条事件就重算一次。
    ///
    /// **「重放是毫秒级的」这句话曾经写在这里，而它没有一直成立**
    /// （2026-09-19 真机上栽的:399 条事件、格式器与日历现建、日历那半再
    /// 重复重放,单次日历计算实测 1135ms）。修完格式器缓存和真正的增量重放，
    /// 单次全量重放现在确实是几十毫秒级——但那是「算得快」,不是「不用算」。
    /// **投影本身依然每次重算,不缓存**：它的输入(整份日志)和输出都在内存里,
    /// 一次会话里最多算一次,这个便宜。真正值得存盘的是下面日历那份——
    /// 它要在两百多个空日子上重复求值,便宜的操作乘以四百天就不便宜了。
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
            refreshPendingCount()
            refreshProjection()
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
            transport: IOSTransport(baseURL: url, token: connection.token, log: log),
            outbox: outbox, events: events, articles: cache, day: dayCache,
            sentences: sentenceCache
        )
    }

    // MARK: 发件箱

    /// 记一条事件。**先落盘再说成功**——反过来的话用户会看见「已标记」
    /// 而它其实没写进去。
    @discardableResult
    func record(_ entry: OutboxEntry) -> Bool {
        guard let outbox else { return false }
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
                try events.append(kind: kind, payload: entry.payload,
                                  idemKey: entry.idemKey, occurredAt: entry.occurredAt)
            } else {
                try outbox.append(entry)
            }
            refreshPendingCount()
            refreshProjection()
            log?.write(.debug, "outbox.recorded", "记下一条事件",
                       fields: ["kind": entry.kind.rawValue, "pending": String(pendingEvents)])
            return true
        } catch {
            // **这一条必须记。**写不进发件箱意味着那次标记消失了，而屏幕上
            // 什么都不会说——正是「静默失败」那一类里最贵的一种。
            log?.write(.error, "outbox.record.failed", "事件没能落盘",
                       fields: ["kind": entry.kind.rawValue,
                                "error": error.localizedDescription])
            return false
        }
    }

    /// 把词池快照报上去。
    ///
    /// **失败不作声，下一趟再来**——同发件箱那条纪律。快照是可重算的值，
    /// 报晚一点的后果是生成那一轮可能把一个在学的词当生词，
    /// 而那比「把界面卡在一个报不上去的请求上」轻得多。日志里要说，
    /// 因为「服务端手上那份是什么时候的」只有这里查得到。
    private func reportPool(_ engine: SyncEngine) async {
        let entries = projection.poolSnapshot()
        let stamp = ISO8601DateFormatter().string(from: Date())
        do {
            let count = try await engine.reportPool(entries, reportedAt: stamp)
            log?.write(.info, "progress.pool.reported", "报了词池快照",
                       fields: ["entries": String(count)])
        } catch {
            log?.write(.warn, "progress.pool.report.failed", "词池快照没报上去",
                       fields: ["entries": String(entries.count),
                                "error": error.localizedDescription])
        }
    }

    /// 重放一次日志。记一条事件之后、以及启动时各一次。
    ///
    /// **算不了排期就不算，但照样重放。** 没有 `schedulerSettings` 时
    /// 记忆状态那一半是空的，而词池、遇见次数、当天那一轮照样对——
    /// 半个投影比一个用猜来的参数算出来的完整投影有用得多。
    /// 上一次重放用的那份日志。**日历复用它**——同一份东西读两遍是白读，
    /// 而它在 799 条的时候就已经看得出来了。
    ///
    /// 它总是新的:唯一往日志里写东西的路径（`record()`、`pull()`、重置）
    /// 写完都会调 `refreshProjection()`。
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
    }

    /// 服务端还没下发排期参数时用的占位。
    ///
    /// **参数个数故意是错的**，所以 `ReviewScheduler.review` 会抛
    /// `wrongParameterCount` 而不是算出一个数来。投影因此拿不到记忆状态，
    /// 而界面据此说「服务端还没更新」——**宁可缺一半，也不要一半是编的**。
    private static let unusableSettings = ReviewScheduler.Settings(
        requestRetention: 0.9, maximumInterval: 180, parameters: [], enableFuzz: false)

    /// 今日包带来的那几个服务端参数。**每次取到包都调它。**
    func adopt(_ package: DayPackage) {
        weightDecay = package.settings.weight_decay
        schedulerSettings = package.schedulerSettings
        if schedulerSettings == nil {
            log?.write(.warn, "settings.scheduler.missing",
                       "今日包里没有排期参数——服务端还是旧镜像")
        }
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
            let adopted = try await engine.pull()
            refreshProjection()
            refreshPendingCount()
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

    /// 待发条数。**只数文件，不读文件。**
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
        if let events, let pending = try? events.unreported(kinds: LoggedEvent.sendableKinds) {
            unsent = pending.count
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

    /// 正在跑的那一趟同步。**并发进来的调用等它，不另起一趟。**
    private var inFlightDrain: Task<SyncReport?, Never>?

    /// 把攒着的事件发出去。**界面上失败不作声**——发件箱的全部意义就是失败了
    /// 可以再来；但日志里要说，那是「我标的东西传上去了没有」唯一查得到的地方。
    ///
    /// **单飞:同一时刻只有一趟**（2026-09-18 真机上栽的）。
    /// 这个类是 `@MainActor`，而 **MainActor 不防重入**——`await` 一让出去，
    /// 第二个调用就从头跑起来了。而调用方有七处:启动的 `.task`、
    /// `scenePhase == .active`、阅读屏、复习屏、拼写屏、设置屏、开发者选项。
    /// 启动那一下 `.task` 和 `.active` 几乎同时到，于是两趟并发。
    ///
    /// 后果不是「多打一次请求」:两趟 `pull()` 各自在开头取一次「已知幂等键」的
    /// 快照，于是**同一批事件被写进日志两遍**（真机上 399 条变 798 条），
    /// 而两边的 `markReported` 各读各写游标、互相覆盖，那 798 条大半又算成
    /// 「还没上报」。日志里每一行都是双份，那正是认出这件事的地方。
    ///
    /// **等它而不是丢掉它**:`await app.drain()` 的含义是「一趟同步完成了」，
    /// 直接返回 nil 会让调用方以为同步过了而其实没有。
    @discardableResult
    func drain() async -> SyncReport? {
        if let running = inFlightDrain { return await running.value }
        let task = Task { await performDrain() }
        inFlightDrain = task
        let report = await task.value
        inFlightDrain = nil
        return report
    }

    private func performDrain() async -> SyncReport? {
        guard let engine else { return nil }
        let before = pendingEvents
        do {
            let report = try await engine.drain()
            // **先推后拉。** 顺序有意义:推完再拉，自己刚推上去的那些会在同一趟
            // 回来（游标是「大于某个号」），按幂等键跳过——而反过来的话，
            // 这一趟拉不到自己刚做的事，下一趟才拉到，中间那段时间另一台设备的
            // 变化看得见、自己的看不见，那种半态最难解释。
            let adopted = (try? await engine.pull()) ?? 0
            if adopted > 0 {
                log?.write(.info, "sync.pulled", "收下了别处做的事",
                           fields: ["adopted": String(adopted)])
                refreshProjection()
            }
            // **词池变了才报**（P9 §7）。词池是事件的函数，事件没动它就没动——
            // 所以只在这一趟真的推上去或拉下来了东西时报一次。
            // 每次 drain 都报也不贵（实测 30 条），但那会让日志里
            // 「什么时候变过」看不出来。
            if report.landed > 0 || adopted > 0 {
                await reportPool(engine)
            }
            lastSync = report
            refreshPendingCount()
            // 上报把游标推了，待发数跟着变；投影不受影响（上报不改事实），
            // 所以这里只刷数，不重放。

            if before > 0 || report.landed > 0 {
                log?.write(report.rejected > 0 ? .warn : .info, "sync.drained", "上报了一批",
                           fields: ["landed": String(report.landed),
                                    "duplicates": String(report.duplicates),
                                    "rejected": String(report.rejected),
                                    "remaining": String(report.remaining),
                                    "offline": report.offline ? "yes" : "no"])
            }
            return report
        } catch {
            log?.write(.warn, "sync.drain.failed", "这一批没送出去",
                       fields: ["pending": String(before),
                                "error": error.localizedDescription])
            refreshPendingCount()
            return nil
        }
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
