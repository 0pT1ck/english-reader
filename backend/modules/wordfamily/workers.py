"""Batch jobs this module contributes: affix glosses, and grading review.

Both are registered with the job runner rather than written as scripts, so they
resume after an interruption and stop at a spend cap like everything else.

The division of labour with :mod:`.derive` is deliberate. The rules find *that*
one word comes from another — cheap, deterministic, and safe because the tests
are structural. What they cannot judge is *how far the meaning has travelled*,
which is exactly what the A/B/C grade encodes and exactly what a model is good
at. So the model never searches for derivations; it only rules on the ones the
rules already found, and writes the Chinese breakdown the reader will see.
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import get_logger
from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed, json_object
from backend.modules.llm.providers import Provider
from backend.modules.wordfamily import repository

log = get_logger("wordfamily.jobs")

# How many entries go into one call. Overridable per job via `params.batch`,
# because the right number depends on the model: a reasoning model spends most
# of its output budget thinking, so a batch that a fast model handles in one go
# can time out the gateway in front of a slow one.
AFFIX_BATCH = 25
REVIEW_BATCH = 20

AFFIX_SYSTEM = (
    "你是一位英语词汇教学的编者。你的读者是中国的四六级考生，"
    "他们在阅读中遇到生词时会看到词缀的中文说明。"
    "说明要短、准确、直接说这个词缀加上去之后表达什么，不要举例，不要客套。"
)

REVIEW_SYSTEM = (
    "你是一位英语词汇教学的编者，正在判断派生词与词根之间的语义距离。"
    "判断标准只有一条：一个已经认识词根的中国学生，"
    "在阅读中遇到这个派生词时会发生什么。"
)

GRADE_RULES = """\
A —— 完全透明。认识词根就一定认识它，只是词性或语法形式变了。
     例：careful → carefully，happy → happiness，used → unused。
B —— 可以推出来，但意思被具体化了，仍需要确认一次。
     例：nation → national，nation → nationality，depend → dependable。
C —— 语义已经漂移，词根帮不上忙，讲构词反而误导。
     例：part → depart，cover → recover，pulse → impulse。"""


# --------------------------------------------------------------------------- #
# Affix glosses
# --------------------------------------------------------------------------- #


def _plan_affixes(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    pending = repository.affixes_without_gloss()
    size = max(1, int(params.get("batch", AFFIX_BATCH)))
    batches = []
    for start in range(0, len(pending), size):
        chunk = pending[start:start + size]
        key = f"{chunk[0]['affix']}…{chunk[-1]['affix']}"
        batches.append((key, {"affixes": chunk}))
    return batches


def _run_affixes(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    entries = payload["affixes"]
    # Keyed by line number, not by the affix itself. Asking a model to echo a
    # fiddly identifier back is a reliable way to lose rows — and it is never
    # necessary, since the caller already knows which line is which.
    listing = "\n".join(
        f"{index}. {item['affix']} ({item['kind']}): {item['meaning_en'] or '?'}"
        for index, item in enumerate(entries, start=1)
    )
    prompt = (
        "下面每行是一个英语词缀和它的英文释义。"
        "请给每个词缀写一句中文说明，不超过 15 个字。\n\n"
        f"{listing}\n\n"
        '只返回 JSON 对象，键是行首的编号（字符串），值是中文说明。'
        '例如 {"1": "名词后缀：性质、状态", "2": "前缀：表示否定"}。'
    )

    completion = client.complete(
        provider,
        [{"role": "system", "content": AFFIX_SYSTEM},
         {"role": "user", "content": prompt}],
        max_tokens=1500,
        temperature=0.2,
        json_mode=provider.kind == "openai",
    )

    glosses = json_object(completion.text)
    stored = 0
    for index, item in enumerate(entries, start=1):
        gloss = glosses.get(str(index))
        if isinstance(gloss, str) and gloss.strip():
            repository.set_affix_gloss(item["affix"], item["kind"], gloss.strip())
            stored += 1

    if stored == 0:
        raise ItemFailed(
            f"这一批 {len(entries)} 个词缀一个也没对上（键名应该是 1…{len(entries)}）",
            raw=completion.text,
        )

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


# --------------------------------------------------------------------------- #
# Grading review
# --------------------------------------------------------------------------- #


def _plan_review(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    pending = repository.ungraded_for_review(int(params.get("limit", 5000)))
    size = max(1, int(params.get("batch", REVIEW_BATCH)))
    batches = []
    for start in range(0, len(pending), size):
        chunk = pending[start:start + size]
        key = f"{chunk[0]['member']}…{chunk[-1]['member']}"
        batches.append((key, {"families": chunk}))
    return batches


def _run_review(provider: Provider, payload: dict[str, Any], params: dict[str, Any]):
    families = payload["families"]
    listing = "\n".join(
        f"{index}. {item['member']} = {item['root']} + {item['affix']}"
        for index, item in enumerate(families, start=1)
    )
    prompt = (
        f"{GRADE_RULES}\n\n"
        "请为下面每一组判定等级，并写一句中文构词说明（不超过 25 个字，"
        "格式如「nation 国家 + -ity 名词后缀：性质」）。"
        "如果这一组的构词关系本身就是错的（词根其实不是这个词的来源），等级填 X。\n\n"
        f"{listing}\n\n"
        '只返回 JSON 对象，键是行首的编号（字符串），'
        '值是 {"grade": "A|B|C|X", "zh": "中文构词说明"}。'
    )

    completion = client.complete(
        provider,
        [{"role": "system", "content": REVIEW_SYSTEM},
         {"role": "user", "content": prompt}],
        max_tokens=2500,
        temperature=0.2,
        json_mode=provider.kind == "openai",
    )

    verdicts = json_object(completion.text)
    graded = 0
    for index, item in enumerate(families, start=1):
        verdict = verdicts.get(str(index))
        if not isinstance(verdict, dict):
            continue
        grade = str(verdict.get("grade", "")).strip().upper()[:1]
        if grade not in ("A", "B", "C", "X"):
            continue
        # X means the rules found a relationship that is not real. Demoting it
        # to C is the safe response: the word is then treated as an ordinary new
        # word and no misleading breakdown is ever shown.
        repository.set_grade(
            item["member"],
            item["root"],
            "C" if grade == "X" else grade,
            breakdown_zh=(str(verdict.get("zh", "")).strip() or None) if grade != "X" else None,
        )
        graded += 1

    if graded == 0:
        raise ItemFailed(
            f"这一批 {len(families)} 组一个也没对上（键名应该是 1…{len(families)}）",
            raw=completion.text,
        )

    return jobs.ItemOutcome(
        result=completion.text,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
    )


def register() -> None:
    jobs.register_worker(jobs.Worker(
        kind="affix_gloss",
        title="翻译词缀说明",
        description="给 188 个前后缀写中文说明，一次性，成本可忽略",
        plan=_plan_affixes,
        run_item=_run_affixes,
    ))
    jobs.register_worker(jobs.Worker(
        kind="family_review",
        title="复核词族分级",
        description="规则找出的派生关系交给模型判定 A/B/C，并写中文构词说明",
        plan=_plan_review,
        run_item=_run_review,
    ))
