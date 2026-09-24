"""Re-annotate the whole corpus — P10 的义项，P11 的词组。

Usage::

    uv run python scripts/reannotate_all.py            # 全量
    uv run python scripts/reannotate_all.py --limit 3  # 先跑 3 篇看看
    uv run python scripts/reannotate_all.py --stale-zero  # 只重标「当时查不到、现在查得到」的

**Why a script and not the job framework.** The batch-job machinery exists so
that a *scheduled* run survives restarts and stops at a spend cap. This is a
one-off migration run by hand: it wants a progress line per article, it wants
to keep going past a failure, and it must not start the scheduler — 踩过的坑
§4.6 is exactly the story of a script that did (it triggered a real nightly
generation, then killed it halfway by exiting).

**Resumable by construction.** An article whose tokens are all annotated is
skipped, so interrupting this and running it again costs nothing. That also
means it can be pointed at the corpus repeatedly while the inventory is still
settling.

**Concurrency is the whole runtime.** Serial, this takes 12.5 hours: the
gateway answers a batch in 20–40 seconds and there are ~2,600 batches, which
works out at 2.7 calls a minute — nowhere near the 18/min the rate limit
allows. The bottleneck is waiting, not throughput, so batches run in a pool.
Connections are per-thread (``core/db.py``), so each worker gets its own.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import runtime_config  # noqa: E402
from backend.core.db import get_connection  # noqa: E402
from backend.core.registry import run_core_migrations  # noqa: E402
from backend.modules.llm import jobs, providers  # noqa: E402
from backend.modules.llm import module as _llm_module  # noqa: E402,F401
from backend.modules.phrases import module as _phrases_module  # noqa: E402,F401
from backend.modules.reading import annotate  # noqa: E402
from backend.modules.reading import module as _reading_module  # noqa: E402,F401
from backend.modules.senses import module as _senses_module  # noqa: E402,F401


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="只处理前 N 篇")
    parser.add_argument("--reset", action="store_true",
                        help="先清空已有标注（换义项集之后必须加）")
    parser.add_argument("--stale-zero", action="store_true",
                        help="只把「标成无义项集、而现在查得到义项」的放回去重标")
    args = parser.parse_args()

    # The framework's own tables — without these a script logs into nothing.
    run_core_migrations()

    # `on_startup` does this for the server; a script has to do it itself.
    jobs.register_worker(annotate.WORKER)

    conn = get_connection("content")
    if args.reset:
        cleared = conn.execute(
            "UPDATE reading_tokens SET sense_id = NULL, sense_ordinal = NULL"
            " WHERE kind = 'content'"
        ).rowcount
        conn.commit()
        print(f"清空了 {cleared:,} 条旧标注")

    if args.stale_zero:
        # 2026-09-24：`senses_of` 学会了借英美拼写变体（catalog → catalogue），
        # 于是当初标成 0（无义项集）的一批词现在有义项了。只放回这些——
        # `-1`（问过了、都不贴合）是模型的答案，不是查法的错，不动它。
        # 按「现在查得到」来挑而不是按词表挑，所以哪天义项集补了词，
        # 同一条命令照样管用。
        from backend.modules.senses import repository as senses_repo

        heads = [row[0] for row in conn.execute(
            "SELECT DISTINCT headword FROM reading_tokens"
            " WHERE kind = 'content' AND sense_id = 0 AND headword IS NOT NULL")]
        stale = [h for h in heads if senses_repo.senses_of(h)]
        reopened = 0
        for headword in stale:
            reopened += conn.execute(
                "UPDATE reading_tokens SET sense_id = NULL, sense_ordinal = NULL"
                " WHERE kind = 'content' AND sense_id = 0 AND headword = ?",
                (headword,),
            ).rowcount
        conn.commit()
        print(f"放回重标 {reopened:,} 处，{len(stale)} 个词：{', '.join(stale)}")

    # **词组也算「待标注」**（P11 决定 ④）。少了后半句，一篇词全标完、
    # 词组一处没答的文章会被当成做完了——而那正是看不见的那一半：
    # 客户端只画有义项的词组，所以屏幕上什么都不会少，只是词组全都不见了。
    article_ids = [row[0] for row in conn.execute(
        "SELECT article_id FROM reading_tokens"
        " WHERE kind = 'content' AND sense_id IS NULL"
        " UNION"
        " SELECT article_id FROM reading_phrases WHERE sense_id IS NULL"
        " ORDER BY article_id"
    )]
    if args.limit:
        article_ids = article_ids[:args.limit]
    print(f"待标注 {len(article_ids)} 篇")

    provider_id = runtime_config.get(jobs.provider_key(annotate.KIND))
    provider = (providers.get(provider_id) if provider_id
                else providers.default_provider())
    print(f"提供商 {provider.label} / {provider.model}，"
          f"批次 {runtime_config.get('annotate_batch_words')} 个决策")

    workers = max(1, int(runtime_config.get("llm_concurrency")))
    print(f"并发 {workers}")

    started = time.time()
    counters = {"calls": 0, "failures": 0, "articles": 0}
    lock = threading.Lock()

    def run_article(article_id: int) -> None:
        plan = annotate._plan({"article_id": article_id})
        for _key, payload in plan:
            try:
                annotate._run(provider, payload, {})
                with lock:
                    counters["calls"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad batch must not stop the run
                with lock:
                    counters["failures"] += 1
                print(f"    文章 {article_id} 批次失败："
                      f"{type(exc).__name__}: {exc}"[:160], flush=True)
        with lock:
            counters["articles"] += 1
            index = counters["articles"]
        elapsed = time.time() - started
        rate = index / elapsed if elapsed else 0
        remaining = (len(article_ids) - index) / rate / 3600 if rate else 0
        print(f"  [{index}/{len(article_ids)}] 文章 {article_id} {len(plan)} 批，"
              f"累计 {counters['calls']} 次调用 {elapsed/60:.0f} 分钟，"
              f"预计还要 {remaining:.1f} 小时", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_article, a) for a in article_ids]
        for future in as_completed(futures):
            future.result()
    calls, failures = counters["calls"], counters["failures"]

    print(f"\n完成：{calls} 次调用，{failures} 次失败，"
          f"用时 {(time.time()-started)/60:.0f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
