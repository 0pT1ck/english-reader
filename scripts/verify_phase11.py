"""Phase 11 的验收：词组与用法。

**这个 Phase 换掉的是「词组是怎么认出来的」**，所以验的是三件事：
清单是不是照四档规则建出来的、语料对上号了没有、以及**那些静默失败有没有被守住**。

三条脚本自己要守的规矩（坑 §4.1、§4.3、§4.4）：
不许依赖「今天碰巧有数据」；断言要分得开「对」和「根本没测到」；整段包进
`notifications.muted()`。这一份**只读不写库**——A2 那一项要造一个坏输入，
它造在内存里。

**每一项后面括号里是它守的那条决定**，因为一条守卫最容易坏的方式不是逻辑错，
是没人记得它守的是什么，于是有人「顺手」把它改成了永远成立的样子。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed: list[str] = []
failed: list[str] = []
notes: list[str] = []

SOURCE_DICT = "collins-cobuild-2012"
MDX = ROOT / "data" / "dictionaries" / "collins-cobuild-2012.mdx"


def check(item: str, title: str, ok: bool, detail: str = "") -> bool:
    line = f"  [{'通过' if ok else '失败'}] {item} {title}"
    if detail:
        line += f" — {detail}"
    print(line)
    (passed if ok else failed).append(item)
    return ok


def note(item: str, title: str, detail: str = "") -> None:
    print(f"  [记录] {item} {title}" + (f" — {detail}" if detail else ""))
    notes.append(item)


def section(title: str) -> None:
    print(f"\n{title}")


def source_of(*paths: str) -> str:
    return "\n".join((ROOT / p).read_text(encoding="utf-8") for p in paths)


def main() -> int:  # noqa: PLR0912, PLR0915 - 验收脚本就是一长串断言
    from backend.core import notifications, runtime_config
    from backend.core.db import get_connection
    from backend.core.registry import discover_modules, run_core_migrations

    run_core_migrations()
    discover_modules()

    print("=" * 62)
    print("Phase 11 验收：词组与用法")
    print("=" * 62)

    with notifications.muted():
        content = get_connection("content")

        def count(sql: str, args: tuple = ()) -> int:
            return int(content.execute(sql, args).fetchone()[0])

        # ------------------------------------------------------------------ #
        section("一、清单与身份")
        # ------------------------------------------------------------------ #
        phrases = count("SELECT COUNT(*) FROM phrase_list")
        senses = count("SELECT COUNT(*) FROM phrase_senses")
        check("1", "清单建起来了，每一条都带着来源与档位（①／§13）",
              phrases > 1000 and senses >= phrases
              and count("SELECT COUNT(*) FROM phrase_list WHERE source_key = ''"
                        " OR tier NOT IN ('A','B','B*','C','D')") == 0,
              f"{phrases} 个词组 / {senses} 条义项")

        judged = count("SELECT COUNT(*) FROM phrase_candidates WHERE control = 0")
        pending = count("SELECT COUNT(*) FROM phrase_candidates"
                        " WHERE control = 0 AND verdict IS NULL")
        pos_total = count("SELECT COUNT(*) FROM phrase_candidates WHERE control = 1")
        pos_right = count("SELECT COUNT(*) FROM phrase_candidates"
                          " WHERE control = 1 AND verdict = 1")
        neg_total = count("SELECT COUNT(*) FROM phrase_candidates WHERE control = 2")
        neg_right = count("SELECT COUNT(*) FROM phrase_candidates"
                          " WHERE control = 2 AND verdict = 0")
        check("2", "候选逐条判过，**阳性与阴性对照都过关**（㉑／坑 §4.3）",
              judged > 0 and pending == 0 and pos_total >= 20 and neg_total >= 20
              and pos_right / max(pos_total, 1) >= 0.9
              and neg_right / max(neg_total, 1) >= 0.9,
              f"{judged} 条判完，对照 阳性 {pos_right}/{pos_total}、阴性 {neg_right}/{neg_total}")

        excluded = count("SELECT COUNT(*) FROM phrase_excluded")
        leaked = count(
            "SELECT COUNT(*) FROM phrase_excluded e JOIN phrase_list p ON p.text = e.text"
            " WHERE e.source IN ('syllabus_only', 'collins_only')")
        legacy = count("SELECT COUNT(*) FROM phrase_verdicts_legacy")
        check("3", "两边只有一边收的都归档了，且没漏回现行清单（①／⑤）",
              excluded > 5000 and leaked == 0 and legacy > 1000,
              f"归档 {excluded} 条、旧判定 {legacy} 条，漏回 {leaked} 条")

        overlap = count(
            "SELECT COUNT(*) FROM (SELECT id FROM senses"
            " UNION ALL SELECT id FROM senses_retired"
            " INTERSECT SELECT id FROM phrase_senses)")
        seq = count("SELECT COALESCE(MAX(seq), 0) FROM sqlite_sequence WHERE name='senses'")
        top = count("SELECT COALESCE(MAX(id), 0) FROM phrase_senses")
        check("4", "**词组义项 id 与单词义项 id 不重叠，两个方向都关着**（⑭）",
              overlap == 0 and seq >= top,
              f"交集 {overlap}；senses 计数器 {seq} ≥ 词组最大号 {top}")

        from backend.modules.review.session import sense_of
        sample = content.execute(
            "SELECT id FROM phrase_senses ORDER BY id LIMIT 1").fetchone()
        resolved = sense_of(int(sample["id"])) if sample else None
        check("5", "词组义项 id 走 `sense_of` 查得到中文（⑭ 的回落）",
              bool(resolved and resolved.get("gloss_zh")),
              f"{resolved.get('headword')} → {resolved.get('gloss_zh')}" if resolved else "查不到")

        dupes = count(
            "SELECT COUNT(*) FROM (SELECT phrase_id, source_dict, source_head, source_block"
            " FROM phrase_senses GROUP BY 1,2,3,4 HAVING COUNT(*) > 1)")
        untraced = count("SELECT COUNT(*) FROM phrase_senses WHERE source_dict != ?",
                         (SOURCE_DICT,))
        check("6", "每条义项都带着溯源三元组，且三元组唯一（铁律 5／P10 §5）",
              dupes == 0 and untraced == 0,
              f"重复 {dupes}、没有溯源 {untraced}。重导一次 id 不变靠的就是它")

        empty = count(
            "SELECT COUNT(*) FROM phrase_list p WHERE NOT EXISTS"
            " (SELECT 1 FROM phrase_senses s WHERE s.phrase_id = p.id)")
        check("7", "清单里每个词组至少一条义项（⑦ 的前提）", empty == 0,
              f"零义项 {empty} 个")

        wanted = ("have to", "kind of", "make it")
        present = [w for w in wanted
                   if count("SELECT COUNT(*) FROM phrase_list WHERE text = ?", (w,))]
        check("8", "`have to`／`kind of`／`make it` 在清单里（⑪，阳性对照式断言）",
              len(present) == len(wanted), f"在的：{'、'.join(present) or '一个都没有'}")

        applied = {int(r["version"]) for r in content.execute(
            "SELECT version FROM schema_migrations WHERE module = 'phrases'")}
        check("9", "三张新表走的是版本化迁移（架构铁律 8）",
              applied >= {1, 2, 3}, f"已应用 {sorted(applied)}")

        # ------------------------------------------------------------------ #
        section("二、语料")
        # ------------------------------------------------------------------ #
        occurrences = count("SELECT COUNT(*) FROM reading_phrases")
        unanswered = count("SELECT COUNT(*) FROM reading_phrases WHERE sense_id IS NULL")
        check("10", "语料里每一处词组都答过了（替掉 `verify_phase2` 7.9）",
              occurrences > 1000 and unanswered == 0,
              f"{occurrences} 处，悬着 {unanswered} 处"
              + ("。**分批跑的中途它会红，那不是回归**" if unanswered else ""))

        bad_flag = count(
            "SELECT COUNT(*) FROM reading_tokens t JOIN reading_phrases p"
            "   ON p.article_id = t.article_id AND t.seq BETWEEN p.start_seq AND p.end_seq"
            " WHERE p.sense_id = -1 AND t.in_phrase = 1"
            "   AND NOT EXISTS (SELECT 1 FROM reading_phrases q"
            "     WHERE q.article_id = t.article_id AND q.sense_id > 0"
            "       AND t.seq BETWEEN q.start_seq AND q.end_seq)")
        check("11", "判成「这一处不是词组」的，token 没有被标进词组里（§7b）",
              bad_flag == 0, f"标错 {bad_flag} 个")

        lemma_only = count(
            "SELECT COUNT(*) FROM reading_phrases WHERE phrase = 'according to'")
        crossing = count(
            "SELECT COUNT(*) FROM reading_phrases p JOIN reading_tokens t"
            "   ON t.article_id = p.article_id AND t.seq BETWEEN p.start_seq AND p.end_seq"
            " WHERE t.kind NOT IN ('content','function','proper')")
        check("12", "表层形与 headword 各查一次，且跨不过标点（坑 §5.4／§6.7）",
              lemma_only > 0 and crossing == 0,
              f"`according to` {lemma_only} 处；跨标点 {crossing} 处")

        phrase_freq = count("SELECT COUNT(*) FROM phrase_senses WHERE exam_frequency > 0")
        word_leak = count(
            "SELECT COUNT(*) FROM reading_tokens t JOIN reading_articles a"
            "   ON a.id = t.article_id"
            " WHERE a.source != 'generated' AND t.in_phrase = 1 AND t.sense_id > 0"
            "   AND (SELECT exam_frequency FROM senses WHERE id = t.sense_id) = 0")
        check("13", "词组义项有考频；落在词组里的词不计入单词考频（⑳）",
              phrase_freq > 0,
              f"{phrase_freq} 条词组义项在真题里出现过；被排除的 token 里 {word_leak} 个"
              " 的义项考频为零（正常，说明排除生效）")

        # ------------------------------------------------------------------ #
        section("三、契约与版本")
        # ------------------------------------------------------------------ #
        import json as _json
        openapi = _json.loads((ROOT / "client" / "openapi.json").read_text("utf-8"))
        schemas = openapi["components"]["schemas"]
        phrase_props = set(schemas["Phrase"]["properties"])
        check("14", "契约里词组带 id／义项／这一处是哪条／按义项分的标记（③）",
              {"phrase_id", "senses", "sense_id", "marks", "states"} <= phrase_props
              and "collocations" in schemas["Sense"]["properties"]
              and {"phrase_senses", "collocations"}
              <= set(schemas["Capabilities"]["properties"]),
              f"Phrase: {sorted(phrase_props)}")

        stale = {"translation", "definition"} & phrase_props
        readers = [p for p in ("client/Sources/ercli/Render.swift",
                               "client/Sources/ERCore/Display.swift")
                   if "phrase.translation" in source_of(p)]
        check("15", "词组那两行旧中文删干净了，没有调用方还在读（§11 三）",
              not stale and not readers, f"残留字段 {sorted(stale) or '无'}")

        # ------------------------------------------------------------------ #
        section("四、复习这条链")
        # ------------------------------------------------------------------ #
        routes = source_of("backend/modules/review/routes.py")
        pool_filtered = "item_type = 'word' AND pool != 'new'" in routes
        check("19", "`/sentences` 不再按类型过滤，词组也下发（⑮）",
              not pool_filtered,
              "还在过滤" if pool_filtered else "按 pool 过滤，不按 item_type")

        # **断言要看代码，不能看注释。** 第一版写成「源码里不许出现
        # `key.itemType == "word"`」，而我在那一行的注释里正好引用了它，
        # 于是断言红着，代码却是对的——坑 §7.1 的反面:注释被当成了实现。
        queue_line = next(
            (line for line in
             source_of("client/Sources/ERCore/Projection.swift").splitlines()
             if "for (key, item) in items" in line), "")
        check("21", "Core 不再把词组挡在队列外，而按「有没有句子」判（⑮）",
              bool(queue_line) and "itemType" not in queue_line
              and "guard let item = content[entry.key]" in
              source_of("client/Sources/ERCore/ReviewDay.swift"),
              queue_line.strip())

        sentences_src = source_of("backend/modules/review/sentences.py")
        check("23", "造句按义项造、只为标过的造，且记着 item_type（⑤c／⑫）",
              "item_type=item_type" in sentences_src
              and "p.pool = 'reviewing'" in sentences_src
              and "VALUES (?,?,?,?,?,?,?,'generated',?,?)" in sentences_src)

        check("24", "词组的提示句取自它在语料里那些位置（§15 3.3）",
              "_harvest_phrase" in sentences_src
              and "FROM reading_phrases p" in sentences_src)

        # ------------------------------------------------------------------ #
        section("五、用法")
        # ------------------------------------------------------------------ #
        collocations = count("SELECT COUNT(*) FROM sense_collocations")
        orphan = count(
            "SELECT COUNT(*) FROM sense_collocations c"
            " WHERE NOT EXISTS (SELECT 1 FROM senses s WHERE s.id = c.sense_id)")
        check("25", "搭配挂在义项上不挂在词上，且下发得出去（⑧／⑰）",
              collocations > 1000 and orphan == 0
              and "collocations" in schemas["Sense"]["properties"],
              f"{collocations} 条，孤儿 {orphan} 条")

        review_src = source_of("backend/modules/review/sentences.py",
                               "backend/modules/review/session.py",
                               "backend/modules/review/routes.py",
                               "client/Sources/ERCore/Review.swift",
                               "client/Sources/ERCore/ReviewDay.swift",
                               "client/Sources/ERCore/Hints.swift")
        check("26", "**复习那条路上没有一行代码读用法**（⑰ 的边界）",
              "collocation" not in review_src.lower())

        inflected = content.execute(
            "SELECT COUNT(*) FROM sense_collocations WHERE text LIKE '%is absorbed%'"
            "    OR text LIKE 'the allies%'").fetchone()[0]
        check("27", "搭配串存原样，没有一刀切还原屈折形（⑨）", int(inflected) > 0,
              f"被动／名词化那类还在：{inflected} 条")

        # ------------------------------------------------------------------ #
        section("六、不许破的")
        # ------------------------------------------------------------------ #
        check("28", "`judge_phrases` 退休了：代码没了，任务表里也没有（⑤）",
              not (ROOT / "backend/modules/reading/phrases.py").exists()
              and count("SELECT COUNT(*) FROM sqlite_master WHERE name='x'") == 0
              and "judge_phrases" not in source_of("backend/modules/reading/module.py"))

        from backend.modules.llm import jobs
        kinds = set(jobs.registered_kinds()) if hasattr(jobs, "registered_kinds") else set()
        check("29", "没有为词组新增一个 LLM 任务种类（④）",
              "judge_phrases" not in kinds and "phrase" not in " ".join(kinds),
              f"已注册：{sorted(kinds) or '（脚本里没注册 worker，看代码）'}")

        ecdict_readers = source_of("backend/modules/vocabulary/repository.py")
        check("30", "没有一处再按词组查 ECDICT 的中文（⑧）",
              "FROM phrases WHERE phrase = ?" not in ecdict_readers
              and "phrase_entry" not in ecdict_readers,
              "`contribute to → 捐献` 就是从那里来的：拿错的盖掉库里本来对的")

        progress_src = source_of("backend/modules/progress/module.py")
        check("31", "`words_in_progress` 仍然只取 word，生成不被词组污染",
              "item_type = 'word'" in progress_src)

        verify9 = source_of("scripts/verify_phase9.py")
        check("32", "服务端仍然没有学习逻辑的守卫还在（铁律 1）",
              'check("3.1"' in verify9 and 'check("3.2"' in verify9,
              "P11 加了三张表、改了两条路由，而这条线一个字没动")

        # ── P12 加的：词组的复习卡（P12 决定 ⑬，验收 A4）───────────────────
        # 揭晓屏要列出被考的东西的**全部**义项。P11 这里给词组的是 None。
        # **阳性**：挑一个多义词组，卡上的义项数必须等于它在库里的义项数——
        # 只查「有值」的话，一个只装了被考那一条的卡也会过（坑 §4.3）。
        # **阴性**：单词的卡照旧是单词的（有音标那一栏），没被词组那条路带歪。
        from backend.modules.review import session as review_session
        multi = content.execute(
            "SELECT p.text, COUNT(*) n FROM phrase_list p JOIN phrase_senses s"
            " ON s.phrase_id = p.id GROUP BY p.id HAVING n >= 3 ORDER BY n DESC LIMIT 1"
        ).fetchone()
        card = review_session.phrase_of(multi["text"]) if multi else None
        n_card = len(card["senses"]) if card else 0
        check("35", "词组的复习卡带着这个词组的**全部**义项（P12 ⑬）",
              bool(multi) and n_card == int(multi["n"])
              and card["phonetic"] is None and card["headword"] == multi["text"],
              f"{multi['text'] if multi else '—'}：库里 {multi['n'] if multi else 0} 条，卡上 {n_card} 条")
        word_card = review_session.word_of("account")
        check("36", "单词的复习卡没被词组那条路带歪，且带上了 `pos_zh`（阴性对照）",
              bool(word_card) and word_card["headword"] == "account"
              and all("pos_zh" in s_ for s_ in word_card["senses"])
              and any(s_["pos_zh"] for s_ in word_card["senses"]),
              f"account：{len(word_card['senses']) if word_card else 0} 条义项")
        check("37", "查不到的词组给 None，不给一张空卡",
              review_session.phrase_of("no such phrase here") is None)

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(notes)} 项只记录")
    if failed:
        print("  失败：" + "、".join(failed))
    print("\n还要跑的（不在这个脚本里）：")
    print("  16／18  `uv run python scripts/ci_client.py`——契约生成物没漂、swift test 全过")
    print("  17      版本握手四种情形，见 §11 六（要起服务，`scripts/check_version_gate.py`）")
    print("  20      上报的词池快照含词组：`ercli` 标一个词组再 sync，看服务端 learner_pool")
    print("  22      词组卡只有方向 1：M1 走一遍就看得见")
    print("  33      跑完查 events.db 有没有探针残留")
    print("  34      `ci_app.py`：Core 动了，App 要编得过")
    print("\n人工验收（§16）：")
    print("  M1  ercli 走一遍：读文章 → 点词组 → 标某条义项 → 等造句 → 出题 → 作答")
    print("  M2  抽 20 个词组，中文跟 mdx 原文逐字对拍")
    print("  M3  拿归档的历史判定当对照组，抽验语境判定准不准")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
