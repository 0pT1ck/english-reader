import Foundation
import Testing
@testable import ERCore

/// 重放出来的投影要守住那几条跨 Phase 不变量。
///
/// **这些不是「功能测试」,是守卫。** 破了它们的后果都是静默的:没有报错、没有日志,
/// 只有「没读过的文章里的词被记成已学、从此不再出现」这类事。
/// 服务端那边由 `verify_phase2.py` 的 7.1／7.4／7.13 守着,这几条是它们搬到
/// Core 之后的对应物。
struct ProjectionTests {

    static let settings = ReviewScheduler.Settings(
        requestRetention: 0.9,
        maximumInterval: 180,
        // FSRS-6 的 21 个内置默认值,和向量文件里那一组同源。
        parameters: [0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001,
                     1.8722, 0.1666, 0.796, 1.4835, 0.0614, 0.2629, 1.6483, 0.6014,
                     1.8729, 0.5425, 0.0912, 0.0658, 0.1542],
        enableFuzz: false
    )

    static func log(_ events: [(LoggedEvent.Kind, [String: JSONValue], String)])
        -> EventLogLoad {
        var list: [LoggedEvent] = []
        for (index, event) in events.enumerated() {
            list.append(LoggedEvent(localSequence: index, idemKey: "k\(index)",
                                    kind: event.0, occurredAt: event.2,
                                    payload: event.1))
        }
        return EventLogLoad(events: list, damaged: 0, tornTail: false)
    }

    /// 别的测试文件也用它（`SentencePoolTests`），所以不是 private。
    static func replay(_ events: [(LoggedEvent.Kind, [String: JSONValue], String)])
        -> Projection {
        Projection.replay(log(events), weightDecay: 0.5, settings: settings)
    }

    static func word(_ key: String, sense: Int = 0) -> Projection.Key {
        Projection.Key(itemType: "word", key: key, senseId: sense)
    }

    // MARK: 不变量

    /// 跨 Phase 不变量:**读完一篇文章只增加遇见次数,不改变词池位置。**
    /// 破了它,没读过的文章里的词会被静默记为已学、从此不再出现。
    @Test("读完只加遇见次数,一个词都不许进复习池")
    func finishingOnlyAddsEncounters() {
        let met: JSONValue = .array([
            .object(["item_key": .string("municipal"), "sense_id": .int(0), "n": .int(3)]),
            .object(["item_key": .string("rigorous"), "sense_id": .int(0), "n": .int(1)]),
        ])
        let projection = Self.replay([
            // 先标记，才有一条记录可以往上加——见下一条测试。
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
            (.read, ["article_id": .int(480), "met": met], "2026-01-01T09:00:00+00:00"),
            (.read, ["article_id": .int(481), "met": met], "2026-01-02T09:00:00+00:00"),
        ])
        #expect(projection.items[Self.word("municipal")]?.encounters == 6,
                "加的是出现次数(3)而不是加一，两篇共 6")
        #expect(projection.items[Self.word("municipal")]?.pool == .reviewing,
                "它是因为被标记才在复习池里，不是因为被读到")
    }

    /// 镜像服务端那句 `if key in known: touch_state(...)`：
    /// **读完不为没标过的词建记录。** 少了这一条，一篇文章会为它的每个实词
    /// 造一行，而那些词你并没有在学。
    @Test("读完不为没标过的词建记录")
    func finishingDoesNotInventRecords() {
        let met: JSONValue = .array([
            .object(["item_key": .string("never_marked"), "sense_id": .int(0), "n": .int(5)]),
        ])
        let projection = Self.replay([
            (.read, ["article_id": .int(480), "met": met], "2026-01-01T09:00:00+00:00"),
        ])
        #expect(projection.items.isEmpty, "一行都不该建，实际 \(projection.items.count)")
        #expect(projection.days["2026-01-01"]?.readArticles == 1, "但「读完一篇」要记上")
    }

    /// 旧客户端发的 `article.finished` 不带词表。
    /// **那就是没有——不去猜。**
    @Test("没有词表的读完事件只算一篇，不动任何遇见次数")
    func aReadEventWithoutAWordListChangesNoCounts() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
            (.read, ["article_id": .int(480)], "2026-01-01T09:00:00+00:00"),
        ])
        #expect(projection.items[Self.word("municipal")]?.encounters == 0)
        #expect(projection.days["2026-01-01"]?.readArticles == 1)
    }

    /// 跨 Phase 不变量:**进复习队列只有一条路——你自己标记。**
    @Test("只有标记能让一个词进复习池")
    func onlyAMarkPutsAnItemInThePool() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown"),
                       "article_id": .int(480), "sentence_id": .int(11)],
             "2026-01-01T09:00:00+00:00"),
        ])
        let item = projection.items[Self.word("municipal")]
        #expect(item?.pool == .reviewing)
        #expect(item?.marks == [.unknown])
        #expect(item?.introducedArticleId == 480)
        // 标记本身不算一次遇见——遇见是读完那一刻记的账。
        #expect(item?.encounters == 0)
    }

    /// 跨 Phase 不变量:**撤销标记后条目退回 `new`,但遇见记录保留**
    /// (它确实被遇见过)。
    @Test("撤销退回 new,而遇见次数和记忆状态都保留")
    func unmarkingKeepsWhatReallyHappened() throws {
        // 先标记再读完:遇见次数是挂在一条已有记录上的计数（镜像 `touch_state`）。
        let met: JSONValue = .array([
            .object(["item_key": .string("municipal"), "sense_id": .int(0), "n": .int(1)]),
        ])
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
            (.read, ["article_id": .int(480), "met": met], "2026-01-01T09:10:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-02T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-02T09:01:00+00:00"),
            (.unmarked, ["item_key": .string("municipal")], "2026-01-03T09:00:00+00:00"),
        ])
        let item = try #require(projection.items[Self.word("municipal")])
        #expect(item.pool == .new, "撤销之后要退回 new")
        #expect(item.marks.isEmpty)
        #expect(item.encounters == 1, "遇见记录保留——它确实被遇见过")
        #expect(item.memory != nil,
                "记忆状态也保留:你对这个词的记忆没有因为撤一个标记就重置")
    }

    /// 镜像服务端 `clear_mark` ＋ `demote_if_unmarked`:
    /// **标记是一个集合,一种都不剩才退回。**
    @Test("两种标记撤掉一种,还留在复习池里")
    func marksAreASetNotABoolean() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("account"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
            (.marked, ["item_key": .string("account"), "kind": .string("fuzzy")],
             "2026-01-01T09:05:00+00:00"),
            (.unmarked, ["item_key": .string("account"), "kind": .string("fuzzy")],
             "2026-01-01T09:10:00+00:00"),
        ])
        let item = projection.items[Self.word("account")]
        #expect(item?.marks == [.unknown])
        #expect(item?.pool == .reviewing, "还有一种标记在,不许退回")
    }

    /// 词组是一个整体:不认识 `account for` 不说明 `account` 怎么样。
    @Test("词组和它的组成词是两个条目")
    func aPhraseIsItsOwnItem() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("account for"),
                       "item_type": .string("phrase"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
        ])
        #expect(projection.items[Projection.Key(itemType: "phrase",
                                                key: "account for")]?.pool == .reviewing)
        #expect(projection.items[Self.word("account")] == nil,
                "标了词组不该把组成词也拖进来")
    }

    // MARK: 一轮与调度

    @Test("一轮两个方向都过才交给调度器,评级看的是当天失误几次")
    func theSchedulerRunsWhenTheRoundEnds() throws {
        // 第一问答错 → 退回第一向;再两次答对 → 一轮完成,失误 1 次 ⇒ Hard。
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(false)],
             "2026-01-02T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-02T09:05:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-02T09:10:00+00:00"),
        ])
        let item = try #require(projection.items[Self.word("municipal")])
        let memory = try #require(item.memory)
        // 失误一次 ⇒ Hard ⇒ 初始强度是参数表第二个数。
        #expect(abs(memory.stability - 1.2931) < 1e-6,
                "失误一次应当按 Hard 取初始强度,实际 \(memory.stability)")
        #expect(memory.reps == 1, "一天一轮 ＝ 一次复习,不是三次")
        #expect(memory.lapses == 0, "失手只在 Again 时记,Hard 不算")
        #expect(projection.days["2026-01-02"]?.answered == 3)
        #expect(projection.days["2026-01-02"]?.finishedRounds == 1)
    }

    @Test("当天标「模糊」的词,当天那一次封顶;用掉就没了")
    func todaysFuzzyMarkIsCappedOnce() throws {
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("fuzzy")],
             "2026-01-01T09:00:00+00:00"),
            // 同一天、零失误。封顶 ⇒ Hard 而不是 Good。
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-01T10:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-01T10:05:00+00:00"),
        ])
        let memory = try #require(projection.items[Self.word("municipal")]?.memory)
        #expect(abs(memory.stability - 1.2931) < 1e-6,
                "封顶应当按 Hard 算,实际 \(memory.stability)")
        #expect(projection.items[Self.word("municipal")]?.capped(on: "2026-01-01") == false,
                "封顶算出来就自己没了:这一轮之后不再是「从没排过期」")
    }

    @Test("拼写不动任何状态,只是记一笔")
    func spellingChangesNothing() {
        let marked: [(LoggedEvent.Kind, [String: JSONValue], String)] = [
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
        ]
        let without = Self.replay(marked)
        let with = Self.replay(marked + [
            (.spelled, ["item_key": .string("municipal"), "typed": .string("municiple")],
             "2026-01-01T11:00:00+00:00"),
        ])
        #expect(without.items == with.items, "拼错不影响复习安排(P3 决定 13)")
    }

    @Test("决策日志不参与任何状态")
    func decisionsAreRecordsNotState() {
        let projection = Self.replay([
            (.decided, ["kind": .string("today.assembled"),
                        "chosen": .array([.int(480), .int(479)])],
             "2026-01-01T04:00:00+00:00"),
        ])
        #expect(projection.items.isEmpty)
    }

    // MARK: 进度报告(§7)

    @Test("词池快照只含学过的,而且 new 一个都不带")
    func snapshotCarriesOnlyWhatIsBeingLearned() {
        let met: JSONValue = .array([
            .object(["item_key": .string("seen_only"), "sense_id": .int(0), "n": .int(2)]),
        ])
        let projection = Self.replay([
            (.read, ["article_id": .int(1), "met": met], "2026-01-01T09:00:00+00:00"),
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:10:00+00:00"),
        ])
        let snapshot = projection.poolSnapshot()
        #expect(snapshot.count == 1, "只有标过的那一个进报告,实际 \(snapshot.count)")
        #expect(snapshot.first?.key == "municipal")
        #expect(snapshot.first?.pool == .reviewing)
        // 只遇见过、没标的词不进报告——「不在大纲里」「遇见过」都不等于「在学」。
        #expect(!snapshot.contains { $0.key == "seen_only" })
    }

    @Test("坏行数照实带出来,不悄悄咽掉")
    func damageIsCarriedThrough() {
        var load = Self.log([
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:00:00+00:00"),
        ])
        load.damaged = 2
        let projection = Projection.replay(load, weightDecay: 0.5, settings: Self.settings)
        #expect(projection.damagedEvents == 2)
        #expect(projection.throughLocalSequence == 0)
    }

    // MARK: 今天的队列与那两个数（§1 那个 0/13 的落点）

    static let now = Projection.date(of: "2026-01-05T09:00:00+00:00")!

    @Test("今天标的进 today，更早标的进 due，都没排过期")
    func todayAndOverdueSplit() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("fresh"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
            (.marked, ["item_key": .string("owed"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
        ])
        let queue = projection.todayQueue(day: "2026-01-05", now: Self.now)
        #expect(queue.count == 2)
        #expect(queue.first { $0.key.key == "fresh" }?.bucket == .today)
        #expect(queue.first { $0.key.key == "owed" }?.bucket == .due,
                "标了却一直没排上的是欠着的，不是今天的")
    }

    @Test("排期没到点的不进队列;到点的进 due")
    func onlyDueItemsCome() throws {
        // 答完一轮 ⇒ 有了排期。Good 的首次间隔是 2 天。
        let events: [(LoggedEvent.Kind, [String: JSONValue], String)] = [
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-01T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-01T09:05:00+00:00"),
        ]
        let projection = Self.replay(events)
        let due = try #require(projection.items[Self.word("municipal")]?.memory?.dueAt)

        // 到期前一天:不该来。
        let before = due.addingTimeInterval(-86_400)
        #expect(projection.todayQueue(day: "2026-01-02", now: before).isEmpty,
                "没到点就不该问它")
        // 到期那一刻:来了，而且在 due 池。
        let queue = projection.todayQueue(day: "2026-01-03", now: due)
        #expect(queue.count == 1)
        #expect(queue.first?.bucket == .due)
        #expect(queue.first?.capped == false, "到期回来的不封顶——它不是几分钟前才读到的")
    }

    @Test("当天标「模糊」的封顶;标「不认识」的不封顶")
    func onlyFuzzyIsCapped() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("vague"), "kind": .string("fuzzy")],
             "2026-01-05T08:00:00+00:00"),
            (.marked, ["item_key": .string("unknown_one"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
        ])
        let queue = projection.todayQueue(day: "2026-01-05", now: Self.now)
        #expect(queue.first { $0.key.key == "vague" }?.capped == true)
        #expect(queue.first { $0.key.key == "unknown_one" }?.capped == false,
                "「不认识」在 P3 就是当天问、当天算全分")
    }

    @Test("new 与 graduated 都不进队列")
    func onlyReviewingItemsCome() {
        let met: JSONValue = .array([
            .object(["item_key": .string("seen_only"), "sense_id": .int(0), "n": .int(1)]),
        ])
        let projection = Self.replay([
            (.read, ["article_id": .int(1), "met": met], "2026-01-05T07:00:00+00:00"),
            (.marked, ["item_key": .string("dropped"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
            (.unmarked, ["item_key": .string("dropped")], "2026-01-05T08:30:00+00:00"),
        ])
        #expect(projection.todayQueue(day: "2026-01-05", now: Self.now).isEmpty,
                "只遇见过的、和撤销过的，都不该来")
    }

    /// **这条就是 §1 那个症状的守卫。** 答完一张，数字当场是 1/2；
    /// 而在 P9 之前它读的是服务端快照，刷新一次就倒退成 0/2。
    @Test("两张卡的数字是重放算出来的，答完当场对，再重放一次也不倒退")
    func theCountsNeverGoBackwards() {
        let events: [(LoggedEvent.Kind, [String: JSONValue], String)] = [
            (.marked, ["item_key": .string("one"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
            (.marked, ["item_key": .string("two"), "kind": .string("unknown")],
             "2026-01-05T08:01:00+00:00"),
            (.answered, ["item_key": .string("one"), "passed": .bool(true)],
             "2026-01-05T09:00:00+00:00"),
            (.answered, ["item_key": .string("one"), "passed": .bool(true)],
             "2026-01-05T09:01:00+00:00"),
        ]
        let progress = Self.replay(events).progress(day: "2026-01-05", now: Self.now)
        #expect(progress.total(.today) == 2)
        #expect(progress.done(.today) == 1, "答完一张就是 1，不是 0")
        #expect(progress.allDone == false)

        // 重放同一份日志任意多次，结果一样——**它是纯函数**，
        // 所以「刷新一下变回 0」这件事在结构上不可能再发生。
        let again = Self.replay(events).progress(day: "2026-01-05", now: Self.now)
        #expect(again == progress)
    }

    @Test("两个池子都走完才算「今天的所有复习」")
    func allDoneNeedsBothBuckets() {
        // 一张今天的、一张欠着的;只做完今天那张。
        let projection = Self.replay([
            (.marked, ["item_key": .string("fresh"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
            (.marked, ["item_key": .string("owed"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
            (.answered, ["item_key": .string("fresh"), "passed": .bool(true)],
             "2026-01-05T09:00:00+00:00"),
            (.answered, ["item_key": .string("fresh"), "passed": .bool(true)],
             "2026-01-05T09:01:00+00:00"),
        ])
        let progress = projection.progress(day: "2026-01-05", now: Self.now)
        #expect(progress.done(.today) == 1)
        #expect(progress.total(.due) == 1)
        #expect(progress.allDone == false, "另一个池子还欠着，不能说「今天的所有复习」")
        #expect(projection.spellingAvailable(day: "2026-01-05", now: Self.now,
                                             spelledToday: false) == false)
    }

    @Test("跨天就是新的一轮:昨天的进度不算今天的")
    func aNewDayIsANewRound() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("owed"), "kind": .string("unknown")],
             "2026-01-01T08:00:00+00:00"),
            // 昨天答了一半（只过了第一向）。
            (.answered, ["item_key": .string("owed"), "passed": .bool(true)],
             "2026-01-04T09:00:00+00:00"),
        ])
        let queue = projection.todayQueue(day: "2026-01-05", now: Self.now)
        #expect(queue.count == 1)
        #expect(queue.first?.state.direction == .wordToSense,
                "解锁每一轮重新挣（P3 决定 7），昨天挣到的不带到今天")
        #expect(queue.first?.state.asks == 0)
    }

    /// **这一条是被上面那条逼出来的守卫。** 一张卡答完之后有了未来的到期时间，
    /// 而它不该因此从今天的名单上消失——服务端那边它是一行带着 `done_at`
    /// 的队列行，照样在名单里。桶和封顶在入队那一刻就定死。
    @Test("答完的卡留在今天的名单上，不因为有了排期而消失")
    func aFinishedCardStaysOnTodaysList() throws {
        let projection = Self.replay([
            (.marked, ["item_key": .string("municipal"), "kind": .string("fuzzy")],
             "2026-01-05T08:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-05T09:00:00+00:00"),
            (.answered, ["item_key": .string("municipal"), "passed": .bool(true)],
             "2026-01-05T09:01:00+00:00"),
        ])
        let queue = projection.todayQueue(day: "2026-01-05", now: Self.now)
        #expect(queue.count == 1, "答完了也还在名单上，实际 \(queue.count) 条")
        let entry = try #require(queue.first)
        #expect(entry.state.done == true)
        #expect(entry.bucket == .today, "桶是入队那一刻定的，不是现算的")
        #expect(entry.capped == true, "封顶也是那一刻定的")
        #expect(projection.items[Self.word("municipal")]?.memory?.dueAt != nil,
                "它确实已经排到将来了——这正是会让现算出错的那个条件")
    }

    /// 镜像服务端 `session.collect` 那句 `item_type = 'word'`。
    ///
    /// **复习有意跳过词组**（`verify_phase3` 2.2 守的是「它是被有意跳过的，
    /// 不是碰巧没查到」），而 P6 定的界面初版也不认词组。
    /// 少了这一条，标过的词组会占着「共」那个数却永远问不出来——
    /// 句子池是按词与义项建的，词组拿不到句子，于是 13/13 永远到不了。
    @Test("标过的词组进词池，但不进复习队列")
    func phrasesStayOutOfTheQueue() {
        let projection = Self.replay([
            (.marked, ["item_key": .string("account for"),
                       "item_type": .string("phrase"), "kind": .string("unknown")],
             "2026-01-05T08:00:00+00:00"),
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-05T08:01:00+00:00"),
        ])
        let phrase = Projection.Key(itemType: "phrase", key: "account for")
        #expect(projection.items[phrase]?.pool == .reviewing,
                "词组照旧进词池——标记就是标记")
        #expect(projection.poolSnapshot().contains { $0.itemType == "phrase" },
                "也照旧上报给服务端:生文要避开它")

        let queue = projection.todayQueue(day: "2026-01-05", now: Self.now)
        #expect(queue.count == 1, "队列里只该有那个词，实际 \(queue.count) 条")
        #expect(queue.first?.key.itemType == "word")
        #expect(projection.progress(day: "2026-01-05", now: Self.now).total(.today) == 1,
                "词组不许占着「共」那个数")
    }

    /// `verify_phase3` 1.6 在服务端守的是「记忆状态存进库再取出来还是同一张卡」。
    /// 搬到客户端之后，「库」是事件日志，而记忆状态是算出来的——
    /// 所以这里守的是它**编码往返**不掉东西:投影要把它交给下一次重放，
    /// 而下一次重放是从头算的。
    @Test("记忆状态编码往返不掉东西")
    func memoryStateSurvivesEncoding() throws {
        let state = ReviewScheduler.MemoryState(
            stability: 12.3456789, difficulty: 6.5, dueAt: Self.now,
            lastReviewAt: Self.now.addingTimeInterval(-86_400),
            reps: 4, lapses: 1, fsrsState: 2)
        let data = try JSONEncoder().encode(state)
        let back = try JSONDecoder().decode(ReviewScheduler.MemoryState.self, from: data)
        #expect(back == state)
    }
}
