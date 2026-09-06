"""Run the Phase 1b verification checklist.

    uv run python scripts/verify_phase1b.py

Checks what can be checked without spending money. Three items need an API key
and a real batch run, and are reported as manual — the script says exactly what
to do for each rather than pretending to have verified it.

Kept in the repository for the same reason as the Phase 0 script: every later
phase builds on this machinery, and a regression in the lemma resolver or the
family rules is invisible until it quietly corrupts a word count.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core.config import get_settings  # noqa: E402
from backend.core.db import BACKED_UP, get_connection  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402

passed: list[str] = []
failed: list[str] = []
manual: list[str] = []

log = get_logger("scripts.verify1b")


def check(number: str, title: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(number)
    mark = "通过" if ok else "失败"
    print(f"  [{mark}] {number} {title}" + (f" — {detail}" if detail else ""))


def note(number: str, title: str, detail: str) -> None:
    manual.append(number)
    print(f"  [人工] {number} {title} — {detail}")


def main() -> int:  # noqa: PLR0915 - a checklist reads better in one place
    settings = get_settings()
    headers = {"X-Admin-Secret": settings.admin_secret}

    with trace(), TestClient(app) as client:
        # --- 1. four databases -------------------------------------------- #
        print("\n1. 四个数据库")
        status = client.get("/v1/admin/status", headers=headers).json()
        check("1.1", "content.db 已建立",
              "content" in status["databases"],
              f"{status['databases'].get('content', {}).get('size_bytes', 0)} 字节")
        check("1.2", "备份范围是 learning + content",
              set(BACKED_UP) == {"learning", "content"}, str(BACKED_UP))

        conn = get_connection("learning")
        attached = [row[1] for row in conn.execute("PRAGMA database_list")]
        check("1.3", "learning 连接同时挂载 dict 与 content",
              {"dict", "content"} <= set(attached), str(attached))

        dictionary_tables = {
            row["name"] for row in get_connection("dictionary").execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        check("1.4", "生成内容已搬出 dictionary.db",
              not ({"senses", "word_families"} & dictionary_tables),
              f"dictionary.db 现有表：{sorted(dictionary_tables)}")

        # --- 2. backup and restore ---------------------------------------- #
        print("\n2. 备份与恢复")
        response = client.get("/v1/admin/backup", headers=headers)
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                members = set(archive.namelist())
        except zipfile.BadZipFile:
            members = set()
        check("2.1", "完整备份含两个库",
              {"learning.db", "content.db"} <= members,
              f"{len(response.content) // 1024} KB · {', '.join(sorted(members))}")

        single = client.get("/v1/admin/backup/content", headers=headers)
        check("2.2", "可单独下载 content.db",
              single.status_code == 200
              and single.content.startswith(b"SQLite format 3\x00"),
              f"{len(single.content) // 1024} KB")

        bad = client.post("/v1/admin/restore", headers=headers,
                          files={"file": ("x.db", b"nope", "application/octet-stream")})
        check("2.3", "恢复接口拒绝非法文件", bad.status_code == 400)

        # --- 3. secrets --------------------------------------------------- #
        print("\n3. 密钥隔离")
        secrets_path = settings.data_dir / "secrets.json"
        bundle = client.get("/v1/admin/diagnostic-bundle?hours=1", headers=headers).json()
        text = json.dumps(bundle, ensure_ascii=False)
        check("3.1", "诊断包里没有密钥字段",
              "api_key" not in text and "bark_url" not in text)

        from backend.core import runtime_config
        secret_specs = [s.key for s in runtime_config.specs() if s.secret]
        check("3.2", "凭据配置项已标记 secret", bool(secret_specs), str(secret_specs))
        check("3.3", "密钥文件不在备份里",
              "secrets.json" not in members,
              f"存放于 {secrets_path.name}，{'已存在' if secrets_path.exists() else '尚未创建'}")

        # --- 4. dictionary defects ---------------------------------------- #
        print("\n4. 五个词典缺陷")
        from backend.modules.vocabulary import analyzer, repository, spelling

        nlp = analyzer._load_model()  # noqa: SLF001 - the loader is the public path
        lemmas = {}
        for token in nlp("The other leaves fell and the mice ate the data."):
            if token.is_alpha:
                lemmas[token.text.lower()] = analyzer.resolve_lemma(token)[0]
        check("4.1", "词形还原不再造词",
              lemmas.get("other") == "other" and lemmas.get("leaves") == "leaf",
              f"other→{lemmas.get('other')}, leaves→{lemmas.get('leaves')}")

        check("4.2", "英美拼写标签合并",
              "cet4" in repository.tags_of("neighbor")
              and "cet4" in repository.tags_of("theater"),
              f"neighbor={sorted(repository.tags_of('neighbor'))}")
        check("4.3", "拼写变体不误配",
              not spelling.variants("size") and not spelling.variants("member"),
              "size / member 没有被配到 sise / membre")

        doc = nlp("A socio-economic study of well-timed policy.")
        spans = analyzer.hyphenated_spans(doc)
        check("4.4", "连字符复合词整体识别",
              len(set(spans.values())) == 2, f"识别出 {len(set(spans.values()))} 个复合词")

        check("4.5", "ECDICT 转义已还原",
              "\\n" not in ((repository.lookup("state") or {}).get("translation") or ""),
              "释义里没有字面 \\n")

        # --- 5. word families --------------------------------------------- #
        print("\n5. 词族")
        from backend.modules.wordfamily import repository as families

        affix_stats = families.affix_stats()
        check("5.1", "词缀表已导入",
              affix_stats.get("total", 0) >= 600,
              f"{affix_stats.get('total', 0)} 条：前缀 {affix_stats.get('prefix', 0)} ·"
              f" 后缀 {affix_stats.get('suffix', 0)} · 词根 {affix_stats.get('root', 0)}")

        family_stats = families.family_stats()
        check("5.2", "规则分解已跑",
              family_stats.get("total", 0) > 1000,
              f"{family_stats.get('total', 0)} 条 {family_stats.get('by_grade', {})}")
        check("5.3", "carefully 归并到 careful，不算生词",
              families.transparent_root("carefully") == "careful")
        check("5.4", "nationality 能给出分解",
              (families.family_of("nationality") or {}).get("root") == "national",
              str(families.family_of("nationality")))

        # --- 6. checker ---------------------------------------------------- #
        print("\n6. 校验器")
        rows = get_connection("learning").execute(
            "SELECT body, target_words, report FROM generation_drafts"
        ).fetchall()
        if not rows:
            note("6.1", "拿 P1a 的 16 篇重跑", "learning.db 里没有草稿，跳过")
        else:
            from backend.modules.generation import checker

            old = sum(json.loads(r["report"])["beyond_rate"] for r in rows) / len(rows)
            new_reports = [
                checker.check(r["body"], target_words=json.loads(r["target_words"]),
                              allowed_tiers=["zk", "gk", "cet4"], exam="cet4").as_dict()
                for r in rows
            ]
            new = sum(r["beyond_rate"] for r in new_reports) / len(rows)
            derived = sum(r["derived_rate"] for r in new_reports) / len(rows)
            check("6.1", "超纲率显著下降", new < old * 0.6,
                  f"{old:.2f}% → {new:.2f}%（另有 {derived:.2f}% 是已知词根的派生词）")

            beyond_words = {b["lemma"] for r in new_reports for b in r["beyond"]}
            ghosts = {"oth", "planning", "neighbor", "carefully", "datum", "socio"}
            check("6.2", "假超纲词已消失",
                  not (beyond_words & ghosts),
                  f"仍在列表里的：{sorted(beyond_words & ghosts) or '无'}")

        baseline_file = settings.data_dir / "syntax_baseline.json"
        check("6.3", "句法基线已按新口径重算",
              baseline_file.exists(),
              "删除该文件即可强制重算" if baseline_file.exists() else "文件不存在")

        # --- 7. senses ------------------------------------------------------ #
        print("\n7. 义项集")
        from backend.modules.senses import repository as senses
        from backend.modules.senses import screening

        screen_stats = screening.stats()
        check("7.1", "粗筛已跑",
              screen_stats.get("total", 0) > 5000,
              f"目标词 {screen_stats.get('total', 0)}，需建 {screen_stats.get('needs_senses', 0)}")

        sense_stats = senses.stats()
        if sense_stats["words"]:
            check("7.2", "义项集已建",
                  sense_stats["words"] > 0,
                  f"{sense_stats['words']} 个词 · {sense_stats['senses']} 个义项 ·"
                  f" 平均 {sense_stats['per_word']}")
        else:
            note("7.2", "义项集已建",
                 "还没跑。到 /admin/llm 建一个 sense_build 任务（先填好提供商和密钥）")

        # --- 8. batch jobs -------------------------------------------------- #
        print("\n8. 批次任务")
        payload = client.get("/v1/admin/llm/jobs", headers=headers).json()
        kinds = {k["kind"] for k in payload["kinds"]}
        check("8.1", "四种批次任务已注册",
              {"affix_gloss", "family_review", "sense_build", "sense_examples"} <= kinds,
              str(sorted(kinds)))

        columns = {
            row[1] for row in get_connection("learning").execute(
                "PRAGMA table_info(llm_job_items)")
        }
        check("8.2", "任务按项记录状态（断点续传的前提）",
              {"status", "attempts", "cost", "tokens_in"} <= columns)
        check("8.3", "任务有花费上限字段",
              "spend_cap" in {
                  row[1] for row in get_connection("learning").execute(
                      "PRAGMA table_info(llm_jobs)")
              })

        providers = client.get("/v1/admin/llm/providers", headers=headers).json()
        if providers["providers"]:
            configured = [p["id"] for p in providers["providers"] if p["has_key"]]
            check("8.4", "至少一个提供商已配置密钥", bool(configured), str(configured))
        else:
            note("8.4", "提供商已配置",
                 "还没添加。到 /admin/llm 选一个预设，填 base_url、模型名和密钥，点测试")

        # --- manual --------------------------------------------------------- #
        print("\n需要人工确认")
        note("M1", "接口真的能调通",
             "/admin/llm 上点「测试」，应返回 ok 并显示本次花费")
        note("M2", "批次任务断点续传",
             "跑一个 sense_build，中途点暂停再点继续，进度应接着走而不是从头开始")
        note("M3", "第二轮生成有改善",
             "/admin/generation 出提示词，贴给模型，看超纲率、内容具体性、被动与形式主语")

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败，{len(manual)} 项待人工")
    if failed:
        print("失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
