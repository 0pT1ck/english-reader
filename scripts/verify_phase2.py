"""Run the Phase 2 verification checklist.

    uv run python scripts/verify_phase2.py

P2 is the reading loop: articles get analysed once and stored, the first client
contract is written, and every tap, mark and finish comes back and lands in the
right table.

Two of the checks are worth calling out because they guard mistakes that are
silent when they happen:

* **入库不记账.** Only *finishing* an article may write sense states. If ingest
  ever writes them, the words used in articles that were never opened are
  quietly marked as learned and never come up again — no error, no log, and
  months of vocabulary lost before anyone notices.
* **难度分能排对三个等级.** The composite score is checked against the one
  sample in this project with a known answer. A weighting that cannot produce
  四级 < 六级 ≈ 考研 is wrong, and this is the second metric in this project to
  have measured something other than its own name.

What no script can check is whether reading it actually works. Those items are
printed at the end.

**This script writes to the learning database.** It registers a device, reports
events, and finishes one generated article to check that finishing records the
ledger. That is unavoidable — the behaviours being verified are writes — and
harmless on a single-user system, but it means the article it finishes will show
as read afterwards.
"""

from __future__ import annotations

from datetime import datetime, timezone

import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # 与 fixture_events 同目录

from fastapi.testclient import TestClient  # noqa: E402

from backend.core import auth  # noqa: E402
from backend.core.config import get_settings  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402  installs modules and migrations
from backend.modules.reading import difficulty, ingest, repository, service  # noqa: E402

passed: list[str] = []
failed: list[str] = []
manual: list[str] = []

log = get_logger("scripts.verify2")


def check(number: str, title: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(number)
    print(f"  [{'通过' if ok else '失败'}] {number} {title}" + (f" — {detail}" if detail else ""))


def note(number: str, title: str, detail: str) -> None:
    manual.append(number)
    print(f"  [人工] {number} {title} — {detail}")


def _first_ready(source: str | None = None) -> dict | None:
    rows = repository.list_articles(shelf="all", source=source, sort="prepared_at", limit=1000)
    return rows[0] if rows else None


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


def clean_probe_events() -> int:
    """把灌进会合点的夹具事件清掉。规则在 `scripts/fixture_events.py`。

    **前后各清一次**（坑 §4.1）:中断的上一轮不会污染下一轮，
    而这一轮也不会留给下一台设备——那正是 2026-09-18 真机上中的那一次。

    **判断「派生的标记能不能删」不在这里做**，在那个模块里，
    因为这个脚本标的是**真文章里的一个真词**，无条件删会删掉用户自己标的那一笔。
    """
    from fixture_events import purge  # noqa: PLC0415 - 只这一处要

    return purge(get_connection("events"))["removed"]


def main() -> int:  # noqa: PLR0912,PLR0915 - a checklist reads better in one place
    with trace():
        clean_probe_events()
        client = TestClient(app)
        token = _fresh_device("verify-phase2")
        head = {"Authorization": "Bearer " + token}

        # The admin surface takes the admin credential, never the device token —
        # that separation is itself check 4.1. The header form exists for the
        # command line and for the AI reading diagnostics, which is exactly what
        # this script is doing.
        admin_head = {"X-Admin-Secret": get_settings().admin_secret}

        # --- 1. 入库管线 ------------------------------------------------- #
        print("\n1. 入库管线")

        papers = ingest.exam_papers("cet4")
        check("1.1", "真题在磁盘上、入库状态可见",
              len(papers) > 100,
              f"cet4 {len(papers)} 篇，已入库 {sum(1 for p in papers if p['ingested'])} 篇")

        target = next((p for p in papers if p["ingested"]), None)
        if target is None:
            target = papers[0]
            ingest.ingest_exam_paper("cet4", target["ref"])
        article_id = repository.create_article("cet4", target["ref"], "", "")
        row = repository.article_row(article_id)

        check("1.2", "分句分词与词形还原已落库",
              row["sentence_count"] > 5 and len(repository.tokens_of(article_id)) > 100,
              f"{row['sentence_count']} 句 / {len(repository.tokens_of(article_id))} 个 token")

        tokens = repository.tokens_of(article_id)
        kinds = {k: sum(1 for t in tokens if t["kind"] == k)
                 for k in ("content", "function", "proper", "nonword")}
        check("1.3", "每个词都分了类",
              kinds["content"] > 50 and kinds["proper"] >= 0 and kinds["function"] > 20,
              " / ".join(f"{k} {v}" for k, v in kinds.items()))

        sentences = repository.sentences_of(article_id)
        spanning = [s for s in sentences
                    if "\n\n" in s["text"] and s["text"].split("\n\n", 1)[1].strip()]
        check("1.4", "句子不跨段落",
              not spanning,
              "段落边界强制断句（P0 的 senter 只看标点，会把整段焊在一起）"
              if not spanning else f"{len(spanning)} 句跨了段落")

        # The client rebuilds the page by slicing the body between token
        # offsets, so an off-by-one anywhere garbles the whole article — and it
        # would garble it *plausibly*, as shifted punctuation rather than an
        # error. Checked as an invariant over every stored article instead.
        mismatched = 0
        checked = 0
        for candidate in repository.list_articles(shelf="all", limit=1000):
            body = repository.article_row(candidate["id"])["body"]
            rebuilt, cursor = [], 0
            for t in repository.tokens_of(candidate["id"]):
                rebuilt.append(body[cursor:t["char_start"]])
                rebuilt.append(t["surface"])
                if body[t["char_start"]:t["char_end"]] != t["surface"]:
                    mismatched += 1
                cursor = t["char_end"]
            rebuilt.append(body[cursor:])
            if "".join(rebuilt) != body:
                mismatched += 1
            checked += 1
        check("1.5", "按字符偏移能原样重建正文",
              mismatched == 0,
              f"{checked} 篇逐字节还原，无偏移错位"
              if mismatched == 0 else f"{mismatched} 处对不上")

        # --- 2. 语境释义标注 --------------------------------------------- #
        print("\n2. 语境释义标注")

        done, total = repository.annotation_progress(article_id)
        check("2.1", "全篇实词都有语境义项",
              total > 0 and done == total,
              f"{done} / {total}")

        with_sense = [t for t in tokens if (t["sense_id"] or 0) > 0]
        check("2.2", "标注结果指向真实义项",
              len(with_sense) > 30,
              f"{len(with_sense)} 个词标到了具体义项，"
              f"{sum(1 for t in tokens if t['sense_id'] == 0)} 个词没有义项集（退回词条级）")

        bad = get_connection("content").execute(
            "SELECT COUNT(*) FROM reading_tokens t LEFT JOIN senses s ON s.id = t.sense_id"
            " WHERE t.sense_id > 0 AND (s.id IS NULL OR s.headword != t.headword)"
        ).fetchone()[0]
        check("2.3", "没有一条标注指向别的词的义项",
              bad == 0,
              "服务端机械校验：编号必须存在且属于该词条" if bad == 0 else f"{bad} 条越界")

        # --- 3. 难度画像 -------------------------------------------------- #
        print("\n3. 难度画像")

        grouped = repository.scores_by_source()
        have_all = all(len(grouped.get(s, [])) >= 5 for s in ("cet4", "cet6", "kaoyan"))
        if have_all:
            medians = {s: statistics.median(grouped[s]) for s in ("cet4", "cet6", "kaoyan")}
            ordered = medians["cet4"] < medians["cet6"] and medians["cet4"] < medians["kaoyan"]
            check("3.1", "综合难度分能把三个等级排对",
                  ordered,
                  f"四级 {medians['cet4']:.1f} < 六级 {medians['cet6']:.1f}"
                  f" ≈ 考研 {medians['kaoyan']:.1f}")
        else:
            check("3.1", "综合难度分能把三个等级排对", False,
                  "三类真题各需至少 5 篇入库才能验："
                  + " ".join(f"{s} {len(grouped.get(s, []))}" for s in
                             ("cet4", "cet6", "kaoyan")))

        measures = row["difficulty"]
        check("3.2", "词汇类指标齐全",
              all(k in measures for k in
                  ("beyond_cet4_pct", "beyond_cet6_pct", "frequency_p90", "rare_word_pct")),
              ", ".join(f"{k}={measures.get(k)}" for k in
                        ("beyond_cet4_pct", "beyond_cet6_pct", "rare_word_pct")))

        check("3.3", "句法指标存着但不进综合分",
              all(k in measures for k in difficulty.NOT_IN_COMPOSITE)
              and all(difficulty.weights().get(k, 0) == 0 for k in difficulty.NOT_IN_COMPOSITE),
              "实测句长与从句密度区分不了三个等级，进综合分只会稀释信号")

        # The measurement error this project already made twice. `the` carries
        # only zk/gk, so "lacks the cet4 tag" would call it out of syllabus.
        the_beyond = get_connection("content").execute(
            "SELECT COUNT(*) FROM reading_tokens WHERE headword IN ('the','make','people')"
            " AND beyond = 1"
        ).fetchone()[0]
        check("3.4", "考纲标签是累积着判的",
              the_beyond == 0,
              "the / make / people 没有被判成超纲词"
              if the_beyond == 0 else f"{the_beyond} 个基础词被误判为超纲")

        # And the third way this same field misleads: the tags are split between
        # British and American spellings at random, so reading one spelling's
        # tags alone calls the other out of syllabus. `labour` is CET-4 and
        # `labor` carries only `ky`; `judgement` is CET-4 and `judgment` carries
        # nothing at all. This is the known-answer sample for that — it went
        # unchecked for a phase and `labor` read as out of syllabus 41 times.
        spelling_beyond = get_connection("content").execute(
            "SELECT COUNT(*) FROM reading_tokens WHERE beyond = 1 AND headword IN"
            " ('labor','center','judgment','organisation','neighbor','theater','honor')"
        ).fetchone()[0]
        check("3.5", "英美拼写的考纲标签是合起来判的",
              spelling_beyond == 0,
              "labor / center / judgment 这些没有被判成超纲词——"
              "ECDICT 把标签随机分给英式或美式其中一边，必须并起来看"
              if spelling_beyond == 0
              else f"{spelling_beyond} 处美式／英式拼写被误判为超纲")

        # The fourth way this same question was got wrong, and the one that hid
        # the longest: `beyond` was decided from the lemma's own tags alone, so
        # a word whose root is in the syllabus was called out of it. Measured
        # before the fix: 3,005 of 8,401 flags in the corpus, 35.8%. These are
        # the known-answer sample — every one is a word no reader would call
        # out of CET-6, and each covers a different one of the four checks.
        derived_beyond = get_connection("content").execute(
            "SELECT COUNT(*) FROM reading_tokens WHERE beyond = 1 AND headword IN"
            " ('quickly','fully','entirely','effectively','cultural','educational',"
            "  'teeth','phenomena','curricula','data','planning','nationality')"
        ).fetchone()[0]
        check("3.6", "词根在纲内的派生词与屈折形不算超纲",
              derived_beyond == 0,
              "quickly / fully / cultural（A 级派生）、teeth / phenomena（还原失败但词典的"
              "变形表认得）、nationality（要走 national → nation 两步）都没被判成超纲"
              if derived_beyond == 0
              else f"{derived_beyond} 处词根在纲内的词被误判为超纲")

        # One rule, one place. The three callers that ask "is this in the
        # syllabus" each used to answer it themselves and each stopped at a
        # different point; this is what stops that happening a fifth time.
        import inspect
        from backend.modules.vocabulary import syllabus as _syllabus
        from backend.modules.generation import checker as _checker
        callers_share = (
            "syllabus.known" in inspect.getsource(ingest._is_beyond)
            and "syllabus.within" in inspect.getsource(difficulty.within)
            and "syllabus." in inspect.getsource(_checker.check)
        )
        check("3.7", "三个调用方问的是同一个函数",
              callers_share,
              "ingest、difficulty、generation checker 都走 vocabulary.syllabus，"
              "没有第二份实现"
              if callers_share else "有调用方自己实现了考纲判定")

        # --- 4. 客户端契约 ------------------------------------------------ #
        print("\n4. 客户端契约")

        check("4.1", "客户端令牌碰不到管理接口",
              client.get("/v1/admin/reading/stats", headers=head).status_code in (401, 403),
              "铁律 4")

        lib = client.get("/v1/client/library?shelf=all", headers=head).json()
        check("4.2", "文章清单带难度指标，可切换排序",
              lib["articles"] and "difficulty" in lib["articles"][0]
              and len(lib["sortable"]) >= 6,
              f"{len(lib['articles'])} 篇，可排序字段 {len(lib['sortable'])} 个")

        check("4.3", "身份由令牌推导并回显",
              lib["learner"]["id"] == 1 and "level" in lib["learner"],
              f"learner={lib['learner']}（路径里不出现人的编号）")

        caps = lib["capabilities"]
        check("4.4", "能力声明区分「没数据」与「没实现」",
              set(caps) >= {"level_estimate", "memory_state", "proper_noun_notes",
                            "exam_frequency"},
              ", ".join(f"{k}={v}" for k, v in caps.items()))

        art = client.get(f"/v1/client/articles/{article_id}", headers=head).json()
        check("4.5", "一次下发全文、标注与已有标记",
              len(art["sentences"]) > 5 and len(art["tokens"]) > 100 and art["glossary"],
              f"{len(art['sentences'])} 句 / {len(art['tokens'])} token /"
              f" {len(art['glossary'])} 个词条")

        # --- 5. 点词查词的各个分支 ---------------------------------------- #
        print("\n5. 点词查词")

        sample = next((t for t in art["tokens"] if (t["sense_id"] or 0) > 0), None)
        gloss = art["glossary"][sample["headword"]] if sample else {}
        sense = next((s for s in gloss.get("senses", []) if s["id"] == sample["sense_id"]), None)
        check("5.1", "三层释义都在",
              bool(sense and sense["concept_en"] and gloss.get("translation")
                   and "exam" in sense),
              f"① {sample['surface']} = {'／'.join(sense['gloss_zh']) if sense else '?'}"
              f" ② 词典释义在 ③ 考频槽位在（{'有值' if sense and sense['exam'] else '待统计'}）")

        derived = [t for t in art["tokens"]
                   if t["headword"] and art["glossary"][t["headword"]].get("derivation")]
        check("5.2", "派生词给构词分解",
              len(derived) > 0,
              f"{len(derived)} 个，例如 "
              + "、".join(f"{t['surface']}←{art['glossary'][t['headword']]['derivation']['root']}"
                          for t in derived[:3]))

        check("5.3", "专有名词可点但不计生词率",
              any(t["kind"] == "proper" for t in art["tokens"]),
              f"{sum(1 for t in art['tokens'] if t['kind'] == 'proper')} 个，"
              "不进难度指标、不进复习队列")

        check("5.4", "超纲词：生成文标注，真题不标注",
              art["article"]["mark_beyond"] is False,
              "真题里遇到不认识的词本来就是要练的内容（C10）")

        check("5.5", "未来资产留了位置且为空",
              sample["note"] is None and sense["memory"] is None
              and art["article"]["difficulty_for_you"] is None
              and derived and art["glossary"][derived[0]["headword"]]["derivation"]["root_known"] is None,
              "note / memory / difficulty_for_you / root_known 都在，值为空")

        # --- 6. 事件与记账 ------------------------------------------------ #
        print("\n6. 事件与记账")

        stamp = str(int(time.time()))
        hw, sid = sample["headword"], sample["sense_id"]
        batch = [
            {"idem_key": f"v2-open-{stamp}", "type": "article.opened",
             "payload": {"article_id": article_id}},
            {"idem_key": f"v2-mark-{stamp}", "type": "word.marked",
             "payload": {"article_id": article_id, "headword": hw, "sense_id": sid,
                         "kind": "fuzzy"}},
            {"idem_key": f"v2-prog-{stamp}", "type": "article.progress",
             "payload": {"article_id": article_id, "sentence_seq": 4, "percent": 30.0}},
        ]
        first = client.post("/v1/client/events", headers=head, json={"events": batch}).json()
        again = client.post("/v1/client/events", headers=head, json={"events": batch}).json()
        check("6.1", "批量上报，重放不产生重复记录",
              first["accepted"] == 3 and again["accepted"] == 0 and again["duplicates"] == 3,
              f"首次 {first['accepted']} 条，重放 {again['duplicates']} 条判为重复")

        raw = get_connection("events").execute(
            "SELECT COUNT(*) FROM client_events WHERE idem_key LIKE ?", (f"v2-%-{stamp}",)
        ).fetchone()[0]
        check("6.2", "原始事件另存一层，可重放",
              raw == 3,
              "业务表改写入逻辑时能拿历史事件重跑，不丢数据")

        art2 = client.get(f"/v1/client/articles/{article_id}", headers=head).json()
        check("6.3", "标记回读得到，跨文章按义项互通",
              art2["glossary"][hw]["marks"].get(str(sid)) == "fuzzy",
              f"{hw} 义项 {sid} = 模糊；同词其他义项的标记也一并下发，供弹层提示")

        check("6.4", "阅读进度保存",
              art2["progress"]["percent"] >= 30.0,
              f"{art2['progress']['percent']}%")

        # --- 7. 读完才记账 ------------------------------------------------ #
        print("\n7. 读完才记账")

        unread = get_connection("content").execute(
            "SELECT COUNT(*) FROM reading_articles WHERE read_at IS NULL"
        ).fetchone()[0]
        # Words the learner marked by hand are legitimately tracked from the
        # moment they marked them, whether or not they went on to finish the
        # article — so they are excluded here. What must never appear is a word
        # that got into the ledger purely by an article being *ingested*.
        leaked = get_connection("events").execute(
            "SELECT COUNT(*) FROM study_states s"
            " WHERE s.introduced_article_id IN"
            "   (SELECT id FROM reading_articles WHERE read_at IS NULL)"
            " AND NOT EXISTS (SELECT 1 FROM study_marks m"
            "   WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type"
            "     AND m.item_key = s.item_key)"
        ).fetchone()[0]
        check("7.1", "入库但没读的文章，其目标词仍在「没学过」",
              leaked == 0,
              f"{unread} 篇未读，没有一个词被误记为已学"
              if leaked == 0 else f"{leaked} 个词被未读文章误记为已学")

        draft = get_connection("content").execute(
            "SELECT id FROM generation_drafts WHERE model = 'gpt-5.5'"
            " AND target_words != '' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if draft:
            gen_id = ingest.ingest_draft(int(draft["id"]))
            targets = get_connection("content").execute(
                "SELECT COUNT(DISTINCT headword) FROM reading_tokens"
                " WHERE article_id = ? AND is_target = 1", (gen_id,)
            ).fetchone()[0]
            check("7.2", "生成文章的目标词被标出来",
                  targets > 0,
                  f"{targets} 个目标词——这是「这篇为教哪些词而写」，不是「读完会记哪些词」")

            before = repository.state_counts().get("reviewing", 0)
            service._finish_article(1, gen_id)
            after = repository.state_counts().get("reviewing", 0)
            check("7.3", "读完不会自动把目标词塞进复习队列",
                  after == before,
                  f"复习中 {before} → {after}，读完本身不加词")
        else:
            check("7.2", "生成文章的目标词被标出来", False, "没有带目标词的 gpt-5.5 草稿")

        unmarked = get_connection("events").execute(
            "SELECT COUNT(*) FROM study_states s WHERE s.pool = 'reviewing'"
            " AND NOT EXISTS (SELECT 1 FROM study_marks m"
            "   WHERE m.learner_id = s.learner_id AND m.item_type = s.item_type"
            "     AND m.item_key = s.item_key AND m.sense_id = s.sense_id)"
        ).fetchone()[0]
        check("7.4", "复习队列里的每一条都追得到一次标记",
              unmarked == 0,
              "进队列只有一条路：你自己标记。系统不替你判断你会不会"
              if unmarked == 0 else f"{unmarked} 条没有对应的标记")

        # This check used to assert that the scheduling columns did **not**
        # exist, guarding "P2 must not build scheduling". P3 built it, so the
        # columns are there legitimately and that phrasing expired. What still
        # matters is the half that was always the point: **P2's own pipeline
        # must not touch them.** Ingest analysing an article may not give a word
        # a due date any more than it may put one in the review pool.
        scheduled_but_unread = get_connection("events").execute(
            "SELECT COUNT(*) FROM study_states WHERE due_at IS NOT NULL AND reps = 0"
        ).fetchone()[0]
        check("7.5", "入库与阅读都不碰调度字段",
              scheduled_but_unread == 0,
              "有到期时间的条目都真复习过——入库、读完、标记都不写调度，那是复习模块的事"
              if scheduled_but_unread == 0
              else f"{scheduled_but_unread} 条有到期时间却从没复习过")

        # --- 7b. 义项集变动后的引用完整性 ---------------------------------- #

        dangling = {
            "标注": get_connection("content").execute(
                "SELECT COUNT(*) FROM reading_tokens t LEFT JOIN senses c"
                " ON c.id = t.sense_id WHERE t.sense_id > 0 AND c.id IS NULL"
            ).fetchone()[0],
            "标记": get_connection("events").execute(
                "SELECT COUNT(*) FROM study_marks w LEFT JOIN senses c"
                " ON c.id = w.sense_id WHERE w.item_type = 'word' AND w.sense_id > 0"
                " AND c.id IS NULL"
            ).fetchone()[0],
            "掌握状态": get_connection("events").execute(
                "SELECT COUNT(*) FROM study_states s LEFT JOIN senses c"
                " ON c.id = s.sense_id WHERE s.item_type = 'word' AND s.sense_id > 0"
                " AND c.id IS NULL"
            ).fetchone()[0],
        }
        check("7.6", "没有指向已删除义项的悬空引用",
              not any(dangling.values()),
              "补一个词的义项会换掉全部义项 id；senses.replaced 事件让阅读模块自动修复"
              if not any(dangling.values())
              else "、".join(f"{k} {v}" for k, v in dangling.items() if v))

        # --- 7c. 词组 ------------------------------------------------------ #
        print("\n7c. 词组")

        from backend.modules.reading import phrases as phrase_module

        conn = get_connection("content")
        pstats = phrase_module.stats()
        article_count = len(repository.list_articles(shelf="all", limit=10000))

        check("7.7", "词组候选已扫出来",
              pstats["candidates"] > 100,
              f"{pstats['candidates']} 处候选，平均每篇 "
              f"{pstats['candidates'] / max(1, article_count):.1f} 处")

        # The structural filter is what keeps dictionary noise out: `to be`,
        # `the world` and `there is` all have dictionary entries, and none of
        # them starts with a verb.
        noise = conn.execute(
            "SELECT COUNT(*) FROM reading_phrases WHERE phrase IN"
            " ('to be','have been','the world','there is','that is','such as')"
        ).fetchone()[0]
        check("7.8", "词典噪音没有进候选",
              noise == 0,
              "动词+小品词的结构预筛挡住了 to be / the world / there is 这类"
              if noise == 0 else f"{noise} 处噪音混进来了")

        # **Waits, rather than reading the count once.** This script ingests an
        # article of its own, ingestion proposes phrase candidates, and judging
        # them is an async job — so the first read happens while the script's
        # own work is still in flight, and the check fails on a number it
        # created itself moments earlier. Measured 2026-09-12: five pending
        # during the run, zero a few seconds after it. 坑 §4.6 is the sibling
        # of this one — a verify script tripping over what it set in motion.
        #
        # Bounded, because a wait with no exit is how a check becomes a hang.
        pending = pstats["pending"]
        if pending:
            deadline = time.time() + 120
            while pending and time.time() < deadline:
                time.sleep(5)
                pending = conn.execute(
                    "SELECT COUNT(*) FROM reading_phrases WHERE verdict IS NULL"
                ).fetchone()[0]
            pstats = phrase_module.stats()

        check("7.9", "候选已逐处判断过",
              pstats["pending"] == 0 and pstats["confirmed"] > 0,
              f"判为词组 {pstats['confirmed']} 处（{pstats['distinct']} 个不同），"
              f"判为字面用法 {pstats['rejected']} 处，待判 {pstats['pending']}"
              + ("" if not pending else "——等了两分钟还没判完，"
                 "要么模型那条路断了，要么这个上限该调"))

        # The judgement has to be per occurrence, not per string — `look at` is
        # a phrase in one sentence and two words in the next. If every
        # occurrence of a sequence got the same verdict, the model is matching
        # strings and the sentence is doing nothing.
        check("7.10", "判断是按上下文做的，不是按字符串",
              pstats["context_dependent"] > 0,
              f"{pstats['context_dependent']} 个序列在不同句子里得到了不同判定"
              if pstats["context_dependent"] else "每个序列的判定都一样，句子没起作用")

        # A confirmed phrase that (a) has a gloss to show and (b) contains a
        # word the learner already has records for — otherwise "the word's rows
        # did not change" is true of zero rows and proves nothing. That is the
        # same trap as a test reporting zero on a known-good sample: it has to
        # be able to fail before passing means anything.
        select_phrase = (
            "SELECT p.article_id, p.phrase, p.start_seq, p.end_seq FROM reading_phrases p"
            " JOIN phrases d ON d.phrase = p.phrase"
            " WHERE p.verdict = 1 AND d.translation IS NOT NULL{extra}"
            " ORDER BY p.article_id LIMIT 1"
        )
        phrase_row = conn.execute(select_phrase.format(
            extra=" AND EXISTS (SELECT 1 FROM study_marks m WHERE m.item_type = 'word'"
                  "   AND p.phrase LIKE m.item_key || ' %')")).fetchone()
        if phrase_row is None:
            phrase_row = conn.execute(select_phrase.format(extra="")).fetchone()

        if phrase_row is None:
            for number in ("7.11", "7.12", "7.13"):
                check(number, "词组相关检查", False, "没有已确认的词组")
        else:
            payload = service.article(1, int(phrase_row["article_id"]))
            spans = {p["phrase"]: p for p in payload["phrases"]}
            sample = spans.get(phrase_row["phrase"])
            covered = [t for t in payload["tokens"]
                       if sample and sample["start_seq"] <= t["seq"] <= sample["end_seq"]]
            check("7.11", "词组随文章下发：跨度、释义，两半都标了 in_phrase",
                  bool(sample and sample["end_seq"] > sample["start_seq"]
                       and sample["translation"] and len(covered) >= 2
                       and all(t["in_phrase"] for t in covered)),
                  f"{len(payload['phrases'])} 处，例如 {sample['surface']} → "
                  f"{sample['translation']}（token {sample['start_seq']}–{sample['end_seq']} "
                  "都指向它，点任一半都开词组面板）" if sample else "这篇没有下发词组")

            # The separation the design insists on, tested by doing it: mark the
            # phrase, then look at every record belonging to the word inside it.
            # Not knowing `account for` says nothing about `account`, and one
            # mark standing for both would put a word the reader has mastered
            # into the review queue.
            inside = next((t for t in covered
                           if t["kind"] == "content" and t["headword"]),
                          covered[0] if covered else None)
            word = inside["headword"] if inside else ""

            def word_records() -> list[tuple]:
                return [tuple(r) for r in conn.execute(
                    "SELECT item_type, item_key, sense_id, kind FROM study_marks"
                    " WHERE item_key = ?"
                    " UNION ALL"
                    " SELECT item_type, item_key, sense_id, pool FROM study_states"
                    " WHERE item_key = ?", (word, word)).fetchall()]

            # **自己种一条组成词的记录，不靠「今天碰巧有」**（坑 §4.1）。
            # 这条检查要证明的是「标词组不牵动组成词」，而没有组成词的记录时
            # 它什么也证明不了——原文只能报一句「这条检查等于没查」，
            # 而那在学习记录清空之后就是常态（P10 清空过一次）。
            stamp0 = str(int(time.time()))
            client.post("/v1/client/events", headers=head, json={"events": [
                {"idem_key": f"v2-ph-seed-{stamp0}", "type": "word.marked",
                 "payload": {"article_id": phrase_row["article_id"], "item_type": "word",
                             "item_key": word, "headword": word,
                             "sense_id": 0, "kind": "unknown"}}]})

            before = word_records()
            stamp2 = str(int(time.time()))
            client.post("/v1/client/events", headers=head, json={"events": [
                {"idem_key": f"v2-ph-mark-{stamp2}", "type": "word.marked",
                 "payload": {"article_id": phrase_row["article_id"], "item_type": "phrase",
                             "item_key": phrase_row["phrase"], "headword": phrase_row["phrase"],
                             "sense_id": 0, "kind": "unknown"}}]})
            after = word_records()
            phrase_mark = conn.execute(
                "SELECT kind FROM study_marks WHERE item_type = 'phrase' AND item_key = ?",
                (phrase_row["phrase"],)).fetchone()
            check("7.12", "标记词组不动组成词的任何记录",
                  after == before and phrase_mark is not None and len(before) > 0,
                  f"标了「{phrase_row['phrase']}」之后，"
                  f"「{word}」的标记与掌握状态共 {len(before)} 行一行未变"
                  if after == before and before
                  else (f"「{word}」名下一条记录都没有，这条检查等于没查"
                        if after == before else f"「{word}」的记录被牵连改动了"))

            # 种下的那条组成词标记用完就撤，前后各清一次的规矩在这里是
            # 「造完即拆」——留着它会让下一轮的 `before` 不再是干净的起点。
            client.post("/v1/client/events", headers=head, json={"events": [
                {"idem_key": f"v2-ph-unseed-{stamp0}", "type": "word.unmarked",
                 "payload": {"item_type": "word", "item_key": word,
                             "headword": word, "sense_id": 0}}]})

            # And taking it back takes it out of the queue again, which is the
            # same invariant read backwards.
            client.post("/v1/client/events", headers=head, json={"events": [
                {"idem_key": f"v2-ph-unmark-{stamp2}", "type": "word.unmarked",
                 "payload": {"item_type": "phrase", "item_key": phrase_row["phrase"],
                             "headword": phrase_row["phrase"], "sense_id": 0}}]})
            left = conn.execute(
                "SELECT COUNT(*) FROM study_states WHERE item_type = 'phrase'"
                " AND item_key = ? AND pool = 'reviewing'", (phrase_row["phrase"],)
            ).fetchone()[0]
            check("7.13", "撤销标记后该条目退出复习队列",
                  left == 0,
                  "进队列只有你的标记这一条路，撤回标记就退出——"
                  "遇见记录仍留着" if left == 0 else "撤销后还留在复习中")

        # 落在词组里的 token 打了标记之后，漏义项报告里不该再有 get up / at least
        # 这类——它们从来不是义项集的缺口，没有哪个 get 的义项能覆盖 get up。
        missing = client.get("/v1/admin/reading/missing-senses",
                             headers=admin_head).json()
        leftover_phrases = [i["headword"] for i in missing.get("items", [])
                            if i["headword"] in ("get", "least", "known")]
        check("7.14", "考频清账后，漏义项报告里不再有词组",
              not leftover_phrases and pstats["tokens_in_phrase"] > 0,
              f"{pstats['tokens_in_phrase']} 个 token 标为落在词组里，"
              f"报告剩 {len(missing.get('items', []))} 个词，都是真缺口"
              if not leftover_phrases else f"仍混着词组：{leftover_phrases}")

        # --- 7d. 学习记录合表 ---------------------------------------------- #
        print("\n7d. 学习记录合表")

        # **`sqlite_master` 是每个文件一张**（P9 §10 把库拆成了五个），
        # 所以问「这张表在不在」必须问对文件——问错的那次会答「不在」，
        # 而这一条正好是在验「不在」，于是错误地通过或错误地报红都很容易。
        # 学习记录那几张在 events.db。
        renamed = {r["name"] for r in get_connection("events").execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        check("7.15", "word_marks / sense_states 已并成 study_marks / study_states",
              {"study_marks", "study_states"} <= renamed
              and not ({"word_marks", "sense_states"} & renamed),
              "复习调度要把单词和词组混在一张列表里排序，分表就要把核心算法写两遍")

        columns = {c["name"] for c in conn.execute(
            "PRAGMA table_info(study_marks)").fetchall()}
        try:
            conn.execute(
                "INSERT INTO study_marks (learner_id, item_type, item_key, sense_id,"
                " kind, created_at) VALUES (1,'bogus','x',0,'unknown','x')")
            conn.rollback()
            constrained = False
        except sqlite3.IntegrityError:
            conn.rollback()
            constrained = True
        check("7.16", "条目类型由数据库约束，不靠自觉",
              {"item_type", "item_key"} <= columns and constrained,
              "item_type 只允许 word / phrase，写错直接报错；"
              "(learner_id, item_type, item_key, sense_id) 唯一")

        # --- 8. 模块自包含 ------------------------------------------------ #
        print("\n8. 模块自包含")

        from backend.core.registry import installed_modules
        mod = installed_modules().get("reading")
        check("8.1", "阅读模块自带表、两套接口、管理页与配置",
              bool(mod and mod.migrations and mod.client_router and mod.admin_router
                   and mod.admin_pages),
              f"{len(mod.migrations)} 个迁移，客户端与管理接口各一套，"
              f"管理页 {[p.path for p in mod.admin_pages]}")

        specs = {s.key for s in __import__(
            "backend.core.runtime_config", fromlist=["specs"]).specs()}
        wanted = {"reading_fresh_days", "annotate_batch_words", "difficulty_weights",
                  "library_default_sort", "progress_report_seconds"}
        check("8.2", "参数一律配置化",
              wanted <= specs,
              "、".join(sorted(wanted)))

        # **清理放在最后一步**（「后台任务与等待」那条）。这里还要多一句:
        # 清完之后断言真的清干净了——**一个说不清自己清没清的清理等于没清**，
        # 而这一次的代价是「别人手机上多出一批没标过的词」。
        from fixture_events import purge  # noqa: PLC0415 - 只这一处要
        result = purge(get_connection("events"))
        check("6.10", "灌进会合点的夹具事件全部清掉了，而真标记一条没动",
              result["leftover"] == 0,
              f"删了 {result['removed']} 条事件；派生的标记删了 "
              f"{len(result['marks_dropped'])} 个、"
              f"**留下 {len(result['marks_kept'])} 个有真事件支持的**。"
              "不清掉会被每台设备拉下来当成真的学习历史——"
              "2026-09-18 真机上就这么中过一次（776 条里 240 条是夹具）"
              if result["leftover"] == 0 else f"还剩 {result['leftover']} 条没清掉")

        # --- 人工 ---------------------------------------------------------- #
        print("\n需要人工确认")
        # **2026-09-18 改写（P9 §11）:Web 阅读页删了，这一条改指手机或 ercli。**
        # 它原本指着 `/admin/reader`——一个在浏览器里实现了半套学习引擎的页面，
        # 而新架构下那是禁止的（客户端只有一份 Core，不许再写第二份）。
        note("M1", "真读完一篇",
             "在手机上或用 ercli 挑一篇，点词、标记、读到底，全程不卡。"
             "**这一条不能再在浏览器里做了**——读一篇文章现在只发生在"
             "走同一份 Core 的客户端上")
        note("M2b", "词组：整体渲染、整体标记，并追问组成词",
             "点 account for 的任一半，都应显示词组释义而不是 account 的「账户」，"
             "且选中框框住整个词组；标记之后整个词组一起变底色，同一词组的每一处都变；"
             "静止状态正文里不加任何记号——两条通道已经占满了；"
             "标了不认识之后，下面出现「那 account 本身呢？」，逐义项可单独标")
        note("M2", "点词弹层的四个分支都对",
             "普通生词看三层释义；派生词看构词分解；人名地名说可以跳过；"
             "超纲词说暂时不用学会（只在生成文里）")
        note("M3", "同词不同义项的提示看得懂",
             "标记一个多义词的某个义项，再找另一篇它用别的义项的地方，"
             "应显示「你标记过它的另一个义项」而不是一片空白")
        note("M4", "把后端停掉，继续读完当前这篇",
             "标记照常，重启后事件全部补报且不重复——这是唯一真正检验离线形状的一条")
        note("M5", "批量补跑后，第三层释义有内容",
             "在 /admin/reading 上批量预备，跑完点「重算考频」，senses.exam_frequency 不再全是 0；"
             "点词时第三层并列显示该词各义项的真题次数与占比")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(manual)} 项待人工")
    if failed:
        print("  失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
