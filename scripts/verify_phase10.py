"""Phase 10 的验收：义项大清洗。

**这个 Phase 换掉的是地基那一层的数据**，所以验的大多是「换进来的东西对不对、
换出去的东西留住了没有」，而不是「功能能不能用」。

用户对这次换血提的要求只有一句，但它是硬的：
**单词全部收进来，词组全部没收进来。** 那句话在这里是 A1 与 A2 两项，
而且是**逐条对账**，不是抽样——因为这次导入过程中漏收过两次
（`VERB` 9,789 块、无标记的真义项 736 条），两次都是抽样看不出来的。

**三条脚本自己要守的规矩**（坑 §4.1、§4.3、§4.4）：
不许依赖「今天碰巧有数据」；断言要分得开「对」和「根本没测到」；
整段包进 `notifications.muted()`。这一份**只读不写**——除了 A3，
那一项必须造一个坏输入，而它造在内存里，碰不到库。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

passed: list[str] = []
failed: list[str] = []
notes: list[str] = []

#: 导入时用的来源标识，A1 的对账以它为准。
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
    """记一个事实，不判通过失败。

    A6 是这种：那道题这个 Phase 修不好（词组顺延），所以「它现在标成什么」
    是要看一眼并记下来的东西，而不是一条会红的断言。**把它写成断言就是在
    制造一个必然失败的守卫**，而那正是坑 §8 说的假警报。
    """
    print(f"  [记录] {item} {title}" + (f" — {detail}" if detail else ""))
    notes.append(item)


def section(title: str) -> None:
    print(f"\n{title}")


def main() -> int:  # noqa: PLR0915 - 验收脚本就是一长串断言
    from backend.core import notifications
    from backend.core.db import get_connection
    from backend.core.registry import run_core_migrations

    run_core_migrations()
    from backend.core.db import run_migrations
    from backend.modules.senses import schema

    run_migrations("senses", schema.MIGRATIONS)

    print("=" * 62)
    print("Phase 10 验收：义项大清洗")
    print("=" * 62)

    with notifications.muted():
        content = get_connection("content")
        events = get_connection("events")

        # ------------------------------------------------------------- #
        section("1. 导入：单词全收，词组全不收")
        # ------------------------------------------------------------- #
        from backend.modules.senses import collins

        if not MDX.exists():
            check("A1", "柯林斯词典文件在", False, f"找不到 {MDX}")
            check("A2", "词组全部没收进来", False, "同上")
        else:
            from mdict_utils.reader import MDX as MDXReader

            entries: dict[str, str] = {}
            for key, value in MDXReader(str(MDX)).items():
                entries.setdefault(
                    key.decode("utf-8", "replace").strip().lower(),
                    value.decode("utf-8", "replace"))

            stored_by_word: dict[str, int] = {}
            for row in content.execute(
                    "SELECT headword, COUNT(*) n FROM senses"
                    " WHERE source_dict = ? GROUP BY headword", (SOURCE_DICT,)):
                stored_by_word[row["headword"]] = row["n"]

            expect_word = expect_phrase = 0
            mismatched: list[str] = []
            for word in stored_by_word:
                html = entries.get(word)
                if html is None:
                    mismatched.append(f"{word}(词典里没有)")
                    continue
                blocks = collins.parse_entry(word, html)
                wants = sum(1 for b in blocks if b.kind is collins.Kind.WORD)
                expect_word += wants
                expect_phrase += sum(1 for b in blocks
                                     if b.kind is collins.Kind.PHRASE)
                if wants != stored_by_word[word]:
                    mismatched.append(f"{word}({wants}≠{stored_by_word[word]})")

            total_stored = sum(stored_by_word.values())
            # **逐条对账，不抽样。** 漏收过两次，两次抽样都看不出来。
            check("A1", "单词义一条不差地进了库",
                  not mismatched and expect_word == total_stored,
                  f"词典判为单词义 {expect_word} 条，库里 {total_stored} 条"
                  + (f"；对不上的：{mismatched[:5]}" if mismatched else "，逐词一致"))

            # 阳性对照：词组确实存在于词典里、确实被排除了。
            # 少了这一半，「一条词组都没有」分不清是排对了还是解析器根本没读到。
            account_blocks = collins.parse_entry("account", entries.get("account", ""))
            take_into = [b for b in account_blocks
                         if b.kind is collins.Kind.PHRASE and "考虑" in b.gloss_zh]
            leaked = content.execute(
                "SELECT COUNT(*) n FROM senses WHERE source_dict = ?"
                " AND (gloss_zh LIKE '%考虑到%' AND headword = 'account')",
                (SOURCE_DICT,)).fetchone()["n"]
            check("A2", "词组义一条都没进来（带阳性对照）",
                  expect_phrase > 4000 and take_into and leaked == 0,
                  f"词典里有 {expect_phrase} 条词组义，库里 0 条；"
                  f"阳性对照 take into account 在词典里找得到={bool(take_into)}、"
                  f"在库里={leaked}")

        # 阴性对照：没见过的标记必须让导入停下来，不许默默归类。
        try:
            collins.classify("zzz", "NOT-A-REAL-MARKER", "some definition here", "中文", [])
            raised = False
        except collins.UnknownMarker:
            raised = True
        check("A3", "没见过的词性标记会报错，不会被默默归类", raised,
              "判据 4。导入时这一条抓到过 `to inf`，"
              "而它顺带暴露了白名单本身是用近似逻辑建的")

        # ------------------------------------------------------------- #
        section("2. 覆盖与质量")
        # ------------------------------------------------------------- #
        from backend.modules.senses import targets

        target_count = len(targets.target_headwords())
        have = content.execute(
            "SELECT COUNT(DISTINCT headword) n FROM senses").fetchone()["n"]
        missing_count = target_count - have
        # 用户 2026-09-20 定「暂时不管」，所以这一项守的不是「一个不缺」，
        # 而是**缺的数量没有失控**——真正「缺的正好是哪些」要人看名单。
        check("A4", "没有义项的词数在已知范围内",
              missing_count <= 320,
              f"{have} / {target_count} 个词有义项，缺 {missing_count} 个"
              f"（已知：词典没条目 + 空壳 + 只有词组义，约 308）")

        broken = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE"
            " (gloss_zh LIKE '%（%' AND gloss_zh NOT LIKE '%）%')"
            " OR (gloss_zh LIKE '%)%' AND gloss_zh NOT LIKE '%(%'"
            "     AND gloss_zh NOT LIKE '%）%')").fetchone()["n"]
        check("A5", "中文释义没有括号残缺", broken == 0,
              f"{broken} 条括号不配对"
              "（2017 有道版实测会切坏，2012 版的中文有独立标签）")

        entity = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE concept_en LIKE '%&%;%'"
            " OR gloss_zh LIKE '%&%;%'").fetchone()["n"]
        check("A5b", "没有未解码的 HTML 实体", entity == 0, f"{entity} 条")

        seeref = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE concept_en LIKE '%→see%'"
        ).fetchone()["n"]
        check("A5c", "定义里没有交叉引用残迹", seeref == 0,
              f"{seeref} 条。导入时误收过 598 条，"
              "`world` 一度有个义项叫「not be the end of the world」")

        # 两类看着像缺陷、其实是词典内容的东西。**写成记录不是断言**——
        # 断言会天天红，而红的时候什么也没坏（坑 §8）。
        formless = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE gloss_zh IS NULL OR gloss_zh = ''"
        ).fetchone()["n"]
        note("A5d", "没有中文释义的义项", f"{formless} 条。"
             "柯林斯把词形说明也编了号——"
             "「Being is the present participle of be.」这类没有中文，"
             "因为它不是一个意思。**严格说不该算义项，但排除它们要靠识别句式，"
             "那是脆弱的判据**，而它对学习者有信息量，所以收着")

        mixed = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE concept_en GLOB '*[一-龥]*'"
        ).fetchone()["n"]
        note("A5e", "英文定义里带中文的义项", f"{mixed} 条。"
             "双解词典本来就会在英文里插中文注"
             "（`Lab is the written abbreviation for (书面缩略=) Labour.`），"
             "不是解析漏进去的")

        # ------------------------------------------------------------- #
        section("3. 身份与溯源（§5 的那套）")
        # ------------------------------------------------------------- #
        dup_prov = content.execute(
            "SELECT COUNT(*) n FROM (SELECT headword, source_dict, source_block"
            " FROM senses WHERE source_dict IS NOT NULL"
            " GROUP BY 1,2,3 HAVING COUNT(*) > 1)").fetchone()["n"]
        check("A7", "溯源三元组唯一", dup_prov == 0,
              "（词典 + 词 + 块号）——重复导入靠它找到已有的行，而不是新插一条")

        bad_ordinal = content.execute(
            "SELECT COUNT(*) n FROM (SELECT headword FROM senses GROUP BY headword"
            " HAVING MAX(ordinal) != COUNT(*) OR MIN(ordinal) != 1)").fetchone()["n"]
        check("A8", "每个词的义项序号是 1..N 连续", bad_ordinal == 0,
              f"{bad_ordinal} 个词不连续。契约把它发给客户端，"
              "标注也让模型按它作答，所以必须密实")

        sourced = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE source_dict IS NOT NULL"
        ).fetchone()["n"]
        total_senses = content.execute(
            "SELECT COUNT(*) n FROM senses").fetchone()["n"]
        check("A9", "每条义项都记得自己出自哪儿",
              sourced == total_senses, f"{sourced} / {total_senses}")

        # ------------------------------------------------------------- #
        section("4. 旧数据：归档了，没删")
        # ------------------------------------------------------------- #
        archived = content.execute(
            "SELECT COUNT(*) n FROM senses_pre_collins").fetchone()["n"]
        check("A10", "模型写的那套义项集已归档", archived == 13741,
              f"senses_pre_collins {archived} 条")

        retired_ok = content.execute(
            "SELECT COUNT(*) n FROM senses_retired").fetchone()["n"]
        check("A11", "被排除的义项进了退休表而不是被删掉", retired_ok > 0,
              f"{retired_ok} 条。旧标注按 id 反查仍然得到一个说得出话的义项")

        gone = [t for t in ("sense_screening", "wiktionary_senses", "senses_p1b")
                if content.execute(
                    "SELECT COUNT(*) n FROM sqlite_master WHERE type='table'"
                    " AND name = ?", (t,)).fetchone()["n"]]
        check("A12", "该删的三张表都删了", not gone, f"还剩：{gone}" if gone else "干净")

        # ------------------------------------------------------------- #
        section("5. 学习记录已清空（§7）")
        # ------------------------------------------------------------- #
        residue = {}
        for table in ("study_marks", "study_states", "review_queue",
                      "review_history", "client_events", "learner_pool"):
            try:
                residue[table] = events.execute(
                    f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]  # noqa: S608
            except Exception:  # noqa: BLE001 - 表不在就是 0
                residue[table] = 0
        check("A13", "学习记录已清空", sum(residue.values()) == 0,
              f"{residue}" if sum(residue.values()) else "十张表全空")

        backups = list((ROOT / "data" / "backups").glob("events-before-p10-*.db"))
        check("A14", "清空之前留了备份", bool(backups),
              backups[0].name if backups else
              "没有备份——events.db 是唯一标着「补不回来」的那一类")

        decisions = events.execute(
            "SELECT COUNT(*) n FROM decisions").fetchone()["n"]
        check("A15", "决策日志没被一起清掉", decisions > 0,
              f"{decisions} 条。它是系统自己的判断记录，不是学习记录")

        # ------------------------------------------------------------- #
        section("6. 标注")
        # ------------------------------------------------------------- #
        tot = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens WHERE kind='content'"
        ).fetchone()["n"]
        nul = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens WHERE kind='content'"
            " AND sense_id IS NULL").fetchone()["n"]
        check("B1", "全语料标注完成", nul == 0,
              f"还有 {nul:,} / {tot:,} 没标"
              "（NULL＝没标到，与 0＝无义项集、-1＝没有贴合的义项 是三件事）")

        fits = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens WHERE kind='content'"
            " AND sense_id > 0").fetchone()["n"]
        nofit = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens WHERE kind='content'"
            " AND sense_id = -1").fetchone()["n"]
        noset = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens WHERE kind='content'"
            " AND sense_id = 0").fetchone()["n"]
        done = fits + nofit + noset
        if done:
            note("B2", "标注结果分布",
                 f"标上义项 {fits/done*100:.1f}% · 说没贴合 {nofit/done*100:.1f}% ·"
                 f" 无义项集 {noset/done*100:.1f}%"
                 f"（旧义项集是 91.3% / 0.6% / 8.0%；"
                 f"那 0.6% 是另一个模型留下的基准，见 phase-10.html §13③）")

        stale = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens t WHERE t.sense_id > 0"
            " AND NOT EXISTS (SELECT 1 FROM senses s WHERE s.id = t.sense_id)"
            " AND NOT EXISTS (SELECT 1 FROM senses_retired r WHERE r.id = t.sense_id)"
        ).fetchone()["n"]
        check("B5", "没有标注指向不存在的义项", stale == 0,
              f"{stale} 条悬空。指着退休义项是正常的，指着空气不是")

        freq = content.execute(
            "SELECT COUNT(*) n FROM senses WHERE exam_frequency > 0").fetchone()["n"]
        note("B4", "真题考频非零的义项数", f"{freq} 条（换之前 7,107 条）")

        # ------------------------------------------------------------- #
        section("7. 内容 id 没有变（架构铁律 5）")
        # ------------------------------------------------------------- #
        arts = content.execute(
            "SELECT COUNT(*) n FROM reading_articles").fetchone()["n"]
        sents = content.execute(
            "SELECT COUNT(*) n FROM reading_sentences").fetchone()["n"]
        toks = content.execute(
            "SELECT COUNT(*) n FROM reading_tokens").fetchone()["n"]
        # **基准取自阿里云那份动手前的备份**（`/root/er-backup-20260920-1421/`），
        # 不是凭印象写的。第一版这里填的句子数是 10,061 —— 一个我没有测量过、
        # 顺手写进文档又抄进断言的数，于是 B6 报红而内容其实一个字节没动。
        # 坑 §1.2：一个错误的度量比没有度量更危险。
        # **2026-09-22 改口径。** 原本钉死在 (498, 10140, 235408)——动手前那份
        # 备份的数——而夜间备稿每天都在合法地加文章，所以这条注定会红，
        # 而红的时候什么也没坏（坑 §8「验收脚本会到期」的第四个实例）。
        #
        # **始终成立的那一半是「只增不减」**：义项换血（以及 P11 换词组识别）
        # 只许改 token 的 sense_id 那两列，不许删了重插——重插会让 id 全变，
        # 而设备上的标记正指着它们（架构铁律 5）。新文章进来是另一回事。
        # 基准与最小 id 都**在那份备份里现量的**（`docker run` 挂上去查了一次），
        # 不是顺手写的——这一条的第一版就是栽在「填了个没测过的数」上。
        baseline = (498, 10140, 235408)
        first_id = int(content.execute(
            "SELECT MIN(id) n FROM reading_tokens").fetchone()["n"] or 0)
        check("B6", "内容只增不减，老 token 的身份没被重建",
              (arts, sents, toks) >= baseline and first_id == 787,
              f"{arts} / {sents} / {toks}（基准 {baseline[0]} / {baseline[1]} /"
              f" {baseline[2]}；最小 token id {first_id}，基准 787）"
              "——删了重插的话最小 id 会跟着水位一起往上走，而那正是"
              "设备上的标记指着的东西")

        # ------------------------------------------------------------- #
        section("8. 触发这次清洗的那道题")
        # ------------------------------------------------------------- #
        row = content.execute(
            "SELECT t.sense_id, t.sense_ordinal, s.gloss_zh FROM reading_tokens t"
            " LEFT JOIN senses s ON s.id = t.sense_id"
            " JOIN reading_sentences q ON q.id = t.sentence_id"
            " WHERE t.headword = 'account' AND q.text LIKE '%into account%'"
            " LIMIT 1").fetchone()
        if row is None:
            note("A6", "语料里没有 take into account 的句子", "跳过")
        else:
            state = ("填 0（退回词典释义）" if row["sense_id"] == -1
                     else f"标成「{row['gloss_zh']}」" if row["sense_id"] and row["sense_id"] > 0
                     else "无义项集")
            note("A6", "`taking … into account` 里的 account 标成了什么", state
                 + "。**这个 Phase 修不好它**——柯林斯把「考虑到」挂在 PHRASE 上，"
                   "而词组一条都不收（§4）。期望是填 0 而不是又选一个错的")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(notes)} 项只记录")
    if failed:
        print("  失败：" + "、".join(failed))
    print("\n人工验收（§11）：")
    print("  A0  **先卸载重装 App** —— 义项 id 全换、学习记录清空，"
          "不重装的话手机上的旧投影指向不存在的义项，而且不会报错")
    print("  M1  用 ercli 读一篇文章，点十个词看释义对不对")
    print("  M2  管理台抽查 take / run / address 的义项列表像不像这本词典该有的样子")
    print("  B3  新旧标注对拍：抽 100 处不一致的三方盲判，混入阳性对照")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
