"""Run the Phase 4 verification checklist.

    uv run python scripts/verify_phase4.py

P4 是每日供给与今日包：每天早上四点自己备好三篇，打开就是今天的包，
断网也能把一天走完，回来对得上账。

这份清单守的四件事，破了都不会报错：

* **补跑一次，不是补跑五次。** 调度状态存在库里而不是内存里，停机三天之后
  应当只补一次。判断错了不会有任何异常，只会某天早上多出三倍的文章。
* **合格线要两头验。** 一个在好稿上报零的检查，不等于它有效——还得看它在
  坏稿上报不报（坑 §4.3）。所以阴性与阳性对照都种，用完删掉。
* **备好的文章必须真的能打开。** 标注没跑完的文章停在 `annotating`，在货架上
  看着一切正常，点开是空的。这个失败全程零日志，实测过两次。
* **幂等键真的在去重。** 补报会重发，重发不能重复计分。这是离线形状唯一
  说得清的判据。

脚本会写库：它要种探针、跑一次备稿的干跑、补报一条作答。前后各清一次
（坑 §4.1），中断的上一轮不会污染下一轮。
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core import runtime_config, tasks  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core import notifications  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402  installs modules and migrations
from backend.modules.generation import daily, pipeline  # noqa: E402
from backend.modules.llm import client, jobs  # noqa: E402

passed: list[str] = []
failed: list[str] = []
manual: list[str] = []

log = get_logger("scripts.verify4")
SECRET = get_settings().admin_secret
PROBE = "probe.p4"


def check(number: str, title: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(number)
    print(f"  [{'通过' if ok else '失败'}] {number} {title}" + (f" — {detail}" if detail else ""))


def note(number: str, title: str, detail: str) -> None:
    manual.append(number)
    print(f"  [人工] {number} {title} — {detail}")


class Report:
    """A checker report with only the fields the bar looks at."""

    def __init__(self, **kw):
        self.beyond_rate = 0.4
        self.target_hits = {f"w{i}": True for i in range(25)}
        self.targets_per_paragraph = [5, 5, 5, 5, 5]
        self.words = 450
        self.avg_sentence = 16.0
        for k, v in kw.items():
            setattr(self, k, v)


def cleanup(conn) -> None:
    """Remove every trace of this script's probes. Runs before and after."""
    conn.execute("DELETE FROM task_runs WHERE name LIKE 'probe.%'")
    conn.execute("DELETE FROM client_events WHERE idem_key LIKE 'p4probe-%'")
    for key in (f"task_{PROBE.replace('.', '_')}_enabled",
                f"task_{PROBE.replace('.', '_')}_schedule"):
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    conn.commit()


def main() -> int:  # noqa: PLR0912,PLR0915 - a checklist reads better in one place
    # 这份清单会故意制造错误（任务抛异常、周期写坏），而 ERROR 是会推到手机的。
    # 跑一次验收推两条假警报，正是 notifications 自己那句「狼来了的通道会被忽略」。
    with notifications.muted(), trace():
        conn = get_connection("learning")
        cleanup(conn)
        now = datetime.now(timezone.utc)

        # --- A. 框架：定时任务 --------------------------------------------- #
        print("\nA. 框架能力 · 定时任务")

        fired: list[int] = []
        tasks.register(tasks.Task(
            name=PROBE, title="验收探针", schedule="04:00",
            run=lambda: fired.append(1),
        ))
        check("A1.1", "任务登记即挂载，并自带两个配置项",
              runtime_config.get(tasks.enabled_key(PROBE)) is True
              and str(runtime_config.get(tasks.schedule_key(PROBE))) == "04:00",
              "开关与周期都是登记时自动建的，加任务不用记得加设置")

        check("A1.2", "「04:00」说的是本地时间，不是 UTC",
              tasks.next_due("04:00", now).astimezone().hour == 4,
              f"下一次 {tasks.next_due('04:00', now).astimezone():%Y-%m-%d %H:%M %Z}"
              f"（按 UTC 解释的话夜里的活会跑在中午）")

        check("A1.3", "刚登记不算到点",
              PROBE not in tasks.due_tasks() and not fired,
              "十点装上的模块不会立刻跑四点的活")

        conn.execute("UPDATE task_runs SET next_due_at = ? WHERE name = ?",
                     ((now - timedelta(days=3)).isoformat(timespec="seconds"), PROBE))
        conn.commit()
        check("A2.1", "停机期间错过的，判为该跑", PROBE in tasks.due_tasks(),
              "到点时间已过三天")
        tasks.run_now(PROBE)
        check("A2.2", "补跑一次，不是补跑三次",
              len(fired) == 1 and PROBE not in tasks.due_tasks(),
              f"跑了 {len(fired)} 次，跑完立刻不再到点")

        state = tasks.state_of(PROBE) or {}
        check("A2.3", "跑过的记录落库，重启读得回",
              state.get("runs") == 1 and bool(state.get("last_trace_id")),
              f"次数 {state.get('runs')}，trace {str(state.get('last_trace_id'))[:8]}…")

        def boom() -> None:
            raise RuntimeError("验收里故意炸的")

        tasks.register(tasks.Task(name=PROBE + ".boom", title="验收探针·失败",
                                  schedule="30m", run=boom))
        outcome = tasks.run_now(PROBE + ".boom")
        after = tasks.state_of(PROBE + ".boom") or {}
        check("A3.1", "任务失败被记下来，并排上下一次",
              outcome["status"] == "failed" and after.get("next_due_at"),
              "ERROR 级——Bark 的告警处理器挂在 logging 上，所以失败会推到手机")

        runtime_config.set(tasks.schedule_key(PROBE), "每天四点")
        check("A3.2", "周期写坏了不会静默不跑",
              PROBE not in tasks.due_tasks(),
              "看不懂的周期跳过并报 ERROR，而不是安静地再也不跑")
        runtime_config.set(tasks.schedule_key(PROBE), "04:00")

        # 「没有下次运行时间」是真实可达的状态：周期在收尾那一刻解析失败，
        # next_due_at 就被写成 NULL。而没有这个时间的任务永远不到点——
        # 把周期改回正确的也救不回来，全程零报错。2026-09-11 实测踩到过，
        # 夜间备稿任务因此静静躺了二十分钟。
        conn.execute("UPDATE task_runs SET next_due_at = NULL WHERE name = ?", (PROBE,))
        conn.commit()
        tasks.due_tasks()
        repaired = (tasks.state_of(PROBE) or {}).get("next_due_at")
        check("A3.3", "「没有下次运行时间」的任务会被重新排上",
              bool(repaired) and PROBE not in tasks.due_tasks(),
              f"重排到 {repaired}，而且不是排到「立刻」——"
              f"修复动作不该顺手多跑一次生成")

        # A2 验的是「到点了算不算该跑」，那是纯函数。这一条验的是另一件事：
        # **调度循环有没有真的接进服务、会不会自己把该跑的跑起来**。
        # 「方案写了但代码没写」在这个项目里发生过（守卫 verify_phase3 5.5），
        # 而一个没接上的循环不会报错，只会让每天四点什么都不发生。
        conn.execute("UPDATE task_runs SET next_due_at = ? WHERE name = ?",
                     ((now - timedelta(minutes=1)).isoformat(timespec="seconds"), PROBE))
        conn.commit()
        fired_by_loop = len(fired)
        waited = 0.0

        # **别的任务必须先让开**，2026-09-15 加。循环是串行的
        # （`for name in due_tasks(): await to_thread(run_now, name)`），
        # 而夜间备稿只要到期就排在探针前面——一篇文章几十秒，加上重试，
        # 70 秒的窗口根本轮不到探针。于是这一条会在「循环其实好好的」时候报红，
        # 指着一个没坏的东西（坑 §1.2 那个形状）。
        # 探针自己留着，别的一律先关掉，`finally` 保证原样还回去——
        # 关掉了没还回去的话，夜间备稿就从此不再跑，而且是静默的。
        others = [name for name in tasks.registered() if name != PROBE]
        was_enabled = {name: bool(runtime_config.get(tasks.enabled_key(name)))
                       for name in others}
        for name in others:
            runtime_config.set(tasks.enabled_key(name), False)
        try:
            with TestClient(app):                  # 进 lifespan，循环真的起来
                limit = tasks.TICK_SECONDS * 2 + 10  # 有出口的等待（坑 §7.2）
                while waited < limit and len(fired) == fired_by_loop:
                    time.sleep(1.0)
                    waited += 1.0
        finally:
            for name, enabled in was_enabled.items():
                runtime_config.set(tasks.enabled_key(name), enabled)
        check("A2.4", "调度循环自己把到点的任务跑起来了",
              len(fired) > fired_by_loop,
              f"服务起来 {waited:.0f} 秒后自动触发——验的是循环接没接进 lifespan，"
              f"不是「到点了算不算该跑」（那是 A2.1）"
              if len(fired) > fired_by_loop else
              f"等了 {waited:.0f} 秒没动静，循环可能没接上")

        # --- A. 框架：模型开关 --------------------------------------------- #
        print("\nA. 框架能力 · 模型开关")

        kinds = sorted(jobs._workers)  # noqa: SLF001 - the registry is the subject
        missing = [k for k in kinds
                   if runtime_config.get(jobs.provider_key(k)) is None
                   or runtime_config.get(jobs.thinking_key(k)) is None]
        # 2026-09-15 改过。原文还断言 `len(kinds) == 9`，而 09-13 为 P7 的翻译
        # 加了 dsflash2 之后是 10 个——于是它一边报失败一边说「缺开关的：无」，
        # 不满足的只是那个数字。**worker 的个数每个 Phase 都会变，那条规则不会**：
        # 要守的是「每个用模型的地方都碰得到自己的设置」，不是有几个地方。
        check("A4.1", "每个用模型的地方都有自己的提供商与思考开关",
              bool(kinds) and not missing,
              f"{len(kinds)} 个 worker，缺开关的：{missing or '无'}")

        check("A4.2", "旧的提供商设置迁移过来了，没丢",
              str(runtime_config.get(jobs.provider_key("generate_article"))) ==
              str(runtime_config.get("gen_provider")),
              f"写文章仍然是 {runtime_config.get('gen_provider')!r}")

        body_off = client._openai_request(  # noqa: SLF001 - asserting the wire format
            next(iter(_a_provider())), "k", [{"role": "user", "content": "x"}],
            max_tokens=10, temperature=0.0, json_mode=False, thinking=False)[2]
        body_none = client._openai_request(  # noqa: SLF001
            next(iter(_a_provider())), "k", [{"role": "user", "content": "x"}],
            max_tokens=10, temperature=0.0, json_mode=False)[2]
        check("A5.1", "思考开关真的进请求体，不设就根本不发这个字段",
              body_off.get("enable_thinking") is False
              and "enable_thinking" not in body_none,
              "关掉思考在写文章那处实测 5/10 → 8/10、61.6 秒 → 9.1 秒")

        # 2026-09-16 放宽。原文断言「只有 `generate_article` 可以非默认，
        # 其余保持 default」——理由是只有写文章那处有实测依据（决定 19）。
        # 而用户当天定了**十个 worker 统一走 `deepseek-flash` 并一律关思考**
        # （见 CLAUDE.md「模型与中转站」），于是十处全非默认，这条天天报红，
        # 而红的时候什么也没坏：那是一个人做的决定，不是代码跑偏。
        #
        # 守始终成立的那一半：**有实测依据的那一处必须是关的**。
        # 别的几处是不是默认，交给 detail 如实说出来——它是情报，不是失败。
        writing = str(runtime_config.get(jobs.thinking_key("generate_article")))
        others = {k: str(runtime_config.get(jobs.thinking_key(k)))
                  for k in kinds if k != "generate_article"}
        non_default = sorted(k for k, v in others.items() if v != "default")
        check("A5.2", "写文章那处的思考是关的（唯一有实测依据的一处）",
              writing == "off",
              f"写文章={writing}；另外 {len(others)} 处里 {len(non_default)} 处非默认"
              + (f"（{'、'.join(non_default[:4])}…）" if non_default else "")
              + "——非默认不算错，但它们没有实测依据，掉质量先查这里")

        # --- B. 每日供给 --------------------------------------------------- #
        print("\nB. 每日供给")

        fails, _ = pipeline.verdict(Report())
        check("B1.1", "阴性对照：合格的稿子不被拦", not fails, "一篇各项都好的稿子放行")

        seeded = [
            ("超纲 2.32%", Report(beyond_rate=2.32)),
            ("命中 18/25", Report(target_hits={f"w{i}": i < 18 for i in range(25)})),
            ("末段没有目标词", Report(targets_per_paragraph=[10, 9, 5, 4, 0])),
        ]
        caught = [name for name, rep in seeded if pipeline.verdict(rep)[0]]
        check("B1.2", "阳性对照：三种已知的坏稿都被拦下",
              len(caught) == 3, "、".join(caught))

        long_fails, long_notes = pipeline.verdict(Report(words=521, avg_sentence=11.1))
        check("B1.3", "篇长只记录不拦；句法根本不作判据",
              not long_fails and len(long_notes) == 1,
              "首要目的是背单词，像不像真题不构成合格与否（2026-09-11 定）")

        in_progress = daily.words_in_progress()
        check("B2.1", "读得到「已经在学的词」这份排除清单",
              isinstance(in_progress, set),
              f"{len(in_progress)} 个词不会再被当成新词教（决定 6）")

        rows = conn.execute(
            "SELECT target_words FROM generation_drafts"
            " WHERE note LIKE 'daily:%' ORDER BY id DESC LIMIT 3"
        ).fetchall()
        if rows:
            collided = set()
            for row in rows:
                try:
                    words = {w.lower() for w in json.loads(row["target_words"])}
                except (TypeError, ValueError):
                    continue
                collided |= words & in_progress
            check("B2.2", "备好的稿子没有拿在学的词当新词教",
                  not collided, f"撞上的：{sorted(collided) or '无'}")
        else:
            note("B2.2", "目标词与在学词零交集", "还没有备稿任务跑过，没有样本可查")

        check("B3.1", "熔断是个数，而且比期望次数高得多",
              int(runtime_config.get("gen_daily_attempt_cap")) >= 10,
              f"上限 {runtime_config.get('gen_daily_attempt_cap')} 次，"
              f"凑够 {runtime_config.get('gen_daily_count')} 篇期望 4–5 次——"
              f"报警一旦误报就没人看了")

        check("B4.1", "备稿任务登记在案，默认凌晨跑",
              "generation.daily" in tasks.registered()
              and str(runtime_config.get("task_generation_daily_schedule")) == "04:00",
              "第二层保险：固定时间兜底")

        from backend.core import events as _events
        subscribed = _events.subscriber_summary().get("article.finished", [])
        check("B4.2", "读完一篇会触发补库存",
              any("generation" in str(s) for s in subscribed),
              f"订阅者 {subscribed}——第一层保险靠订阅事件接入，不改阅读那条流程（铁律 6）")

        stuck = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_articles"
            " WHERE source = 'generated' AND status != 'ready'"
        ).fetchone()["n"]
        check("B5.1", "没有备好却打不开的文章", stuck == 0,
              f"{stuck} 篇停在半路——标注没跑完的文章在货架上看着正常，点开是空的")

        unjudged = conn.execute(
            "SELECT COUNT(*) AS n FROM reading_phrases WHERE verdict IS NULL"
        ).fetchone()["n"]
        check("B5.2", "备好的文章连词组也判过了", unjudged == 0,
              f"{unjudged} 处待判——没判过的词组不会显示，而且不会报错")

        # --- C. 今日包与离线闭环 ------------------------------------------- #
        print("\nC. 今日包与离线闭环")

        token = conn.execute(
            "SELECT value FROM settings WHERE key = 'reading_web_token'"
        ).fetchone()
        headers = {"Authorization": f"Bearer {token['value']}"} if token else {}

        with TestClient(app) as http:
            check("C0.1", "客户端接口要令牌",
                  http.get("/v1/client/today").status_code in (401, 403),
                  "两套 API 分开（铁律 4）")

            package = http.get("/v1/client/today", headers=headers)
            check("C1.1", "今日包拿得到", package.status_code == 200,
                  f"HTTP {package.status_code}")
            data = package.json() if package.status_code == 200 else {}

            # 条件请求（2026-09-16 加）。今日包实测约 1 MB，而客户端每开一次
            # 复习都要它——内容没变就该回 304 和零字节。
            # **两头都验**：只验「带 ETag 回 304」的话，一个永远回 304 的服务端
            # 也能通过，而那意味着改动永远传不到手机上。
            etag = package.headers.get("etag")
            again = http.get("/v1/client/today",
                             headers={**headers, "If-None-Match": etag or "x"})
            stale = http.get("/v1/client/today",
                             headers={**headers, "If-None-Match": '"stale"'})
            check("C1.6", "今日包没变时只回 304，一个字节都不传",
                  bool(etag) and again.status_code == 304 and len(again.content) == 0,
                  f"{len(package.content)} 字节 → {len(again.content)} 字节"
                  if etag else "响应里没有 ETag")
            check("C1.7", "版本对不上时照样给全量",
                  stale.status_code == 200 and len(stale.content) > 1000,
                  f"陈旧的 ETag 换回 {len(stale.content)} 字节——"
                  f"永远回 304 的服务端会让改动永远到不了手机上")

            wanted_fields = {"learner", "capabilities", "day", "articles",
                             "extra_articles", "reviews", "settings"}
            check("C1.2", "契约字段齐全", wanted_fields <= set(data),
                  f"缺：{sorted(wanted_fields - set(data)) or '无'}")

            articles = data.get("articles") or []
            inlined = all(len(a.get("tokens") or []) > 50 and a.get("glossary")
                          for a in articles)
            check("C1.3", "正文与释义随包内联，不是给个清单",
                  bool(articles) and inlined,
                  f"{len(articles)} 篇，首篇 {len(articles[0].get('tokens', [])) if articles else 0} 个 token、"
                  f"{len(articles[0].get('glossary', {})) if articles else 0} 条释义"
                  if articles else "今日包里没有文章")

            ids = [a["article"]["id"] for a in articles if a.get("article")]
            sources = {
                conn.execute("SELECT source FROM reading_articles WHERE id = ?",
                             (i,)).fetchone()["source"] for i in ids
            }
            check("C1.4", "今日包不装真题，且响应里说破了这件事",
                  sources <= {"generated"} and data.get("excludes_exam_papers") is True,
                  f"来源 {sources or '空'}；真题走 /library?source=（决定 9）")

            check("C1.5", "capabilities 如实声明还没实现的东西",
                  data.get("capabilities", {}).get("level_estimate") is False,
                  "水平估计没做，契约里是 false 而不是装作有")

            decision = conn.execute(
                "SELECT kind, chosen FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            check("C2.1", "选了哪几篇、为什么，写进了决策日志",
                  decision is not None and decision["kind"] == "today.assembled",
                  "「今天为什么是这三篇」将来答得上来")

            # **自己种探针，不靠「今天碰巧有未做完的复习」**（坑 §4.1）。
            #
            # 这两项原先要先 `GET /v1/client/reviews` 拿一条真的队列行，于是
            # 当天复习做完之后它们就报「跑不了这一项」——2026-09-12 记过一次
            # （坑 §4.4c）。P9 把那个端点删了，正好把这条欠账一起还掉:
            # **作答现在按身份上报**（item_type/item_key/sense_id，queue_id 恒 0），
            # 而身份是凭空造得出来的，不需要服务端先有一行。
            key = f"p4probe-{uuid.uuid4().hex}"
            batch = {"answers": [{
                "idem_key": key, "queue_id": 0,
                "item_type": "word", "item_key": "__p4probe__", "sense_id": 0,
                "passed": True, "revealed": 0,
                "occurred_at": now.isoformat(timespec="seconds"),
            }]}
            first = http.post("/v1/client/reviews/answers",
                              headers=headers, json=batch).json()
            again = http.post("/v1/client/reviews/answers",
                              headers=headers, json=batch).json()
            check("C3.1", "离线补报的作答收得下",
                  first.get("accepted") == 1, f"接受 {first.get('accepted')} 条")
            check("C4.1", "同一条补两次不重复计分",
                  again.get("duplicates") == 1 and again.get("accepted") == 0,
                  "幂等键的唯一索引把住的，不是先查后插")
            stored = conn.execute(
                "SELECT COUNT(*) AS n FROM client_events WHERE idem_key = ?",
                (key,)).fetchone()["n"]
            check("C4.2", "事件只存了一行", stored == 1,
                  "复习作答进的是阅读那张 client_events，一套事件存储不是两套")

            bad = http.post("/v1/client/reviews/answers", headers=headers, json={
                "answers": [{"idem_key": f"p4probe-{uuid.uuid4().hex}",
                             "queue_id": 999999, "passed": True}]}).json()
            check("C3.2", "一条坏的不拖垮整批",
                  bad.get("failed") == 1 and bad.get("accepted") == 0,
                  "没有身份的那一条报 failed 并说明理由，其余照常——"
                  "「没人认得这条」必须说出来，不能静默丢掉（坑 §6.6）")

            # **这一条 2026-09-18 改过，原文守的是「单条那个原样还在」。**
            #
            # P9 §11 有意删掉了 `POST /v1/client/reviews/answer`:它是这条路上
            # 最后一处「服务端跟着用户的拇指改学习状态」，而这个 Phase 的整件事
            # 就是把那件事搬到设备上。**铁律 5 的放宽只在状态/同步那半边、只此一次**，
            # 它唯一的调用方（Web 复习页）在同一个 Phase 一起删了。
            #
            # 所以这里改成守始终成立的那一半（坑 §8 那条规矩）:批量那个端点在，
            # 而且**它认得没有队列号的身份作答**——那才是新的那一半，删掉这条守卫
            # 会让「补报还能不能用」没人管。
            contract = set(app.openapi().get("paths", {}))
            answer_item = app.openapi()["components"]["schemas"]["AnswerItem"]["properties"]
            check("C5.1", "批量端点在，且认得不带队列号的身份作答",
                  {"/v1/client/reviews/answers", "/v1/client/today"} <= contract
                  and {"item_type", "item_key", "sense_id"} <= set(answer_item)
                  and answer_item["queue_id"].get("default") == 0,
                  "单条那个在 P9 §11 有意删了（铁律 5 只在状态/同步这半边放宽一次）；"
                  "身份是可重放的，队列行号不是")

        # --- D. 不变量回归 ------------------------------------------------- #
        print("\nD. 不变量")

        unread_learned = conn.execute("""
            SELECT COUNT(*) AS n FROM study_states s
            WHERE s.pool != 'new' AND NOT EXISTS (
                SELECT 1 FROM study_marks m
                WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type
                  AND m.item_key = s.item_key AND m.sense_id = s.sense_id)
        """).fetchone()["n"]
        check("D1.1", "备稿与今日包都没有替你把词记成学过",
              unread_learned == 0,
              "进复习队列只有一条路：你自己标记。备三篇文章不是替你学了三篇")

        # 今日包必须是只读的。它挑文章、拼释义、附上复习——都是读。
        # 组装一份包就动了学习状态的话，光是打开 App 看一眼就会改变调度，
        # 而这件事不会报错，只会让间隔慢慢失真。
        def fingerprint() -> tuple:
            return (
                conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),'')"
                             " FROM study_states").fetchone()[:2],
                conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),'')"
                             " FROM reading_progress").fetchone()[:2],
                conn.execute("SELECT COUNT(*) FROM study_marks").fetchone()[0],
            )

        before = fingerprint()
        with TestClient(app) as probe_http:
            probe_http.get("/v1/client/today", headers=headers)
        check("D1.2", "组装今日包不改任何学习状态",
              fingerprint() == before,
              "挑文章、拼释义、附复习全是读——打开 App 看一眼不该动你的调度")


        # --- 人工 ---------------------------------------------------------- #
        print("\n需要人工确认")
        # **2026-09-18 改写（P9 §11/§12）。** 原文说「打开 /admin/review」，
        # 而那个页面在 P9 删了——复习整条链现在只在手机上。断网这件事本身
        # 也换了形状:队列不再从服务端拿，设备重放自己的日志就把它算出来了，
        # 所以「断网还能不能答」已经不是那个问题，「重连之后两边对不对得上」
        # 才是。机器验得了前半（C3/C4 自己种探针补报），验不了后半。
        note("M1", "断网走完一天，再连回来看两边对不对得上",
             "补报这半边已经自动验过（收得下、补两次不重复计分、"
             "坏的那条报 failed 不拖垮整批）。剩下真需要人的是手机上那一遍："
             "开飞行模式，把当天复习答完，关掉再打开 App 看进度还在不在，"
             "然后连上网——看事件是不是自己补了上去，且没有多算一遍")
        note("M2", "早上打开就有三篇",
             "调度循环会不会自己触发已由 A2.4 自动验过。"
             "这一条验的是剩下那半——四点跑完之后，文章是不是真的能读、"
             "读着是不是那么回事。这个只有你自己打开才知道")

        cleanup(conn)
        print("\n探针已清除")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(manual)} 项待人工")
    if failed:
        print("失败：" + "、".join(failed))
    return 1 if failed else 0


def _a_provider():
    """Any provider object — the wire format does not depend on which."""
    from backend.modules.llm.providers import Provider
    yield Provider(id="probe", label="probe", kind="openai",
                   base_url="http://example.invalid/v1", model="m",
                   price_in=0.0, price_out=0.0, currency="CNY",
                   enabled=True, is_default=False)


if __name__ == "__main__":
    raise SystemExit(main())
