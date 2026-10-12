"""One definition per dictionary boundary / 每个明确词典小节补一条英文定义."""
from __future__ import annotations

import json
from typing import Any

from backend.modules.llm import client, jobs
from backend.modules.llm.parsing import ItemFailed, json_object
from backend.modules.senses import repository
from backend.modules.wordfamily import repository as families
from backend.modules.wordfamily.derive import SUFFIX_RULES

KIND = "sense_derived_definition"
PROMPT_VERSION = "derived-definition-1"


def formation(payload: dict[str, Any]) -> dict[str, Any] | None:
    if not payload.get("formation_unambiguous", True):
        return None
    member, root = payload["headword"], payload["parent"]
    affix = None
    change = ""
    for suffix, _, _ in SUFFIX_RULES:
        stems = {root}
        if root.endswith("y"):
            stems.add(root[:-1] + "i")
        if root.endswith("e") and suffix[0] in "aeiou":
            stems.add(root[:-1])
        if root and root[-1] not in "aeiouy":
            stems.add(root + root[-1])
        if member.endswith(suffix) and member[:-len(suffix)] in stems:
            affix = "-" + suffix
            if member != root + suffix:
                change = "（词干拼写随派生改变；stem spelling changes）"
            break
    if affix:
        explanation = f"{member} ← {root} + {affix}{change}（派生后缀；derivational suffix）。"
    else:
        explanation = f"{member} ← {root}（词典明确的派生关系；dictionary-attached derivative，不作逐字拆分）。"
    explanation += "柯林斯明确挂在对应母义项下；具体含义见本词独立义项，不等同于母词的所有含义。"
    return {"member": member, "root": root, "affix": affix,
            "breakdown_zh": explanation, "grade": "B"}


def plan(params: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(p["source_key"], p) for p in params.get("candidates", [])
            if not repository.derived_sense_for(p)]


def run_item(provider, payload: dict[str, Any], params: dict[str, Any]) -> jobs.ItemOutcome:
    existing = repository.derived_sense_for(payload)
    if existing:
        _store_formation(payload)
        return jobs.ItemOutcome(result=existing["definition_response"])
    fields = payload["runon_2017"]
    prompt = (
        "词典已经确定下面这一个派生词义项的边界。只为这一项补一句独立英文定义，"
        "不要分义项，不要改词性、中文或例句，不要加入母词其他意思。"
        "用常见英文解释派生词本身，而不是照抄形容词定义。证据不足时返回 error，不能猜。\n"
        + json.dumps({"word": payload["headword"], "pos": fields["pos"],
                      "gloss_zh": fields["gloss_zh"], "parent": payload["parent"],
                      "parent_definition": payload["parent_2012"]["concept_en"],
                      "parent_gloss": payload["parent_2012"]["gloss_zh"],
                      "examples": payload["runon_2012"]["examples"]}, ensure_ascii=False)
        + '\n只返回 JSON：{"concept_en":"one English definition"} 或 {"error":"原因"}。'
    )
    completion = client.complete(
        provider, [{"role": "system", "content": "You write one evidence-bound English dictionary definition."},
                   {"role": "user", "content": prompt}],
        max_tokens=600, temperature=0.2, json_mode=provider.kind == "openai",
    )
    parsed = json_object(completion.text)
    concept = parsed.get("concept_en")
    if (set(parsed) != {"concept_en"} or not isinstance(concept, str)
            or not concept.strip() or any("\u3400" <= c <= "\u9fff" for c in concept)):
        raise ItemFailed("未返回唯一、完整的英文定义", raw=completion.text)
    repository.store_derived_sense(payload, concept, provider=provider.id, model=provider.model,
                                  prompt=f"{PROMPT_VERSION}\n{prompt}", response=completion.text)
    _store_formation(payload)
    return jobs.ItemOutcome(result=completion.text, tokens_in=completion.tokens_in,
                            tokens_out=completion.tokens_out)


def _store_formation(payload: dict[str, Any]) -> None:
    relation = formation(payload)
    if relation:
        families.store_dictionary_family(**relation)


def register() -> None:
    jobs.register_worker(jobs.Worker(
        kind=KIND, title="补词典派生义项英文定义", plan=plan, run_item=run_item,
        description="2012明确挂载决定边界，2017提供词性中文，模型仅补英文定义；逐项落库可续跑。",
        default_provider=lambda: jobs.configured_provider("sense_build"),
    ))
