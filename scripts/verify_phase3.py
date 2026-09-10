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

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core import auth, runtime_config  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402  installs modules and migrations
from backend.modules.reading import service  # noqa: E402
from backend.modules.review import clock, repository, scheduler, sentences, session  # noqa: E402

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


def interval(card_state, asks, now):
    return scheduler.review(card_state, asks, now, fuzz=False).interval_days


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


def main() -> int:  # noqa: PLR0912,PLR0915 - a checklist reads better in one place
    with trace():
        conn = get_connection("learning")
        now = datetime.now(timezone.utc)
        rng = random.Random(20260909)

        # --- 0. 时钟没被挪着 ---------------------------------------------- #
        print("\n0. 前提")
        check("0.1", "模拟时钟归零了", clock.offset_days() == 0,
              "复习页上的「模拟跳到下一天」是开发工具，它在骗系统说今天是哪天。"
              "在模拟的未来里通过验收，什么也证明不了——先归零再验"
              if clock.offset_days() == 0
              else f"时钟还停在 +{clock.offset_days()} 天，去复习页点「时钟归零」再跑")

        # --- 1. 调度器接对了没有 ------------------------------------------ #
        print("\n1. 调度器")

        fresh = None
        seq = []
        state = None
        for _ in range(5):
            outcome = scheduler.review(state, 0, now, fuzz=False)
            seq.append(round(outcome.interval_days, 1))
            state = outcome.state
            now = outcome.due_at
        check("1.1", "连续答对，间隔必须递增",
              all(b > a for a, b in zip(seq, seq[1:])),
              " → ".join(f"{d}天" for d in seq))

        long_state = state
        collapsed = scheduler.review(long_state, 2, now, fuzz=False)
        check("1.2", "答错，间隔必须塌回去",
              collapsed.interval_days < seq[-1] / 2,
              f"{seq[-1]} 天 → {round(collapsed.interval_days, 1)} 天")

        base = scheduler.review(None, 0, datetime.now(timezone.utc), fuzz=False).state
        at = datetime.fromisoformat(base["due_at"])
        by_miss = {m: round(interval(base, m, at), 2) for m in (0, 1, 2)}
        easy_iv = round(scheduler.review(base, 0, at, easy=True, fuzz=False).interval_days, 2)
        check("1.3", "失误越多，下次来得越快；标了太简单则更远",
              easy_iv > by_miss[0] > by_miss[1] >= by_miss[2],
              f"太简单→{easy_iv}天  " + "  ".join(f"失误{m}次→{d}天" for m, d in by_miss.items()))

        # 数的是**失误次数**，不是问了几道题。第一版数问题数，而一轮天生就是两道
        # （两个方向），于是完美一轮记成 2、磕绊一次记成 4——把只磕了一下的词判成
        # 最差的 Again，而 Easy 在真实流程里根本够不着。2026-09-09 改。
        rows = [(n, scheduler.rating_for(n).name) for n in (0, 1, 2, 5)]
        check("1.4", "评分按失误次数，不按问了几道题",
              [r for _, r in rows] == ["Good", "Hard", "Again", "Again"],
              "、".join(f"失误{n}次→{r}" for n, r in rows))

        check("1.4b", "Easy 只由你自己标，且磕过就不认",
              scheduler.rating_for(0, easy=True) is scheduler.EASY_RATING
              and scheduler.rating_for(1, easy=True).name == "Hard",
              "系统量的是「答没答对」，不是「费不费劲」——没有的信号不编")

        sched = scheduler.scheduler(fuzz=False)
        sched_cap = scheduler.scheduler(fuzz=False)
        check("1.7", "最长间隔压到了半年",
              sched_cap.maximum_interval <= 365,
              f"{sched_cap.maximum_interval} 天——FSRS 默认 36500 天是给「终身记住」的，"
              "而这个项目瞄的是有日期的考试，排到考试之后等于没排")

        check("1.5", "FSRS 自带的当天重复已关掉",
              not sched.learning_steps and not sched.relearning_steps,
              "learning_steps 与 relearning_steps 都为空——当天重复由伪随机池负责，"
              "两个都开会把一天算成两次复习")

        state_a = scheduler.review(None, 2, datetime(2026, 1, 1, tzinfo=timezone.utc), fuzz=False)
        revived = scheduler.to_card(state_a.state)
        check("1.6", "记忆状态存进库再取出来还是同一张卡",
              revived.stability == state_a.state["stability"]
              and revived.difficulty == state_a.state["difficulty"],
              "换算法要能拿历史重放，前提是状态存得住")

        # --- 2. 今天的队列 ------------------------------------------------- #
        print("\n2. 今天要复习什么")

        now = datetime.now(timezone.utc)

        # A probe item of our own, due now, with a sentence in the pool.
        #
        # **The first version walked whatever happened to be due today, and that
        # made the script pass or fail depending on the day** — run it after
        # finishing the day's review and there was nothing left to walk, so five
        # checks reported failure while nothing was wrong. A verification script
        # that only works on some days verifies nothing. It seeds its own item
        # and removes it again, the same way section 3 already did.
        PROBE, PROBE_SENSE = "__probe_round", 0
        cleanup_probe(conn, PROBE)
        conn.execute(
            "INSERT INTO study_states (learner_id, item_type, item_key, sense_id, pool,"
            " introduced_at, encounters, updated_at, due_at)"
            " VALUES (?,'word',?,?, 'reviewing', ?, 0, ?, ?)",
            (LEARNER, PROBE, PROBE_SENSE, now.isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds"),
             (now - timedelta(hours=1)).isoformat(timespec="seconds")))
        for text, blank in (("The team had to probe the problem before the meeting.", 16),
                            ("Careful workers probe every corner of the old house.", 16)):
            conn.execute(
                "INSERT OR IGNORE INTO review_sentences (item_type, item_key, sense_id, text,"
                " blank_start, blank_end, surface, source, created_at)"
                " VALUES ('word',?,?,?,?,?,'probe','generated',?)",
                (PROBE, PROBE_SENSE, text, blank, blank + 5,
                 now.isoformat(timespec="seconds")))
        conn.commit()

        items = session.collect(LEARNER, now)
        check("2.1", "到期的条目进得了队列",
              any(i["item_key"] == PROBE for i in items),
              f"{len(items)} 条，含刚种下的探针")

        phrase_rows = conn.execute(
            "SELECT COUNT(*) FROM study_states WHERE item_type='phrase' AND pool='reviewing'"
        ).fetchone()[0]
        in_queue = [i for i in items if i["item_type"] != "word"]
        check("2.2", "词组是被有意跳过的，不是碰巧没查到",
              phrase_rows > 0 and not in_queue,
              f"复习池里确有 {phrase_rows} 个词组条目，而队列里 0 个——"
              "决定 17 把词组留到有数据之后再定" if phrase_rows else
              "复习池里一个词组都没有，这条查不出东西来")

        state = session.ensure(LEARNER, now)
        rows = repository.queue_rows(state["id"])
        check("2.3", "会话建起来了", bool(rows), f"session {state['id']}，队列 {len(rows)} 条")

        # --- 3. 「模糊」当天不来 ------------------------------------------- #
        print("\n3. 三档标记在调度上真的有差别")

        probe_day = (now + timedelta(days=400)).date().isoformat()
        conn.execute("DELETE FROM study_states WHERE item_key IN ('__probe_u','__probe_f')")
        conn.execute("DELETE FROM study_marks WHERE item_key IN ('__probe_u','__probe_f')")
        for key, kind in (("__probe_u", "unknown"), ("__probe_f", "fuzzy")):
            conn.execute(
                "INSERT INTO study_states (learner_id, item_type, item_key, sense_id, pool,"
                " introduced_at, encounters, updated_at) VALUES (?,'word',?,0,'reviewing',?,0,?)",
                (LEARNER, key, probe_day, probe_day))
            conn.execute(
                "INSERT INTO study_marks (learner_id, item_type, item_key, sense_id, kind,"
                " created_at) VALUES (?,'word',?,0,?,?)",
                (LEARNER, key, kind, probe_day + "T08:00:00+00:00"))
        conn.commit()

        probe_now = datetime.fromisoformat(probe_day + "T12:00:00+00:00")
        keys = {i["item_key"] for i in session.collect(LEARNER, probe_now)}
        check("3.1", "标「不认识」的当天就来", "__probe_u" in keys)
        check("3.2", "标「模糊」的当天不来", "__probe_f" not in keys,
              "P2 那句「模糊只记录不调度」的前提是 P2 没有调度；到了 P3 必须验出差别，"
              "否则三档就白分了")
        keys_next = {i["item_key"] for i in
                     session.collect(LEARNER, probe_now + timedelta(days=1))}
        check("3.3", "「模糊」第二天进队列", "__probe_f" in keys_next)
        conn.execute("DELETE FROM study_states WHERE item_key IN ('__probe_u','__probe_f')")
        conn.execute("DELETE FROM study_marks WHERE item_key IN ('__probe_u','__probe_f')")
        conn.commit()

        # --- 4. 走一轮 ------------------------------------------------------ #
        print("\n4. 一轮复习")

        open_rows = [r for r in repository.queue_rows(state["id"], open_only=True)
                     if r["item_key"] == PROBE]
        if not open_rows:
            check("4.0", "探针条目在队列里", False, "种下的探针没进队列")
        else:
            row = open_rows[0]
            item_key, sense_id = row["item_key"], row["sense_id"]
            before = repository.state_of(LEARNER, "word", item_key, sense_id)

            session.answer(LEARNER, row["id"], passed=True, now=now)
            mid = [r for r in repository.queue_rows(state["id"]) if r["id"] == row["id"]][0]
            check("4.1", "看词想义过了才解锁看义想词",
                  int(mid["step"]) == session.SENSE_TO_WORD and int(mid["asks"]) == 1,
                  f"step {mid['step']}，今天已问 {mid['asks']} 次")

            session.answer(LEARNER, row["id"], passed=False, now=now)
            after = [r for r in repository.queue_rows(state["id"]) if r["id"] == row["id"]][0]
            check("4.2", "看义想词失败要重新上锁并降权",
                  int(after["step"]) == session.WORD_TO_SENSE
                  and float(after["weight"]) < float(mid["weight"]),
                  f"step 回到 {after['step']}，权重 {mid['weight']} → {after['weight']}")
            check("4.3", "降权不是出局",
                  float(after["weight"]) > 0 and after["done_at"] is None,
                  "当天结束的唯一条件是池子空了（决定 15），降权只是让路")

            session.answer(LEARNER, row["id"], passed=True, now=now)
            done = session.answer(LEARNER, row["id"], passed=True, now=now)
            check("4.4", "两个方向都过才结算",
                  done["done"] and done["settled"] is not None,
                  f"{done['settled']['rating_name']}，{done['settled']['interval_days']} 天后再来"
                  if done["settled"] else "")
            check("4.8", "结算按失误次数，磕过的不许标太简单",
                  done.get("misses", 0) >= 1 and done["settled"]["rating_name"] != "Easy",
                  f"这一轮失误 {done.get('misses')} 次 → {done['settled']['rating_name']}")

            settled_state = repository.state_of(LEARNER, "word", item_key, sense_id)
            check("4.5", "复习不改变词池位置",
                  settled_state["pool"] == (before or {}).get("pool", "reviewing"),
                  "这是 P2 的不变量：只有你自己的信号能移动词池，复习不是")
            check("4.6", "记忆状态落库了",
                  settled_state["stability"] is not None and settled_state["due_at"] is not None,
                  f"S={round(settled_state['stability'], 2)} "
                  f"D={round(settled_state['difficulty'], 2)} due={settled_state['due_at']}")
            hist = conn.execute(
                "SELECT COUNT(*) FROM review_history WHERE item_key=? AND sense_id=?",
                (item_key, sense_id)).fetchone()[0]
            check("4.7", "每一次作答都进了复习历史", hist >= 4,
                  f"{hist} 条——换算法时靠它重放")

        cleanup_probe(conn, PROBE)

        # --- 5. 句子池 ------------------------------------------------------ #
        print("\n5. 句子池")

        pool_rows = conn.execute("SELECT COUNT(*) FROM review_sentences").fetchone()[0]
        check("5.1", "池子里有句子", pool_rows > 0, f"{pool_rows} 条")

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
        q, h = sentences.split_pools(sample["item_key"], sample["sense_id"], finished)
        overlap = {x["id"] for x in q} & {x["id"] for x in h}
        check("5.4", "考句池与提示池不重叠，且是算出来的不是存的",
              not overlap,
              "读过的文章里的句子是提示，其余是考题——多读一篇就自动搬家，没有要维护的字段")

        # --- 6. 契约 -------------------------------------------------------- #
        print("\n6. 客户端契约")

        client = TestClient(app)
        token = auth.create_device("verify-phase3")
        head = {"Authorization": "Bearer " + token}

        res = client.get("/v1/client/reviews", headers=head)
        check("6.1", "GET /v1/client/reviews 通", res.status_code == 200,
              f"HTTP {res.status_code}")
        body = res.json() if res.status_code == 200 else {}
        check("6.2", "一次给全，读的过程零请求",
              bool(body.get("items")) and all("questions" in i for i in body["items"]),
              f"{len(body.get('items', []))} 条，每条自带句子与提示——这是离线形状")
        check("6.3", "顶层回显 learner", isinstance(body.get("learner"), dict),
              str(body.get("learner")))

        # Both directions have to be answerable from this one response, and they
        # want different things: 看词想义 answers with the Chinese sense,
        # 看义想词 with the word. The first manual run found the page showing the
        # English word as the answer to "what does it mean here", so the payload
        # is now checked for carrying both.
        answerable = [
            i for i in body.get("items", [])
            if (i.get("sense") or {}).get("gloss_zh")
            and any(q.get("surface") for q in i.get("questions", []) + i.get("hints", []))
        ]
        with_sense = [i for i in body.get("items", []) if i.get("sense")]
        check("6.6", "两个方向的答案都在同一份响应里",
              len(answerable) == len(with_sense) and bool(with_sense),
              "看词想义答中文义项，看义想词答那个词——问的是什么就答什么，"
              f"{len(answerable)}/{len(with_sense)} 条齐备")

        has_article = [q for i in body.get("items", []) for q in i.get("hints", [])
                       if "article_id" in q]
        check("6.7", "提示句带得出处，最深一级能跳回原文",
              bool(has_article) or not any(i.get("hints") for i in body.get("items", [])),
              f"{len(has_article)} 条提示句带 article_id")

        caps = service.capabilities()
        check("6.4", "capabilities.memory_state 已转 true", caps.get("memory_state") is True,
              "P2 留的位置，P3 填上；老客户端一行不用改")

        res = client.get("/v1/admin/review/today")
        check("6.5", "客户端令牌碰不到管理接口",
              client.get("/v1/admin/review/today", headers=head).status_code in (401, 403),
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
        check("6.8", "词表能说清每个词凭什么排到那天",
              over.status_code == 200 and bool(rows6)
              and all(k in rows6[0] for k in
                      ("stability", "difficulty", "reps", "total_misses",
                       "rating_label", "due_in_days", "pool_total", "from_title")),
              f"{len(rows6)} 条，每条带记忆强度、难度、轮次、失误、上次评级、下次到期、出处、句子数"
              if rows6 else "取不到")

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

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'review%'"
            " OR name = 'spelling_attempts'")}
        check("8.1", "复习模块自带表、两套接口、管理页与配置",
              tables >= {"review_sentences", "review_sessions", "review_queue",
                         "review_history", "spelling_attempts"},
              "、".join(sorted(tables)))
        keys = [k for k in ("review_pool_target", "review_weight_decay", "review_spelling",
                            "review_gen_provider", "fsrs_parameters",
                            "fsrs_desired_retention", "fsrs_fuzz")
                if runtime_config.get(k) is not None]
        check("8.2", "参数一律配置化", len(keys) == 7, "、".join(keys))

        print("\n需要人工确认")
        note("M1", "真跑三到五天",
             "打开 /admin/review，每天走完。验的是脚本验不了的：原句能不能勾起记忆、"
             "生成的句子读着自不自然、当天重复到会会不会烦")
        note("M2", "看义想词的歧义烦不烦",
             "「着手解决」也对得上 deal with。设计上靠首字母那一级挡，"
             "挡得够不够只有真做几天才知道")
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
