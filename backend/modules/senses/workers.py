"""Batch jobs that build and audit sense sets.

The prompt carries the one rule the whole design rests on: **senses divide on
English concepts, never on differences that exist only in Chinese.** `present`
covers 提出 / 介绍 / 呈现 because in English it is one thing — showing or giving
something to someone — and Chinese needs three verbs only because of collocation.
`address` splits into 地址 and 着手解决 because those are two things in English
too. Part of speech is not a reason to split: the noun and the verb ``address``
are the same concept.

Stated as a test rather than as a principle, because a model executes a test more
consistently than a principle: *after learning one of these meanings, would the
other be understood on sight, or misunderstood?* Understood → one sense.
Misunderstood → two.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.core.db import get_connection
from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed, json_object
from backend.modules.llm.providers import Provider
from backend.modules.senses import repository
from backend.modules.vocabulary import repository as dictionary

log = get_logger("senses.jobs")

BUILD_BATCH = 8
EXAMPLE_BATCH = 10

SYSTEM = (
    "你是一位英语词汇教学的编者，为中国的四六级和考研考生整理义项。"
    "你的读者语法基础扎实，词汇量在高中水平，正在通过阅读扩充词汇。"
)

GRANULARITY = """\
划分义项的唯一标准是英文里是不是同一个概念，不是中文翻译有没有区别。

一句话判据：学会了其中一个意思之后，在阅读中遇到另一个用法，
是能自然理解，还是会理解错？能自然理解就合并，会理解错就拆开。

合并的例子：present 的「提出、介绍、呈现」——英文都是 to show or give
something to someone，中文因为搭配习惯才译成三个词，合成一项。
拆开的例子：address 的「地址」和「着手解决」——在英文里也是两回事，各算一项。

不要因为词性不同就拆：address 作名词「地址」和作动词「写地址」是同一个概念。
绝大多数词只有 1 到 3 个义项，超过 5 个几乎一定是拆得太细了。"""

TOPICS_LINE = "、".join(repository.TOPICS)


def _plan_build(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    # An explicit word list is the top-up path. The coarse filter judges by the
    # shape of ECDICT's glosses, and ECDICT's glosses are uneven, so some words
    # with real sense splits will have been screened out. Rebuilding one costs a
    # fraction of a cent; having no way to do it would mean those words stay
    # wrong forever.
    explicit = params.get("words")
    if explicit:
        words = [str(word).strip().lower() for word in explicit if str(word).strip()]
    else:
        words = repository.pending_words(int(params.get("limit", 5000)))
    size = max(1, int(params.get("batch", BUILD_BATCH)))
    batches = []
    for start in range(0, len(words), size):
        chunk = words[start:start + size]
        batches.append((f"{chunk[0]}…{chunk[-1]}", {"words": chunk}))
    return batches


def _run_build(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    # Ask only for what is still missing. An item is retried with the batch it
    # was planned with, and a retry exists because some of that batch came back
    # and some did not — re-asking for the words already built would pay again
    # for work already done, and on a batch missing one word out of eight that
    # is seven eighths waste.
    words = [w for w in payload["words"] if not repository.senses_of(w)]
    if not words:
        return jobs.ItemOutcome(result="", tokens_in=0, tokens_out=0)

    listing = []
    for index, word in enumerate(words, start=1):
        entry = dictionary.lookup(word)
        gloss = (entry["translation"] or "").replace("\n", " / ") if entry else ""
        listing.append(f"{index}. {word}: {gloss[:200]}")

    prompt = (
        f"{GRANULARITY}\n\n"
        "下面每行是一个词和它的词典释义（词典的划分仅供参考，不要照抄）。\n\n"
        + "\n".join(listing)
        + "\n\n为每个词给出义项集。每个义项包含：\n"
        "- concept_en：英文概念定义，一句话，**只用最常见的 2000 个英语单词**，"
        "读定义的人不应该在定义里撞上新生词；\n"
        "- gloss_zh：2 到 3 个中文说法，是这一个概念在不同语境下的投影；\n"
        "- pos：主要词性（noun / verb / adj / adv 等），仅供参考，不作为拆分依据；\n"
        f"- topic：主题标签，从这些里选一个：{TOPICS_LINE}。与主题无关的词填「通用」。\n\n"
        "按常用度排序，最常用的排第一。\n"
        '只返回 JSON 对象，键是行首的编号（字符串），值是义项数组。例如某一行是 '
        '"3. address: …" 时：\n'
        '{"3": [{"concept_en": "the details of where someone lives or works",'
        ' "gloss_zh": ["地址", "住址"], "pos": "noun", "topic": "日常生活"},'
        ' {"concept_en": "to start dealing with a problem", "gloss_zh": ["着手解决", "处理"],'
        ' "pos": "verb", "topic": "社会"}]}'
    )

    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=3000,
        temperature=0.3,
        json_mode=provider.kind == "openai",
    )

    parsed = json_object(completion.text)
    stored = 0
    for index, word in enumerate(words, start=1):
        senses = parsed.get(str(index))
        if not isinstance(senses, list) or not senses:
            continue
        cleaned = [s for s in (_clean_sense(item) for item in senses) if s]
        if cleaned:
            repository.store_senses(word, cleaned, model=provider.model)
            stored += 1

    if stored < len(words):
        # A partial reply is a failure, not a success with a footnote. Accepting
        # one — the original rule was "at least one word came back" — is how a
        # job reported 368/368 with nothing wrong while four hundred words had
        # no senses at all: models skip a word or two per batch, and nobody was
        # counting. Failing here makes the item retry, and the retry asks only
        # for the words still missing.
        raise ItemFailed(
            f"这一批 {len(words)} 个词只返回了 {stored} 个，"
            f"缺 {[w for i, w in enumerate(words, 1) if str(i) not in parsed][:6]}",
            raw=completion.text,
        )

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def _clean_sense(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    concept = str(item.get("concept_en", "")).strip()
    glosses = item.get("gloss_zh")
    if isinstance(glosses, str):
        glosses = [part.strip() for part in re.split(r"[,，;；/]", glosses) if part.strip()]
    if not concept or not isinstance(glosses, list) or not glosses:
        return None
    topic = str(item.get("topic", "")).strip()
    return {
        "concept_en": concept,
        "gloss_zh": [str(g).strip() for g in glosses if str(g).strip()][:3],
        "pos": str(item.get("pos", "")).strip() or None,
        "topic": topic if topic in repository.TOPICS else "通用",
    }


# --------------------------------------------------------------------------- #
# Examples, generated only for senses that are about to be reviewed
# --------------------------------------------------------------------------- #


def _plan_examples(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Senses named in ``params['senses']``, or every sense with no example yet.

    In normal use the caller passes a short list — the senses due for review
    tonight. The unbounded form exists for filling a gap deliberately, and is
    exactly what the design warns against doing by default: most of these senses
    will never reach a review card.
    """
    from backend.core.db import get_connection

    wanted = params.get("senses")
    if wanted:
        rows = get_connection("content").execute(
            "SELECT id, headword, concept_en FROM senses WHERE id IN"
            f" ({','.join('?' * len(wanted))})",
            tuple(wanted),
        ).fetchall()
    else:
        rows = get_connection("content").execute(
            "SELECT s.id, s.headword, s.concept_en FROM senses s"
            " LEFT JOIN sense_examples e ON e.sense_id = s.id"
            " WHERE e.id IS NULL ORDER BY s.headword LIMIT ?",
            (int(params.get("limit", 200)),),
        ).fetchall()

    items = [dict(row) for row in rows]
    size = max(1, int(params.get("batch", EXAMPLE_BATCH)))
    batches = []
    for start in range(0, len(items), size):
        chunk = items[start:start + size]
        batches.append((f"{chunk[0]['headword']}…{chunk[-1]['headword']}", {"senses": chunk}))
    return batches


def _run_examples(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    senses = payload["senses"]
    listing = "\n".join(
        f"{item['id']}. {item['headword']} — {item['concept_en']}" for item in senses
    )
    prompt = (
        "下面每行是一个义项。请为每个义项写 2 个典型例句。\n"
        "要求：句子自然、场景具体，长度 10-20 词；用词不要超出四级范围；"
        "这个义项的用法要在句子里一眼可辨。\n\n"
        f"{listing}\n\n"
        '只返回 JSON 对象，键是行首的编号（字符串），值是数组，'
        '每项形如 {"en": "英文例句", "zh": "中文翻译"}。'
    )

    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=2500,
        temperature=0.5,
        json_mode=provider.kind == "openai",
    )

    parsed = json_object(completion.text)
    for item in senses:
        rows = parsed.get(str(item["id"]))
        if not isinstance(rows, list):
            continue
        cleaned = [
            {"text_en": str(r["en"]).strip(), "gloss_zh": str(r.get("zh", "")).strip()}
            for r in rows
            if isinstance(r, dict) and str(r.get("en", "")).strip()
        ]
        if cleaned:
            repository.add_examples(item["id"], cleaned[:3], source=provider.model)

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


# --------------------------------------------------------------------------- #
# P1c: rebuild from the Wiktionary checklist
# --------------------------------------------------------------------------- #

REBUILD_BATCH = 4

REGISTER_RULE = """\
先筛，再合并。

筛的标准**只有语域**，不是「常不常用」，也不是「值不值得学」：

保留 —— 一般人读报纸、科普、社会评论时会遇到的意思，**包括不常见的那些**。
删除 —— 只在某个专业领域内部使用的意思。判断方法：这个意思要不要先懂那个专业
才能理解？要，就删。例如：
  ✗ address 的「网络地址／IP 地址」——计算机专业用法
  ✗ present 的「举枪敬礼」——军事口令
  ✗ run 的「板球得分」「养鸡场围栏」——体育与农业行话
  ✗ bank 的「内存区块」「飞机侧倾」——计算机与航空行话
  ✓ bank 的「河岸」——虽然词典标了 geography，但这是人人都懂的日常意思，保留

**不要因为一个意思「不是这个词最常见的意思」就删掉它。**
四六级阅读最爱考的恰恰是常见词那个不显眼的义项——address 的「着手解决」比「地址」少见得多，
但它才是真题里出现的那个。删错它，学生就永远学不到。

合并按英文概念，不按中文差异，不按词性，不按正式程度，也不按程度深浅：
  present 的「赠送／展示／提出／介绍」在英文里是同一件事——
    把东西拿给别人看或交给别人，**合成一项，不要拆成三四项**。
  fast 的「快的」（形容词）和「快速地」（副词）是同一个概念，合成一项。
  fast 的「禁食」（动词）和「禁食期」（名词）是同一个概念，合成一项。
  fast 的「牢固的」「不褪色的」「熟睡的」都是「牢牢固定住」的引申，合成一项。
  address 的「向听众讲话」和「向国王正式请愿」只是场合不同，合成一项。
  address 的「地址」和「着手解决」在英文里是两回事，各算一项。

**只能用清单上的意思，不许自己添加清单上没有的义项。**
每一项的 covers 都必须至少包含一个编号；covers 为空的项一律不要输出。"""


def _plan_rebuild(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every target word — there is no screening step any more.

    The screen existed to save money and cost more than it saved: it judged
    English polysemy by counting commas in a Chinese gloss, wrote off 13% of
    what it did build as single-sense, and skipped `bank`, `come` and `do` as
    "simple". Building everything removes the threshold, the blind spot and the
    code at once, for about twice the tokens.
    """
    from backend.modules.senses import screening

    explicit = params.get("words")
    if explicit:
        words = [str(w).strip().lower() for w in explicit if str(w).strip()]
    else:
        words = [w for w, _ in screening.target_words()]
        built = {r["headword"] for r in get_connection("content").execute(
            "SELECT DISTINCT headword FROM senses")}
        words = [w for w in words if w not in built]
        if params.get("limit"):
            words = words[: int(params["limit"])]

    size = max(1, int(params.get("batch", REBUILD_BATCH)))
    return [
        (f"{chunk[0]}…{chunk[-1]}", {"words": chunk})
        for chunk in (words[i:i + size] for i in range(0, len(words), size))
    ]


def _run_rebuild(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    from backend.modules.senses import inventory

    words = [w for w in payload["words"] if not repository.senses_of(w)]
    if not words:
        return jobs.ItemOutcome()

    blocks: list[str] = []
    numbering: dict[str, dict[int, int]] = {}
    for word in words:
        cands = inventory.candidates(word)
        if not cands:
            continue
        entry = dictionary.lookup(word)
        chinese = (entry["translation"] or "").replace("\n", " / ")[:160] if entry else ""
        numbering[word] = {i: c.id for i, c in enumerate(cands, start=1)}
        lines = "\n".join(c.as_line(i) for i, c in enumerate(cands, start=1))
        # An explicit target does far more work than a ratio rule. Told only to
        # "merge hard", the model anchors on the granularity of the list it was
        # handed and returns roughly one output per input; told "aim for about
        # four", it actually merges.
        target = max(1, round(len(cands) * 0.22))
        blocks.append(
            f"### {word}（清单 {len(cands)} 条，合并后应约 "
            f"{max(1, target - 1)}-{target + 1} 项）\n中文参考：{chinese}\n{lines}"
        )

    if not blocks:
        raise ItemFailed("这一批词在词典清单里一条义项都没有", raw="")

    prompt = (
        f"{REGISTER_RULE}\n\n"
        "下面每个词后面是它在英语词典里的义项清单（编号是给你引用的）。\n\n"
        + "\n\n".join(blocks)
        + "\n\n为每个词给出合并后的义项集，每项包含：\n"
        "- concept_en：英文概念定义，一句话，**只用最常见的 2000 个英语单词**；\n"
        "- gloss_zh：2 到 3 个中文说法；\n"
        "- pos：主要词性；\n"
        f"- topic：从这些里选一个：{TOPICS_LINE}；\n"
        "- covers：这一项覆盖了上面哪几个编号（数组）。**每个保留的编号都要出现在某一项里**，"
        "被你按语域删掉的编号不要写；covers 不能为空。\n\n"
        "按常用度排序，最常用的排第一。\n"
        '只返回 JSON 对象，键是词本身，值是义项数组。'
    )

    completion = client.complete(
        provider,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=4000,
        # Lower than the P1b build: this is a classification and merging task,
        # and run-to-run variance in where the register line falls shows up
        # directly as inconsistent sense counts.
        temperature=0.15,
        json_mode=provider.kind == "openai",
    )

    parsed = json_object(completion.text)
    stored = 0
    for word in words:
        senses = parsed.get(word)
        if not isinstance(senses, list) or not senses:
            continue
        cleaned = []
        for item in senses:
            sense = _clean_sense(item)
            if not sense:
                continue
            # Map the prompt's local numbers back to real row ids, so coverage
            # survives the batch it was produced in.
            local = item.get("covers") if isinstance(item, dict) else None
            ids = [
                numbering.get(word, {}).get(int(n))
                for n in (local or []) if str(n).strip().isdigit()
            ]
            sense["covers"] = json.dumps([i for i in ids if i])
            sense["source"] = "wiktionary"
            cleaned.append(sense)
        if cleaned:
            repository.store_senses(word, cleaned, model=provider.model)
            stored += 1

    if stored < len(words):
        raise ItemFailed(
            f"这一批 {len(words)} 个词只返回了 {stored} 个",
            raw=completion.text,
        )

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def register() -> None:
    jobs.register_worker(jobs.Worker(
        kind="sense_rebuild",
        title="重建义项集（按词典清单）",
        description="用 Wiktionary 的义项清单做输入，模型只做语域筛选和概念合并",
        plan=_plan_rebuild,
        run_item=_run_rebuild,
    ))
    jobs.register_worker(jobs.Worker(
        kind="sense_build",
        title="建义项集",
        description="为粗筛留下的词生成义项集，8 个词一批",
        plan=_plan_build,
        run_item=_run_build,
    ))
    jobs.register_worker(jobs.Worker(
        kind="sense_examples",
        title="补例句",
        description="为指定义项生成例句。默认只补没有例句的，用 limit 控制数量",
        plan=_plan_examples,
        run_item=_run_examples,
    ))
