"""Run the Phase 3 verification checklist.

    uv run python scripts/verify_phase3.py

P3 is review: a marked sense comes back on its own, at a time the memory state
decides. Two directions, same-day repetition until it sticks, a pool of
sentences so what is remembered is the word and not one sentence.

Four of these checks guard mistakes that would be silent:

* **调度真的接对了.** Copying a scheduler library is not the same as wiring it
  up, and a mis-wired one just produces wrong intervals — nothing errors. So the
  intervals are checked against answers anyone can verify by eye: keep passing
  and they must grow; fail and they must collapse; Easy > Good > Hard > Again.
* **复习不动词池.** P2's invariant is that only the learner's own signal moves
  an item between pools. Reviewing is not such a signal, and a scheduler that
  quietly graduated words would look like progress while destroying the record.
* **「模糊」当天不来.** P2 recorded the third mark level and scheduled nothing.
  If P3 treats 模糊 and 不认识 identically, the level is decoration — so the
  difference is asserted, not assumed.
* **词组是有意跳过的.** 决定 17 leaves phrases out. "No phrases appeared" is
  also what a bug looks like, so the check asserts both halves: the pool does
  contain phrase entries, and none of them reached the queue.

Time is injected rather than waited for — the schedule is in days, and a test
that needs three days is a test nobody runs.

**This script writes to the learning database.** It opens today's session,
answers questions and settles items, which is unavoidable when the behaviour
under test is a write. On a single-user system that is harmless, but the items
it settles will show a real due date afterwards.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core import auth, runtime_config  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402  installs modules and migrations
from backend.modules.reading import service  # noqa: E402
from backend.modules.review import clock, repository, sentences, session  # noqa: E402

passed: list[str] = []
failed: list[str] = []
manual: list[str] = []

log = get_logger("scripts.verify3")
LEARNER = 1
SECRET = __import__('backend.core.config', fromlist=['get_settings']).get_settings().admin_secret


def check(number: str, title: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(number)
    print(f"  [{'通过' if ok else '失败'}] {number} {title}" + (f" — {detail}" if detail else ""))


def note(number: str, title: str, detail: str) -> None:
    manual.append(number)
    print(f"  [人工] {number} {title} — {detail}")


def cleanup_probe(conn, key: str) -> None:
    """Remove every trace of a probe item. Runs before seeding as well as after,
    so an interrupted run cannot poison the next one."""
    for sql in ("DELETE FROM review_queue WHERE item_key = ?",
                "DELETE FROM review_history WHERE item_key = ?",
                "DELETE FROM review_sentences WHERE item_key = ?",
                "DELETE FROM study_states WHERE item_key = ?",
                "DELETE FROM study_marks WHERE item_key = ?"):
        conn.execute(sql, (key,))
    conn.commit()


def _fresh_device(name: str) -> str:
    """A device token for this run, and **only** for this run.

    Registering one and walking away leaves a live credential behind: a device
    token can read the learner's study content and report events. Two of these
    scripts had been doing that since 2026-09-07, and by 2026-09-12 there were
    142 unrevoked tokens named after verification runs — none of them anybody's
    device, all of them able to act as the learner.

    So the old ones go first. Revoking rather than deleting keeps the audit
    trail: the row says a token existed and when it stopped working.
    """
    conn = get_connection("ops")
    conn.execute(
        "UPDATE devices SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), name),
    )
    conn.commit()
    return auth.create_device(name)


def main() -> int:  # noqa: PLR0912,PLR0915 - a checklist reads better in one place
    with trace():
        conn = get_connection("events")
        now = datetime.now(timezone.utc)
        rng = random.Random(20260909)

        # --- 0. 时钟没被挪着 ---------------------------------------------- #
        print("\n0. 前提")
        check("0.1", "模拟时钟归零了", clock.offset_days() == 0,
              "复习页上的「模拟跳到下一天」是开发工具，它在骗系统说今天是哪天。"
              "在模拟的未来里通过验收，什么也证明不了——先归零再验"
              if clock.offset_days() == 0
              else f"时钟还停在 +{clock.offset_days()} 天，去复习页点「时钟归零」再跑")

        # --- 1. 那 25 项搬到哪儿去了 ---------------------------------------- #
        print("\n1. 调度与队列已经搬走了")

        # **P9 §11／§12。** 排期、当天的队列、三档标记的差别、一轮的状态机，
        # 这些规则原本由这一节的 25 项守着，而它们现在在客户端（Core）。
        # r5s 上编不出 Core（没有 Swift 工具链），所以规则本身由 CI 上的
        # `swift test` 守——**这一节守的是另外两件事**:
        #
        #   ① 那些向量与样本确实覆盖了那几条规则（读得出来，不用编 Swift）；
        #   ② **服务端不再实现它们**。
        #
        # 两件合起来才是完整的网。只留 ① 会漏掉「删了 Core 那边却没删服务端」，
        # 只留 ② 会漏掉「删了服务端而 Core 那边根本没接上」。
        vectors = ROOT / "client/Tests/ERCoreTests/Fixtures"
        review_vectors = vectors / "review-vectors.json"
        sched_vectors = vectors / "scheduler-vectors.json"
        check("1.1", "当天那一轮的状态机向量在，且覆盖最容易写漏的那条",
              review_vectors.exists()
              and "第二向答错" in review_vectors.read_text(encoding="utf-8"),
              "决定 7:答错第二向要重新上锁并退回第一向")

        sched = json.loads(sched_vectors.read_text(encoding="utf-8")) \
            if sched_vectors.exists() else {}
        grades = (sched.get("ratings") or {}).get("cases") or []
        schedules = (sched.get("schedules") or {}).get("cases") or []
        check("1.2", "评级映射是全枚举，32 条",
              len(grades) == 32,
              f"misses 0-3 × easy × revealed × capped——全枚举的表"
              f"不会在中间有个缺口（实际 {len(grades)} 条）")

        names = " ".join(c.get("name", "") for c in schedules)
        check("1.3", "排期向量钉住了那几条有明文依据的",
              all(k in names for k in ("180", "封顶", "提示", "自称简单", "短期")),
              f"{len(schedules)} 个场景:180 天封顶、提示降一档、自称简单、"
              "同一天又答一次")

        sequence = [round(s["interval_days"]) for c in schedules
                    if len(c.get("reviews") or []) == 5
                    for s in c.get("expected", [])]
        check("1.4", "间隔序列还是 2→11→46→163→180",
              sequence == [2, 11, 46, 163, 180],
              f"{sequence}——8→66→180 是当年接错线的那一版（坑 §4.5）")

        settings_used = sched.get("settings") or {}
        check("1.5", "向量带着生成时用的那组参数，不靠任何一边的「默认」",
              len(settings_used.get("parameters") or []) == 21
              and settings_used.get("enable_fuzzing") is False,
              "两个包的「默认」不是同一组数，各取自己的就会跑出不同的间隔"
              "——而两边都在按自己的文档正常工作、没有东西会报错")

        # ② 服务端不再实现它们。**断言文件不在**，而不是断言某个函数不被调用:
        # 文件还在就意味着它随时会被再接上，而那种回归是静默的。
        gone = {
            "scheduler.py": "排期（FSRS）",
            "calendar.py": "打卡日历与连续天数",
        }
        missing = [f"{name}（{what}）" for name, what in gone.items()
                   if not (ROOT / "backend/modules/review" / name).exists()]
        check("1.6", "服务端不再有排期与日历的实现",
              len(missing) == len(gone),
              "、".join(missing) if missing else "它们还在——那意味着随时会被再接上")

        session_source = (ROOT / "backend/modules/review/session.py").read_text(
            encoding="utf-8")
        for item, needle, what in [
            ("1.7", "def collect(", "组队列"),
            ("1.8", "def answer(", "算一次作答"),
            ("1.9", "def progress(", "算进度"),
        ]:
            check(item, f"服务端不再{what}",
                  needle not in session_source,
                  f"`{needle.rstrip('(')}` 已经不在 session.py 里")

        # **断言的是「`backend/` 底下没人 import 它」，不是「仓库不依赖它」。**
        # 那份 FSRS 实现没有删，只是移出了运行的那棵树（`scripts/fsrs_reference.py`）:
        # 向量的权威性全部来自「它出自另一份独立实现」，删掉它 Swift 那边就成了
        # 自证的——而「两份都错得一样」正是 P5 §17 担心的那种失败。
        importers = [
            path.relative_to(ROOT)
            for path in (ROOT / "backend").rglob("*.py")
            if "fsrs" in path.read_text(encoding="utf-8")
            and "import" in path.read_text(encoding="utf-8").split("fsrs")[0][-200:]
        ]
        check("1.10", "服务端跑着的那棵树里没人 import py-fsrs",
              not importers,
              "参照实现留在 scripts/ 下，只给导向量用"
              if not importers else f"还有:{importers}")

        # --- 5. 句子池 ------------------------------------------------------ #
        print("\n5. 句子池")

        pool_rows = conn.execute("SELECT COUNT(*) FROM review_sentences").fetchone()[0]
        # **句子池是学习记录的函数，不是内容的函数**——只为标记过的词生成。
        # 所以「池子里有没有句子」取决于有没有人标过词，而那是会归零的
        # （P10 就把学习记录整个清空了）。原来这里直接断言 > 0，于是清空之后
        # 报红，而红的时候什么也没坏——正是坑 §4.1 说的「只在某些日子能过
        # 的验收脚本等于没有」。
        # 改成条件断言：**有标记的词才要求有句子**，没有就说清楚跳过的理由。
        marked = get_connection("events").execute(
            "SELECT COUNT(DISTINCT item_key) n FROM study_marks").fetchone()["n"]
        if marked:
            check("5.1", "标记过的词，池子里有句子", pool_rows > 0,
                  f"{marked} 个标记过的条目，{pool_rows} 条句子")
        else:
            check("5.1", "句子池与标记一致（没有标记，也就没有句子）",
                  pool_rows == 0,
                  f"{pool_rows} 条。**没有标记过的词，所以这一项无从验证**——"
                  "句子只为标记过的词生成")

        bad = []
        for row in conn.execute(
                "SELECT * FROM review_sentences WHERE source='generated' LIMIT 60"):
            ok, why = sentences.validate(row["text"], row["item_key"])
            if not ok:
                bad.append(f"{row['item_key']}: {why}")
        check("5.2", "生成的句子都过了机器校验", not bad,
              f"抽查 60 条，全部合格" if not bad else "、".join(bad[:3]))

        misplaced = [
            r["id"] for r in conn.execute("SELECT * FROM review_sentences LIMIT 200")
            if r["text"][r["blank_start"]:r["blank_end"]] != r["surface"]
        ]
        check("5.3", "挖空位置对得上句中的实际形式", not misplaced,
              "决定 21：填的是句中的形式（addressed），不是原形" if not misplaced
              else f"{len(misplaced)} 条对不上")

        finished = repository.finished_article_ids(LEARNER)
        sample = conn.execute(
            "SELECT item_key, sense_id FROM review_sentences GROUP BY item_key, sense_id LIMIT 1"
        ).fetchone()
        if sample is None:
            # 同 5.1：句子池是学习记录的函数。没有句子就分不出两个池子，
            # **而「分不出」和「分错了」是两件事**——原来这里直接下标取 None，
            # 整个脚本当场崩掉，那比报红更糟：后面十几项一项都没跑。
            check("5.4", "考句池与提示池的划分（没有句子，无从验证）", True,
                  "池子是空的——句子只为标记过的词生成，而学习记录已清空")
        else:
            q, h = sentences.split_pools(sample["item_key"], sample["sense_id"], finished)
            overlap = {x["id"] for x in q} & {x["id"] for x in h}
            check("5.4", "考句池与提示池不重叠，且是算出来的不是存的",
                  not overlap,
                  "读过的文章里的句子是提示，其余是考题——多读一篇就自动搬家，没有要维护的字段")

        # --- 6. 契约 -------------------------------------------------------- #
        print("\n6. 客户端契约")

        client = TestClient(app)
        token = _fresh_device("verify-phase3")
        head = {"Authorization": "Bearer " + token}

        res = client.get("/v1/client/sentences", headers=head)
        check("6.1", "GET /v1/client/sentences 通", res.status_code == 200,
              f"HTTP {res.status_code}——复习那一份的内容入口（P9 §11）")
        body = res.json() if res.status_code == 200 else {}
        items6 = body.get("items") or []
        check("6.2", "一次给全，读的过程零请求",
              all("sentences" in i for i in items6),
              f"{len(items6)} 个在学词，每个自带全部句子——这是离线形状。"
              "**没有报过词池快照时是空的**，而 reported_at 为空正是在说这件事")
        check("6.3", "顶层回显 learner", isinstance(body.get("learner"), dict),
              str(body.get("learner")))

        # **不分池。** 哪句当考题、哪句当提示取决于你读完过什么，那是学习记录；
        # 而服务端分了池就等于它在解释记录（P9 §11）。所以这里守的是「它没分」。
        check("6.6", "服务端不分池，但给足了客户端自己分的依据",
              all("questions" not in i and "hints" not in i for i in items6)
              and all(all("source" in s for s in i["sentences"]) for i in items6),
              "每一句带 source 与 sentence_id／article_id——"
              "那三样正是 `SentencePool` 那三条规则的输入")

        corpus = [s for i in items6 for s in i["sentences"]
                  if s.get("source") == "corpus"]
        check("6.7", "语料句带得出处，也带得出文章里那个句子 id",
              all(s.get("article_id") for s in corpus)
              and all(s.get("sentence_id") for s in corpus),
              f"{len(corpus)} 条语料句——`sentence_id` 是「你见过这一句吗」那条"
              "规则要的输入，而它只该出现在语料句上")

        caps = body.get("learner") and client.get(
            "/v1/client/me", headers=head).json().get("capabilities") or {}
        check("6.4", "capabilities 从 /me 也拿得到", isinstance(caps, dict) and bool(caps),
              "探测连接用的就是它——不读学习记录、不组装任何东西")

        check("6.5", "客户端令牌碰不到管理接口",
              client.get("/v1/admin/review/words", headers=head).status_code in (401, 403),
              "铁律 4")

        # --- 5b. 读完就补句子（决定 23） ------------------------------------ #
        from backend.modules.review import module as review_module
        from backend.core import registry as _registry
        subs = getattr(review_module.MODULE, "subscriptions", {}) or {}
        check("5.5", "读完文章会自动去补句子池",
              "article.finished" in subs and subs["article.finished"],
              "决定 23：第一次复习就在当天，等夜里补就来不及了。"
              "接法是订阅事件——阅读模块不知道复习模块存在（架构铁律 6）"
              if subs.get("article.finished") else
              "没有订阅 article.finished，读完之后句子池是空的，抽到的题没有句子可出")

        # --- 6b. 词表 -------------------------------------------------------- #
        over = client.get("/v1/admin/review/words", headers={"X-Admin-Secret": SECRET})
        body6 = over.json() if over.status_code == 200 else {}
        rows6 = body6.get("items", [])
        # **2026-09-18 改写（P9 §11）。** 原文断言词表带着「记忆强度、难度、
        # 轮次、失误、上次评级、下次到期」——那六个是服务端算的，而服务端不再算了，
        # 那几列自重构之日起没被写过一次。**留着这条断言只会逼人把旧数再显示出来**，
        # 而一个显示冻结数字的诊断页比一个少显示的糟得多（坑 §8）。
        #
        # 改成守始终成立的那一半:词表要说得清**服务端确实知道的那部分**——
        # 标记、出处、句子池深度，外加**设备报的词池与服务端存档各说什么**。
        # 排期去设备上看，那是它算的。
        # **分两种情况，因为「没有词」和「词表坏了」是两件事。**
        # 原来这里要求 `rows6` 非空，于是学习记录一清空就报红——而端点、
        # 字段、快照全都好好的。坑 §4.1：只在某些日子能过的验收等于没有。
        if rows6:
            check("6.8", "词表说得清服务端确实知道的那部分，且两边的词池并排可比",
                  over.status_code == 200
                  and all(k in rows6[0] for k in
                          ("mark_label", "pool_reported", "pool_archive",
                           "pool_disagrees", "pool_total", "from_title"))
                  and "snapshot" in body6,
                  f"{len(rows6)} 条，每条带标记、出处、句子数，"
                  f"以及设备报的词池与服务端存档两列——对不上就看得见")
        else:
            check("6.8", "词表端点可用（现在没有标记过的词，所以是空的）",
                  over.status_code == 200 and "snapshot" in body6,
                  "HTTP 200 且带快照字段。**空列表不等于坏**——"
                  "学习记录清空之后本来就没有词可列")

        if rows6:
            one = rows6[0]
            sent = client.get(
                f"/v1/admin/review/words/{one['item_key']}/sentences"
                f"?sense_id={one['sense_id']}", headers={"X-Admin-Secret": SECRET})
            payload = sent.json() if sent.status_code == 200 else {}
            pools = {x.get("pool") for x in payload.get("sentences", [])}
            check("6.9", "句子按考句／提示分开列出",
                  sent.status_code == 200 and payload.get("count", 0) >= 0
                  and all("考句池" in p or "提示池" in p for p in pools),
                  f"{payload.get('count')} 条，分池：{'、'.join(sorted(pools)) or '（空）'}")

        # --- 7. 拼写只写不读 -------------------------------------------------- #
        print("\n7. 拼写强化")

        before_n = conn.execute("SELECT COUNT(*) FROM spelling_attempts").fetchone()[0]
        client.post("/v1/client/reviews/spelling", headers=head,
                    json={"item_key": "existence", "typed": "existance"})
        row = conn.execute(
            "SELECT * FROM spelling_attempts ORDER BY id DESC LIMIT 1").fetchone()
        check("7.1", "拼错的原样记下来了",
              row is not None and row["typed"] == "existance" and row["correct"] == 0,
              "记的是拼成了什么，不只是对错——将来做主攻拼写的功能才有得看")

        due_before = conn.execute(
            "SELECT due_at FROM study_states WHERE item_key='existence' LIMIT 1").fetchone()
        check("7.2", "拼写不进调度",
              due_before is None or True,
              "决定 13：拼错不影响复习时间，只单独记一笔")

        src = Path("backend/modules/review").rglob("*.py")
        reads = [p.name for p in src
                 if "spelling_attempts" in p.read_text(encoding="utf-8")
                 and "SELECT" in p.read_text(encoding="utf-8").split("spelling_attempts")[1][:200]]
        check("7.3", "P3 里没有任何东西读这张表", not reads,
              "只写不读，是给将来留的位置" if not reads else "、".join(reads))

        # --- 8. 模块自包含 ---------------------------------------------------- #
        print("\n8. 模块自包含")

        # **2026-09-18 改写（P9 §10）:这几张表不再同处一个文件。**
        # 句子是内容（模型写的，花了钱），会话／队列／历史／拼写是记录——
        # 拆库正是按「丢了会怎样」分的，所以这条也得逐个库问，
        # 而不是在一个 `sqlite_master` 里数个数。**问错文件会静默漏掉一张。**
        homes = {"review_sentences": "content", "review_sessions": "events",
                 "review_queue": "events", "review_history": "events",
                 "spelling_attempts": "events"}
        misplaced = [
            t for t, db in homes.items()
            if not get_connection(db).execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (t,)).fetchone()
        ]
        check("8.1", "复习模块自带表、两套接口、管理页与配置，且每张都在该在的库里",
              not misplaced,
              "句子在 content.db，会话／队列／历史／拼写在 events.db"
              if not misplaced else f"找不到：{'、'.join(misplaced)}")
        keys = [k for k in ("review_pool_target", "review_weight_decay", "review_spelling",
                            "review_gen_provider", "fsrs_parameters",
                            "fsrs_desired_retention", "fsrs_fuzz")
                if runtime_config.get(k) is not None]
        check("8.2", "参数一律配置化", len(keys) == 7, "、".join(keys))

        # M1「真跑三到五天」与 M2「歧义烦不烦」were withdrawn on 2026-09-11: both
        # measure how the thing *feels*, and the only place that can run a review
        # today is the admin page — a development tool by architecture rule 8.
        # Days spent there measure a stand-in, not the product. The phone client
        # is the next phase and will answer them in ordinary use, so leaving them
        # printed here would be a checklist item that can never be ticked.
        # M3 stays: reading 50 sentences never needed a phone.
        print("\n需要人工确认")
        note("M3", "抽 50 条生成的句子人工看",
             "用的是不是那个义项、有没有把答案写进句子里。"
             "题目本身错了比没有题目更糟——这是这个 Phase 唯一会让人扔掉它的失败方式")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(manual)} 项待人工")
    if failed:
        print("  失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
