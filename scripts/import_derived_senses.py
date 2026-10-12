"""Scan, import and target-reannotate explicit Collins derived sections.

Scan is read-only and never opens an application database. Mutating steps need
both --apply and an explicit --data-dir; no scheduler or server is started.

Examples:
  uv run python scripts/import_derived_senses.py scan --word severely
  uv run python scripts/import_derived_senses.py import --word severely --apply --data-dir data/p13-smoke --provider gemini
  uv run python scripts/import_derived_senses.py import --resume 1 --apply --data-dir data/p13-smoke
  uv run python scripts/import_derived_senses.py reannotate --word severely --apply --data-dir data/p13-smoke
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load(path: Path) -> dict[str, str]:
    from scripts.import_collins import load_dictionary
    return load_dictionary(path)


def scan(args) -> tuple[list[dict], list[dict]]:
    from backend.modules.senses import collins, derived
    entries12, entries17 = load(args.mdx2012), load(args.mdx2017)
    index17 = collins.squash_index(entries17)
    wanted = {w.lower().strip() for w in args.word} if args.word else None
    candidates, issues = [], []
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (args.mdx2012, args.mdx2017)}
    for parent, html12 in sorted(entries12.items()):
        if 'class="vExplain_r"' not in html12:
            continue
        key17 = collins.entry_key(parent, entries17, index17)
        found, rejected = derived.scan_entry(parent, html12, entries17.get(key17), wanted)
        for candidate in found:
            payload = candidate.payload()
            payload["dictionary_files"] = hashes
            payload["entry_2017"] = key17
            candidates.append(payload)
        issues.extend(rejected)
    roots = defaultdict(set)
    for p in candidates:
        roots[p["headword"]].add(p["parent"])
    for p in candidates:
        p["formation_unambiguous"] = len(roots[p["headword"]]) == 1
    if args.limit:
        keep = set(sorted({p["headword"] for p in candidates})[:args.limit])
        candidates = [p for p in candidates if p["headword"] in keep]
    if wanted:
        seen = {p["headword"] for p in candidates} | {p.get("headword") for p in issues}
        issues.extend({"headword": w, "reason": "no_explicit_2012_runon"} for w in sorted(wanted - seen))
    print(json.dumps({"step": "scan", "candidates": candidates, "issues": issues,
                      "counts": dict(Counter(i["reason"] for i in issues))}, ensure_ascii=False, indent=2))
    return candidates, issues


def setup(data_dir: Path) -> None:
    os.environ["ER_DATA_DIR"] = str(data_dir.resolve())
    from backend.core.db import run_migrations
    from backend.core.registry import run_core_migrations
    from backend.modules.llm import schema as llm_schema
    from backend.modules.llm import module as _llm_module  # noqa: F401
    from backend.modules.senses import schema as sense_schema, derived_worker
    from backend.modules.wordfamily import schema as family_schema
    run_core_migrations()
    run_migrations("llm", llm_schema.MIGRATIONS)
    run_migrations("senses", sense_schema.MIGRATIONS)
    run_migrations("wordfamily", family_schema.MIGRATIONS)
    derived_worker.register()


def await_job(job_id: int, timeout: float, *, resume: bool = False) -> int:
    from backend.modules.llm import jobs
    if jobs.get(job_id)["status"] == "done":
        print(f"任务 {job_id} 已完成")
        return 0
    if resume:
        if jobs.get(job_id)["live"]:
            raise ValueError("任务仍在本进程运行，不能重置重试次数")
        from backend.core.db import get_connection
        conn = get_connection("ops")
        # Explicit resumption after quota recovery retries failures, never done items.
        conn.execute("UPDATE llm_job_items SET attempts = 0 WHERE job_id = ? AND status = 'failed'",
                     (job_id,))
        conn.commit()
    deadline = time.monotonic() + timeout
    jobs.start(job_id)
    try:
        while True:
            job = jobs.get(job_id)
            if job["status"] not in {"running", "pending"} and not job["live"]:
                print(json.dumps({"job_id": job_id, "status": job["status"], "done": job["done"],
                                  "total": job["total"], "error": job.get("error")}, ensure_ascii=False))
                return 0 if job["status"] == "done" else 1
            if time.monotonic() >= deadline:
                raise TimeoutError("等待超时；完成部分已落库，任务暂停后可用 --resume 续跑")
            time.sleep(0.5)
    except (KeyboardInterrupt, TimeoutError) as exc:
        jobs.pause(job_id)
        # Allow bounded in-flight provider calls to commit before this CLI exits.
        for entry in jobs.running_entries():
            if entry.thread.name == f"llm-job-{job_id}":
                entry.thread.join()
        print(f"{exc}；job_id={job_id}", file=sys.stderr)
        return 1


def reannotation(args) -> int:
    from backend.core.db import get_connection
    from backend.modules.senses import repository
    from backend.modules.llm import jobs
    from backend.modules.reading import annotate
    from backend.modules.reading import module as _reading_module  # noqa: F401
    conn = get_connection("content")
    # Targeted repair consumes an initialized corpus; it must not replay legacy
    # reading/phrase cutovers merely to discover an empty database.
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reading_tokens'",
    ).fetchone():
        if args.resume:
            raise ValueError("指定库没有阅读语料，无法续跑标注任务")
        print("指定库没有阅读语料；无需定向重标，未执行历史 reading/phrases 迁移")
        return 0
    phrase_columns = {row[1] for row in conn.execute("PRAGMA table_info(reading_phrases)")}
    if "sense_id" not in phrase_columns:
        raise ValueError("阅读语料尚未完成既有 reading/phrases 迁移；定向重标不代替初始化或升级")
    words = {r[0] for r in conn.execute("SELECT DISTINCT headword FROM derived_sense_sources")}
    if args.word:
        words &= {w.strip().lower() for w in args.word}
    from backend.modules.vocabulary import spelling
    words.update(variant for word in tuple(words) for variant in spelling.variants(word))
    rows = []
    for word in sorted(words):
        if any(s.get("source_dict") == repository.DERIVED_SOURCE for s in repository.senses_of(word)):
            rows.extend(dict(r) for r in conn.execute(
                "SELECT id, article_id, sentence_id FROM reading_tokens"
                " WHERE kind = 'content' AND headword = ? AND (sense_id = 0 OR sense_id IS NULL)"
                " ORDER BY article_id, seq", (word,),
            ))
    if not rows and not args.resume:
        print("没有本次派生义项影响的旧 0 或待续跑 token")
        return 0
    kind = "sense_derived_reannotation"
    def plan(params):
        grouped = defaultdict(list)
        for row in params["tokens"]:
            grouped[row["article_id"]].append(row["id"])
        from backend.core import runtime_config
        size = max(5, int(runtime_config.get("annotate_batch_words")))
        return [(f"{article}:{start}", {"article_id": article, "token_ids": ids[start:start+size],
                                        "phrase_ids": []})
                for article, ids in grouped.items() for start in range(0, len(ids), size)]
    jobs.register_worker(annotate.WORKER)
    jobs.register_worker(jobs.Worker(kind=kind, title="定向重标派生词旧无义项语境", plan=plan,
                                    run_item=annotate._run,
                                    default_provider=lambda: jobs.configured_provider(annotate.KIND)))
    if args.resume:
        job_id = args.resume
        if jobs.get(job_id)["kind"] != kind:
            raise ValueError("--resume 不是派生词定向标注任务")
    else:
        repository.reopen_derived_zero_tokens(sorted(words))
        job_id = jobs.create(kind, params={"tokens": rows}, provider_id=args.provider, spend_cap=0)
    print(f"定向标注 job_id={job_id}，仅影响词={','.join(sorted(words))}")
    return await_job(job_id, args.wait_timeout, resume=bool(args.resume))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("scan", "import", "reannotate"))
    parser.add_argument("--word", action="append", default=[], help="派生词；可重复，不是母词")
    parser.add_argument("--limit", type=int, help="限制派生词数，保留同词所有可确定小节")
    parser.add_argument("--mdx2012", type=Path, default=Path("data/dictionaries/collins-cobuild-2012.mdx"))
    parser.add_argument("--mdx2017", type=Path, default=Path("data/dictionaries/collins-youdao-2017.mdx"))
    parser.add_argument("--dry-run", action="store_true", help="仅扫描；不打开或迁移数据库，不调用模型")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--provider", help="否则使用 llm_provider_sense_derived_definition 配置")
    parser.add_argument("--resume", type=int, help="续跑已有任务号，不重建完成项")
    parser.add_argument("--wait-timeout", type=float, default=1800)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1 or args.wait_timeout <= 0:
        parser.error("limit 和 wait-timeout 必须为正数")
    if args.step == "scan" or args.dry_run or not args.apply:
        scan(args)
        if args.step != "scan":
            print("仅扫描。写库或重标需明确 --apply --data-dir。", file=sys.stderr)
        return 0
    if args.data_dir is None:
        parser.error("写库需明确 --data-dir（本地调试用 data/p13-smoke）")
    setup(args.data_dir)
    from backend.modules.llm import jobs
    from backend.modules.senses import derived_worker, repository
    if args.step == "reannotate":
        return reannotation(args)
    if args.resume:
        job = jobs.get(args.resume)
        if job["kind"] != derived_worker.KIND:
            parser.error("--resume 不是派生英文定义任务")
        if job["status"] == "done":
            print(f"任务 {args.resume} 已完成，不重新调用模型")
            return 0
        return await_job(args.resume, args.wait_timeout, resume=True)
    candidates, _ = scan(args)
    pending = []
    for p in candidates:
        own = [s for s in repository.senses_of(p["headword"])
               if s.get("source_dict") != repository.DERIVED_SOURCE]
        if own:
            print(f"跳过 {p['headword']}：已有主词义项，不能覆盖", file=sys.stderr)
        elif not repository.derived_sense_for(p):
            pending.append(p)
        else:
            derived_worker._store_formation(p)
    if not pending:
        print("没有尚未补完的可确定派生义项")
        return 0
    job_id = jobs.create(derived_worker.KIND, params={"candidates": pending},
                         provider_id=args.provider, spend_cap=0)
    print(f"英文定义 job_id={job_id}，未完成时用 --resume {job_id} 续跑", flush=True)
    return await_job(job_id, args.wait_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
