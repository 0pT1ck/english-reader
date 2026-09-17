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
            .object(["item_key": .string("municipal"), "sentence_id": .int(11)]),
            .object(["item_key": .string("rigorous"), "sentence_id": .int(12)]),
        ])
        let projection = Self.replay([
            (.read, ["article_id": .int(480), "met": met], "2026-01-01T09:00:00+00:00"),
            (.read, ["article_id": .int(481), "met": met], "2026-01-02T09:00:00+00:00"),
        ])
        #expect(projection.items[Self.word("municipal")]?.encounters == 2)
        #expect(projection.items[Self.word("municipal")]?.pool == .new,
                "读完不许改词池位置")
        #expect(projection.items.values.allSatisfy { $0.pool == .new })
        #expect(projection.items[Self.word("municipal")]?.introducedArticleId == 480,
                "首次遇见记的是第一篇,不是最后一篇")
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
        let met: JSONValue = .array([.object(["item_key": .string("municipal")])])
        let projection = Self.replay([
            (.read, ["article_id": .int(480), "met": met], "2026-01-01T09:00:00+00:00"),
            (.marked, ["item_key": .string("municipal"), "kind": .string("unknown")],
             "2026-01-01T09:10:00+00:00"),
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
        #expect(projection.items[Self.word("municipal")]?.cappedToday == false,
                "封顶用掉就没了——它说的是「你几分钟前才读到它」")
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
        let met: JSONValue = .array([.object(["item_key": .string("seen_only")])])
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
}
