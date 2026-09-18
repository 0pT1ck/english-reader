"""Phase 9 的验收：重构。

**这个 Phase 没有新功能，所以验的不是「它能做什么」，而是「东西在哪一侧」。**
那条线（用户 2026-09-17 定）是：服务端只管生文，客户端拿到文、学习、把进度汇报
回去，服务端照着进度继续生文。所以下面绝大多数检查都是**读源码断言某件事不在
某一侧**——而那正是这种重构唯一验得动的东西。

**这一份不重复别人守着的东西。** 学习规则搬进 Core 之后由 `swift test` 守
（CI 上 95 项以上），而 Core 在 r5s 上编不出来——**没有 Swift 工具链**，那是
「开发场地」那条决定的代价。所以这里守的是**向量与样本确实覆盖了那几条规则**，
以及**服务端不再实现它们**；规则本身对不对，由 CI 上那一套负责。
这两件事合起来才是完整的网：一半在这儿，一半在 CI。

**两条脚本自己要守的规矩**（坑 §4.1、§4.4）：
不许依赖「今天碰巧有数据」；整段包进 `notifications.muted()`。
这一份**只读不写**，所以连探针都不用种——那是它的一个优点，不是巧合：
「东西在哪一侧」是关于代码的事实，不是关于数据的。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CLIENT = ROOT / "client"
BACKEND = ROOT / "backend"

passed: list[str] = []
failed: list[str] = []


def check(item: str, title: str, ok: bool, detail: str = "") -> bool:
    line = f"  [{'通过' if ok else '失败'}] {item} {title}"
    if detail:
        line += f" — {detail}"
    print(line)
    (passed if ok else failed).append(item)
    return ok


def section(title: str) -> None:
    print(f"\n{title}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def code_only(source: str) -> str:
    """把注释剥掉再扫。

    **第一版的四条断言全栽在这上面**（2026-09-18）：查「代码还读不读
    `buckets_done`」，查到的是注释里那句「在这之前这几个数来自
    `progress.buckets_done`」；查「还有没有手动 `todayDone += 1`」，
    查到的是注释里那句「上一版在这里手动 `todayDone += 1`」。

    形状正是坑 §1.2——**一个错误的度量比没有度量更危险**：这几条断言当时红着，
    而它们指向的东西其实一点问题都没有，差一点就去改根本没错的代码。
    而这个项目的注释写得越详细，「注释里提到某个名字」就越不能当成
    「代码里用着它」。

    只剥行注释（`//` 与 `///`），够用:Swift 的块注释在这个代码库里没人用，
    而字符串里的 `//` 只出现在 URL 上，那不影响这几条断言。
    """
    return re.sub(r"^\s*///?.*$", "", source, flags=re.M)


def main() -> int:
    from backend.core import notifications

    print("=" * 62)
    print("Phase 9 验收：重构")
    print("=" * 62)

    with notifications.muted():
        section("1. 那条线：谁该干什么")

        # 服务端拿学习记录只做一件事:避开在学的词、保证有生词。
        # 而它现在拿的是客户端上报的**快照**，不是自己从 study_states 推的。
        daily = read(BACKEND / "modules/generation/daily.py")
        check("1.1", "生文读的是客户端上报的词池快照，不是自己推的",
              "from backend.modules.progress import module as progress" in daily
              and "progress.words_in_progress()" in daily,
              "服务端与学习记录唯一的解释关系，而它连解释都不算:直接用")

        check("1.2", "没有快照时大声回落，不静默当成「一个词都没学」",
              "generation.pool.snapshot_missing" in daily,
              "静默回落会让「从没报过」和「一个都没学」长得一样，"
              "于是把在学的词当生词再教一遍——而那是静默的")

        progress_mod = read(BACKEND / "modules/progress/module.py")
        check("1.3", "词池快照是「替换」而不是「合并」",
              "DELETE FROM learner_pool WHERE learner_id = ?" in progress_mod
              and "conn.rollback()" in progress_mod,
              "逐条 upsert 读不出一个「不出现」——撤销标记之后那个词"
              "在新快照里正是「不出现」")

        check("1.4", "`words_in_progress` 返回可选值，区分「不知道」与「一个都没有」",
              "set[str] | None" in progress_mod,
              "空集合是「一个都没学」，None 是「还没人报过」，两件事差别很大")

        section("2. 五种事件")

        event_log = read(CLIENT / "Sources/ERCore/EventLog.swift")
        kinds = re.findall(r"^\s*case (marked|unmarked|read|answered|spelled|decided)$",
                           event_log, re.M)
        check("2.1", "日志里只有那五种事件（标记/撤销算一对）",
              sorted(set(kinds)) == ["answered", "decided", "marked", "read",
                                     "spelled", "unmarked"],
              f"{sorted(set(kinds))}——别的都算得回来，所以别往这里加")

        check("2.2", "遥测不进日志",
              'case "word.marked": .marked' in event_log
              and "default: nil" in event_log,
              "打开文章、读到哪、点了哪个词不改变任何状态，"
              "放进日志只会让重放变慢、让「五种」那条规矩变松")

        check("2.3", "决策事件没有去处时不算在待发里",
              "sendableKinds" in event_log
              and ".decided: return nil" in event_log,
              "端点还没做（§6 的活）。不过滤的话它会永远堆着，"
              "把待发数变成一个永远非零的噪音")

        app_model = read(CLIENT / "App/Sources/AppModel.swift")
        check("2.4", "一条事件只有一个家",
              "entry.loggedKind, let events" in app_model
              and "} else {\n                try outbox.append(entry)" in app_model,
              "从前两处都写:崩在两次写之间，事件在日志里而发件箱里没有，"
              "于是它永远发不出去，而屏幕上什么都不会说")

        section("3. 屏幕上没有一个数字等网络")

        review_model = read(CLIENT / "App/Sources/ReviewModel.swift")
        check("3.1", "复习那几屏不再读服务端快照里的进度",
              "buckets_done" not in code_only(review_model),
              "包里那个数是服务端上次收到上报时的样子——读它就是把那个 0/13 请回来")

        check("3.2", "两张卡的数字由重放算出来",
              "app.projection.todayQueue(" in review_model
              and "entries.count {" in review_model,
              "只有一个来源，所以「刷新一下变回 0」在结构上不可能")

        check("3.3", "作答之后不手动加数，而是重放",
              "todayDone += 1" not in code_only(review_model)
              and "sync(app)" in review_model,
              "「同一个数有两处在写」正是这个 Phase 要消掉的东西")

        projection = read(CLIENT / "Sources/ERCore/Projection.swift")
        check("3.4", "桶与封顶在入队那一刻定死，之后只读不算",
              "public var bucket: Bucket" in projection
              and "if let round = rounds[key], round.day == day" in projection,
              "现算的话，一轮做完之后条目有了排期，"
              "那张卡会从今天的名单上消失、「已复习」那个数跟着丢")

        check("3.5", "复习不取词组",
              'key.itemType == "word"' in projection,
              "镜像服务端 `collect` 那句 item_type = 'word'。"
              "少了它，标过的词组会占着「共」那个数却永远问不出来")

        section("4. 向量与样本：Core 那一半的网")

        review_vectors = CLIENT / "Tests/ERCoreTests/Fixtures/review-vectors.json"
        sched_vectors = CLIENT / "Tests/ERCoreTests/Fixtures/scheduler-vectors.json"
        check("4.1", "两套向量都在",
              review_vectors.exists() and sched_vectors.exists(),
              "一套是当天那一轮的状态机，一套是排期——"
              "后者是 P9 新建的，它是删掉服务端 py-fsrs 的前置条件")

        doc = json.loads(read(sched_vectors) or "{}")
        grades = (doc.get("ratings") or {}).get("cases") or []
        schedules = (doc.get("schedules") or {}).get("cases") or []
        check("4.2", "评级映射是全枚举",
              len(grades) == 32,
              f"{len(grades)} 条 ＝ misses 0-3 × easy × revealed × capped。"
              "全枚举的表不会在中间有个缺口")

        names = " ".join(c.get("name", "") for c in schedules)
        check("4.3", "排期向量钉住了那几条有明文依据的",
              "180" in names and "封顶" in names and "提示" in names
              and "自称简单" in names and "短期" in names,
              f"{len(schedules)} 个场景，含 maximum_interval 封顶、提示降一档、"
              "自称简单、同一天又答一次")

        sequence = [round(s["interval_days"]) for c in schedules
                    if len(c.get("reviews") or []) == 5
                    for s in c.get("expected", [])]
        check("4.4", "间隔序列是 2→11→46→163→180，不是 8→66→180",
              sequence == [2, 11, 46, 163, 180],
              f"{sequence}——后者是当年接错线的那一版（坑 §4.5）")

        settings = doc.get("settings") or {}
        check("4.5", "向量带着它生成时用的那组参数，不靠任何一边的「默认」",
              len(settings.get("parameters") or []) == 21
              and settings.get("enable_fuzzing") is False,
              f"{len(settings.get('parameters') or [])} 个参数（FSRS-6）、抖动关。"
              "两个包的「默认」不是同一组数，各取自己的就会跑出不同的间隔"
              "——而两边都不会报错")

        section("5. 稳定键与那张永不删除的映射表")

        from backend.core.db import get_connection
        from backend.modules.senses import keys as sense_keys

        content = get_connection("content")
        total, keyed, distinct = content.execute(
            "SELECT COUNT(*), COUNT(sense_key), COUNT(DISTINCT sense_key) FROM senses"
        ).fetchone()
        check("5.1", "每条义项都有稳定键，且键唯一",
              total == keyed == distinct and total > 0,
              f"{total} 条，{keyed} 条有键，{distinct} 个不同的键")

        check("5.2", "键是纯函数：同一段概念总得到同一个键",
              sense_keys.sense_key("account", "A record of money spent!")
              == sense_keys.sense_key("account", "a record of money spent"),
              "归一化只做小写、去标点、折空白——不做词干化，"
              "那会把两句不同的话判成同一个键，连带把两个义项的标注合到一起")

        tables = {r["name"] for r in content.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        check("5.3", "退休表与映射表都在",
              {"senses_retired", "sense_key_map"} <= tables,
              "退休而不是删除:一条两个月前的标记仍然指得到一个说得出话的义项")

        senses_repo = read(BACKEND / "modules/senses/repository.py")
        check("5.4", "`store_senses` 不再删了重插",
              "DELETE FROM senses WHERE headword = ?" not in senses_repo
              and "INSERT INTO senses_retired SELECT" in senses_repo,
              "从前每一次调用都让这个词的义项 id 全变，而 9 万多条标注指着它们")

        check("5.5", "退休过的键回来时复活原来那一行",
              "senses.revived" in senses_repo,
              "不然「同一个键总是同一个 id」就不成立:"
              "退休一次再加回来，指着它的标注照旧作废")

        check("5.6", "按 id 反查找得到退休的义项",
              "FROM senses_retired WHERE id = ?" in senses_repo,
              "而列义项的地方照旧只列现行的——那二十处读取口一处都不用改")

        section("6. 双向同步")

        reading_routes = read(BACKEND / "modules/reading/routes.py")
        check("6.1", "有按序号往后取事件的端点",
              'client_router.get("/events"' in reading_routes,
              "在这之前只有上报——一台设备把事件送上来，另一台永远看不到")

        reading_repo = read(BACKEND / "modules/reading/repository.py")
        check("6.2", "全序序号用的是那张表本来就有的自增 id",
              "def events_after(" in reading_repo
              and "ORDER BY id LIMIT ?" in reading_repo,
              "client_events 从 P2 起就是 AUTOINCREMENT。"
              "新建一套会得到第二个顺序，然后两个顺序说反话")

        sync = read(CLIENT / "Sources/ERCore/Sync.swift")
        check("6.3", "拉回自己推上去的事件按幂等键跳过",
              "known.contains(item.idemKey)" in sync,
              "比让服务端按设备过滤好:那要服务端知道「哪台设备产生了哪条」，"
              "而设备换了令牌就不认了，那时它会以为自己的历史不存在")

        check("6.4", "翻页有上限，不写「跑到服务端说没有为止」",
              "for _ in 0..<200" in sync,
              "服务端一直说 more: true 时那个循环没有出口（坑 §7.2）")

        check("6.5", "先推后拉，一趟里完成",
              "let adopted = (try? await engine.pull()) ?? 0" in app_model,
              "反过来的话这一趟拉不到自己刚做的事，"
              "中间那段「别人的变化看得见、自己的看不见」最难解释")

        section("7. 排期参数从服务端来，不在客户端写默认值")

        today_contract = read(BACKEND / "modules/today/contract.py")
        check("7.1", "今日包下发排期参数",
              "fsrs_parameters" in today_contract
              and "fsrs_maximum_interval" in today_contract,
              "排期搬到设备上之后，设备必须知道服务端配的是什么")

        today_service = read(BACKEND / "modules/today/service.py")
        check("7.2", "下发的是实际生效的参数向量，不是「空表示用默认」",
              "review_scheduler.scheduler(fuzz=False).parameters" in today_service,
              "配置项默认是空的，意思是「用包自带的」——而两个包自带的不是同一组")

        scheduler_swift = read(CLIENT / "Sources/ERCore/Scheduler.swift")
        check("7.3", "客户端没有本地默认参数，参数个数不对就抛错",
              "case wrongParameterCount" in scheduler_swift
              and "static let parameterCount = 21" in scheduler_swift
              and "throw SchedulerError.wrongParameterCount" in code_only(scheduler_swift),
              "宁可抛错也不猜:19 个是 FSRS-5，21 个是 FSRS-6，"
              "拿 19 个去跑 6 的公式不会报错，只会给出别的间隔。"
              "**断言那个常量本身，不扫「19」这个字串**——"
              "`w[19]` 是个真的数组下标，第一版就栽在这儿")

        check("7.4", "服务端没下发就说出来，不回落到编出来的参数",
              "settings.scheduler.missing" in app_model
              and "unusableSettings" in app_model,
              "宁可缺一半，也不要一半是编的")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败")
    if failed:
        print("  失败：" + "、".join(failed))
    print("\n另一半在 CI 上：`client.yml` 的 `swift test`（95 项以上）。")
    print("r5s 上编不出 Core（没有 Swift 工具链），这是「开发场地」那条决定的代价。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
