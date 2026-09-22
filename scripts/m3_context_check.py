"""M3：拿归档的旧判定当对照组，抽验语境判定准不准 — P11 §16。

用法::

    uv run python scripts/m3_context_check.py            # 一致率 + 抽 12 处分歧
    uv run python scripts/m3_context_check.py --count 20

**对照组是 P2 那 3,458 条**：同一批语料上，另一个模型、另一套问法回答过
「这一处算不算一个词组」。P11 不再单独问这个问题——它是「这一处是哪个意思」
的副产品（填 -1 就是「都不是」）。所以两边回答的是同一件事，可以对拍。

**这不是一个判对错的脚本，它只找分歧。** 一致率高只说明两套办法像，
不说明谁对；真正有用的是分歧那些——**看的时候要连原句一起看**，
因为这两套办法都可能错，而句子不会。

**别把一致率当成准确率**（坑 §4.3c 的形状）：旧判定自己就有噪音，
P2 §18 实测同一篇问三次的稳定率只有 38%。
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.db import get_connection  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="M3：语境判定与旧判定对拍")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    conn = get_connection("content")
    # 按「同一篇文章、同一个起始位置、同一个词组串」对上号——
    # 那三样唯一确定语料里的一处，而行号两边不通用（出现位置表重建过）。
    rows = conn.execute(
        """
        SELECT n.phrase, n.article_id, n.start_seq, n.surface,
               n.sense_id AS new_sense, o.verdict AS old_verdict,
               s.text AS sentence, g.gloss_zh
          FROM reading_phrases n
          JOIN phrase_verdicts_legacy o
            ON o.article_id = n.article_id AND o.start_seq = n.start_seq
           AND o.phrase = n.phrase AND o.verdict IS NOT NULL
          JOIN reading_sentences s ON s.id = n.sentence_id
          LEFT JOIN phrase_senses g ON g.id = n.sense_id
         WHERE n.sense_id IS NOT NULL
        """
    ).fetchall()

    if not rows:
        print("两边对不上号——旧判定里的词组串和新清单没有交集，这一项跳过。")
        return 0

    agree = [r for r in rows
             if (int(r["new_sense"]) > 0) == (int(r["old_verdict"]) == 1)]
    new_yes = [r for r in rows
               if int(r["new_sense"]) > 0 and int(r["old_verdict"]) == 0]
    new_no = [r for r in rows
              if int(r["new_sense"]) == -1 and int(r["old_verdict"]) == 1]

    print("=" * 74)
    print(f"M3：两套办法都判过的 {len(rows)} 处")
    print("=" * 74)
    print(f"  一致 {len(agree)}（{len(agree) / len(rows) * 100:.1f}%）")
    print(f"  新说是词组、旧说不是：{len(new_yes)} 处")
    print(f"  新说不是词组、旧说是：{len(new_no)} 处")
    print("\n**一致率不是准确率**：旧判定自己就不稳（P2 §18：同一篇问三次，"
          "稳定率 38%）。下面这些分歧要连句子一起看，判断哪一边对。\n")

    random.seed(args.seed)
    for label, group in (("新说「是词组」／旧说「不是」", new_yes),
                         ("新说「不是词组」／旧说「是」", new_no)):
        if not group:
            continue
        print(f"\n── {label} ──")
        for row in random.sample(group, min(args.count // 2, len(group))):
            text = " ".join(str(row["sentence"]).split())
            print(f"\n  〈{row['phrase']}〉 原文里是「{row['surface']}」"
                  f"（文章 {row['article_id']}）")
            print(f"    句子：{text[:150]}")
            if row["gloss_zh"]:
                print(f"    新标的意思：{row['gloss_zh']}")
            else:
                print("    新标：这一处不是词组，两个词各显示各的")
    print("\n" + "=" * 74)
    print("判法：看句子。这一处确实是那个固定说法吗？"
          "\n新的判错了就记下来——单个错不推翻什么，"
          "**成片地错在同一类词组上**才是要回头改的信号。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
