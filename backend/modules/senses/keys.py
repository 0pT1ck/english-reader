"""义项的稳定键。

稳定键 stable key / 代理键 surrogate key / churn 无谓的变动

**为什么要有它。** 义项 id 是自增代理键，而 ``store_senses`` 从前是删了重插——
于是**任何**一次重建（补一个词的义项、重跑一次生成器、做一次迁移）都会让 id 全变，
**而这跟语义变没变毫无关系**。九万三千条语境标注（实测 93,063）每次都被推到
悬崖边上，靠一段注释和一条跨 Phase 不变量拦着。

**它解决的是哪一半。** 「为什么换义项集费力」分两半：
新义项集对词的切分和现在不一样，「旧义项 3 对应新义项 2」是个判断题——**那一半
躲不掉**，稳定键不能让它变便宜。而「id 因为无关的原因全变」是架构造成的，
这一半可以消掉。P9 §8 做的就是这一半。

**键的来源是 ``concept_en``**，也就是「这是哪个意思」的那句英文描述。
归一化之后取哈希，所以：

* 重跑生成器、补一个词、做迁移 → 键不变，标注自动存活；
* **把同一个意思换个说法写 → 键会变。** 这是这个设计的边界，不是缺陷：
  键认的是那句话，不是那个意思，而「两句话说的是不是同一个意思」正是那个
  躲不掉的判断题。这种情况走 ``sense_key_map``。
"""

from __future__ import annotations

import hashlib
import re

#: 键里那段哈希取多少位。12 个十六进制位 ＝ 48 bit，在一万多条义项上
#: 撞一次的概率约 1e-10；而撞了也不会静默——``store_senses`` 会按序号加后缀，
#: 而且那通常意味着数据里本来就有两条一样的义项（实测 `underground` 就是一例）。
DIGEST_LENGTH = 12


def normalize(concept: str) -> str:
    """把一句英文概念描述归一成比较用的形式。

    只做三件事：小写、非字母数字换成空格、空白折叠。**不做词干化、不去停用词**——
    那些会把「两句不同的话」判成同一个键，而键判错的后果是两个义项合成一个，
    连带它们的标注也合到一起。宁可过于敏感。
    """
    text = (concept or "").strip().lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def sense_key(headword: str, concept: str, *, discriminator: int = 1) -> str:
    """一个义项的稳定键。

    形如 ``account:3f0a1c9d2b7e``。**带上词本身**，这样键在库里全局唯一，
    而不只是在一个词的范围内唯一——跨词比较键因此是安全的。

    ``discriminator`` 只在同一个词里有两条归一化之后完全一样的概念时才 > 1，
    形如 ``…:2``。**那通常是数据里本来就重复了**，键把它照了出来。
    """
    digest = hashlib.sha256(normalize(concept).encode("utf-8")).hexdigest()
    base = f"{(headword or '').strip().lower()}:{digest[:DIGEST_LENGTH]}"
    return base if discriminator <= 1 else f"{base}:{discriminator}"


def assign_keys(headword: str, concepts: list[str]) -> list[str]:
    """一个词的一组概念，各自的键，按顺序返回。

    撞键的按出现顺序加后缀，所以**同一份输入总得到同一组键**——
    这个函数是纯的，而键的稳定性正是靠它是纯的。
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for concept in concepts:
        base = sense_key(headword, concept)
        seen[base] = seen.get(base, 0) + 1
        keys.append(sense_key(headword, concept, discriminator=seen[base]))
    return keys
