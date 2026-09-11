"""Run the Phase 0 verification checklist.

    uv run python scripts/verify_phase0.py

Checks the items that can be verified mechanically. Three of the nine have to be
done by hand and are reported as such: receiving a Bark push, running the
container on the NanoPi, and clicking through the console.

Kept in the repository rather than thrown away because every later phase will
add to the framework this exercises, and a regression in it — migrations,
logging, module discovery — is exactly the kind of failure that otherwise stays
invisible until it corrupts something.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend.core.config import get_settings  # noqa: E402
from backend.core import notifications  # noqa: E402
from backend.core.db import Migration, get_connection, run_migrations  # noqa: E402
from backend.core.logging import get_logger, trace  # noqa: E402
from backend.main import app  # noqa: E402

passed: list[str] = []
failed: list[str] = []


def check(number: str, label: str, condition: bool, detail: str = "") -> None:
    mark = "通过" if condition else "失败"
    (passed if condition else failed).append(f"{number} {label}")
    print(f"  [{mark}] {number} {label}" + (f" — {detail}" if detail else ""))


def main() -> int:  # noqa: PLR0915 - a checklist reads better in one piece
    settings = get_settings()
    headers = {"X-Admin-Secret": settings.admin_secret}

    print("Phase 0 验收\n" + "=" * 62)

    with TestClient(app) as client:
        # --- 1. service starts ------------------------------------------- #
        print("\n1. 服务启动")
        r = client.get("/health")
        check("1.1", "健康检查", r.status_code == 200 and r.json()["status"] == "ok")

        # --- 2. vocabulary analysis --------------------------------------- #
        print("\n2. 词汇分析")
        text = ("The leaves fell as he left Copenhagen. She saw the mayor address "
                "a problem the city had failed to address.")
        r = client.post("/v1/admin/vocabulary/analyze", headers=headers,
                        json={"text": text})
        check("2.1", "分析接口可调用", r.status_code == 200)

        tokens = [t for s in r.json()["sentences"] for t in s["tokens"]]
        by_key = {(t["text"].lower(), t["pos"]): t["lemma"] for t in tokens}
        expectations = {
            ("leaves", "NOUN"): "leaf",
            ("fell", "VERB"): "fall",
            ("left", "VERB"): "leave",
            ("saw", "VERB"): "see",
            ("address", "VERB"): "address",
        }
        wrong = {k: (by_key.get(k), v) for k, v in expectations.items()
                 if by_key.get(k) != v}
        check("2.2", "歧义词形归类正确", not wrong,
              f"{len(expectations) - len(wrong)}/{len(expectations)}"
              + (f"，错误：{wrong}" if wrong else ""))

        proper = [t["text"] for t in tokens if t["is_proper_noun"]]
        check("2.3", "专有名词被标出", "Copenhagen" in proper, f"识别到 {proper}")

        known = [t for t in tokens if t["is_content"] and t["in_dictionary"]]
        cet = [t for t in known if t["tags"] and "cet" in t["tags"]]
        check("2.4", "词典数据可用（词条/词频/大纲标签）",
              len(known) > 5 and len(cet) > 0,
              f"实词命中 {len(known)} 个，其中带 CET 标签 {len(cet)} 个")

        # --- 3. API contract ---------------------------------------------- #
        print("\n3. 契约文档")
        r = client.get("/openapi.json")
        spec = r.json() if r.status_code == 200 else {}
        paths = spec.get("paths", {})
        check("3.1", "OpenAPI 可访问", r.status_code == 200,
              f"{len(paths)} 个接口")
        # Until P2 this asserted that *no* client endpoint existed, which was a
        # phase-boundary marker rather than a property. P2 wrote the first
        # client contract, so the marker has expired and what is left to check
        # is the property architecture rule 4 actually states: the two surfaces
        # live under separate prefixes and neither leaks into the other. That
        # a client token cannot reach an admin endpoint is checked in
        # verify_phase2.py, where the token exists.
        admin_paths = [p for p in paths if p.startswith("/v1/admin")]
        client_paths = [p for p in paths if p.startswith("/v1/client")]
        stray = [p for p in paths
                 if p.startswith("/v1/") and p not in admin_paths and p not in client_paths]
        check("3.2", "两套接口分离",
              bool(admin_paths) and not stray,
              f"管理 {len(admin_paths)} 个 / 客户端 {len(client_paths)} 个"
              + (f"，越界路径：{stray}" if stray else "，没有越界路径"))
        r = client.get("/docs")
        check("3.3", "文档页面可打开", r.status_code == 200)

        # --- 4. admin console --------------------------------------------- #
        print("\n4. 管理控制台")
        check("4.1", "未认证访问被拒",
              client.get("/v1/admin/status").status_code == 401)
        check("4.2", "未登录页面重定向到登录",
              client.get("/admin", follow_redirects=False).status_code == 303)

        r = client.post("/admin/login", data={"secret": settings.admin_secret},
                        follow_redirects=False)
        check("4.3", "密码登录", r.status_code == 303 and "er_admin" in r.cookies)

        pages = {"/admin": "状态", "/admin/logs": "日志", "/admin/config": "配置",
                 "/admin/backup": "备份", "/admin/vocabulary": "词汇",
                 "/admin/example": "示例"}
        broken = [p for p in pages if client.get(p).status_code != 200]
        check("4.4", "各页面可打开", not broken,
              f"{len(pages) - len(broken)}/{len(pages)}"
              + (f"，打不开：{broken}" if broken else ""))

        r = client.get("/v1/admin/logs?level=INFO&limit=10", headers=headers)
        check("4.5", "日志可按条件查询", r.status_code == 200,
              f"返回 {r.json().get('count', 0)} 条")

        r = client.get("/v1/admin/diagnostic-bundle?hours=1", headers=headers)
        check("4.6", "诊断包可导出",
              r.status_code == 200 and "attachment" in r.headers.get("content-disposition", ""),
              f"{len(r.content) / 1024:.0f} KB")

        # P1b turned this into a zip holding every database that cannot be
        # regenerated, so the check is now "both are in there", not "it is a
        # SQLite file".
        r = client.get("/v1/admin/backup", headers=headers)
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
                members = set(archive.namelist())
        except zipfile.BadZipFile:
            members = set()
        check("4.7", "备份可下载且含 learning + content",
              r.status_code == 200 and {"learning.db", "content.db"} <= members,
              f"{len(r.content) / 1024:.0f} KB · {', '.join(sorted(members)) or '不是压缩包'}")

        r = client.post("/v1/admin/restore", headers=headers,
                        files={"file": ("bad.db", b"not a database", "application/octet-stream")})
        check("4.8", "恢复接口拒绝非法文件", r.status_code == 400)

        # --- 5. notifications (manual) ------------------------------------ #
        print("\n5. Bark 通知")
        r = client.post("/v1/admin/notifications/test", headers=headers)
        check("5.1", "测试接口可调用（是否收到需人工确认）", r.status_code == 200,
              "已发送" if r.json().get("sent") else "未配置 Bark，跳过实际发送")

        # --- 6. extensibility --------------------------------------------- #
        print("\n6. 扩展机制")
        status = client.get("/v1/admin/status", headers=headers).json()
        names = {m["name"] for m in status["modules"]}
        check("6.1", "模块自动发现", {"vocabulary", "example"} <= names,
              f"已装载 {sorted(names)}")
        check("6.2", "模块页面自动进入导航",
              client.get("/admin/example").status_code == 200)
        check("6.3", "模块接口自动挂载",
              client.get("/v1/admin/example/observations", headers=headers).status_code == 200)
        check("6.4", "模块事件订阅生效",
              "app.started" in status["event_subscribers"],
              str(status["event_subscribers"].get("app.started", [])))
        cfg = client.get("/v1/admin/config", headers=headers).json()
        keys = {s["key"] for s in cfg["specs"]}
        check("6.5", "模块配置项自动出现", "example_keep_observations" in keys)

        obs = client.get("/v1/admin/example/observations", headers=headers).json()
        check("6.6", "模块数据表已建立并写入", obs["count"] > 0,
              f"{obs['count']} 条观察记录")

        # --- 7. migrations ------------------------------------------------- #
        print("\n7. 迁移机制")
        rows = get_connection("learning").execute(
            "SELECT module, version, name FROM schema_migrations ORDER BY module, version"
        ).fetchall()
        check("7.1", "迁移已记录版本", len(rows) >= 4,
              f"learning.db 中 {len(rows)} 条：{sorted({r['module'] for r in rows})}")

        probe = Migration(version=99, name="verification probe", database="learning",
                          apply="CREATE TABLE IF NOT EXISTS _verify_probe (x INTEGER)")
        applied = run_migrations("verify.probe", [probe])
        again = run_migrations("verify.probe", [probe])
        check("7.2", "迁移只应用一次", len(applied) == 1 and len(again) == 0)

        backups = sorted(settings.backup_dir.glob("learning-*.db"))
        check("7.3", "改动 learning.db 前自动备份", len(backups) > 0,
              f"{len(backups)} 个备份文件")
        get_connection("learning").execute("DROP TABLE IF EXISTS _verify_probe")
        get_connection("learning").execute(
            "DELETE FROM schema_migrations WHERE module = 'verify.probe'")
        get_connection("learning").commit()

        # --- 9. traceability ----------------------------------------------- #
        print("\n9. 错误可追溯")
        log = get_logger("verify")
        # 这一段故意报一条 ERROR 来验追溯链路，而 ERROR 会推到手机——
        # 验收不该把假警报推给用户（notifications 自己说的：狼来了的通道会被忽略）。
        with notifications.muted(), trace() as tid:
            log.debug("verify.detail", "这条 DEBUG 平时只留在内存里", step=1)
            log.debug("verify.detail", "这条也是", step=2)
            log.error("verify.failure", "故意触发的错误，用于验证追溯链路")

        found = get_connection("logs").execute(
            "SELECT level, event FROM logs WHERE trace_id = ? ORDER BY id", (tid,)
        ).fetchall()
        levels = [r["level"] for r in found]
        check("9.1", "错误按 trace 可查回", "ERROR" in levels, f"trace {tid}")
        check("9.2", "出错时自动落盘该 trace 的 DEBUG 明细",
              levels.count("DEBUG") == 2,
              f"落盘 {levels.count('DEBUG')} 条 DEBUG + {levels.count('ERROR')} 条 ERROR")

        r = client.get("/health")
        check("9.3", "响应带 trace 编号", bool(r.headers.get("X-Trace-Id")),
              r.headers.get("X-Trace-Id", ""))

        # 一个 404 也必须留下能按 trace 查回的记录。它曾经不留：处理器上写着
        # 「保持 INFO 级」，下面却没有任何 log 调用，于是 404 带着 trace_id 发了
        # 出去、什么也没写下。用户把 trace 贴过来，查出来是空的——而「贴 trace
        # 就能定位」正是把它放进响应里的全部理由。2026-09-09 发现并补上。
        # 每个管理页都继承 base.html，也就继承了它的全局作用域。P3 的复习页在这里
        # 栽过：它定义了自己的 `api()` 走 /v1/client，而 base.html 也有一个同名的
        # `api()` 走 /v1/admin——两个函数声明同名同作用域，后解析的赢。base 的脚本
        # 排在内容块之后，于是页面加载正常（首次请求早于 base 解析），之后每一次
        # 点击都发到 /v1/admin 去，404。**页面看着是「按钮点不动」，日志里才是真相。**
        def top_level_names(text: str) -> set[str]:
            """Declarations at brace depth 0 of a template's <script> blocks."""
            import re as _re
            names, depth = set(), 0
            js = "\n".join(m.group(1) for m in
                           _re.finditer(r"<script>(.*?)</script>", text, _re.S))
            for line in js.splitlines():
                bare = _re.sub(r"//.*", "", line)
                if depth == 0:
                    m = _re.match(r"\s*(?:async\s+)?(?:function|const|let|var)\s+"
                                  r"([A-Za-z_$][\w$]*)", bare)
                    if m:
                        names.add(m.group(1))
                depth += bare.count("{") + bare.count("(") - bare.count("}") - bare.count(")")
                depth = max(0, depth)
            return names

        base_names = top_level_names(
            (ROOT / "backend/admin/templates/base.html").read_text(encoding="utf-8"))
        clashes = []
        for tpl in sorted((ROOT / "backend").rglob("templates/*.html")):
            text = tpl.read_text(encoding="utf-8")
            if "{% extends" not in text:
                continue
            hit = top_level_names(text) & base_names
            if hit:
                clashes.append(f"{tpl.name}: {'、'.join(sorted(hit))}")
        check("4.9", "管理页没有覆盖控制台的全局名",
              not clashes,
              f"base.html 的全局有 {'、'.join(sorted(base_names))}，没有页面重定义它们"
              if not clashes else "；".join(clashes))

        # `hidden` has to actually hide. base.html styles every button with a
        # `display` rule, which outranks the browser's own `[hidden]` rule — so
        # hiding a button left it visible but inert, and the page looked frozen
        # while behaving correctly. One line of CSS fixes it for every module
        # page; this check is here so nobody deletes that line.
        base_css = (ROOT / "backend/admin/templates/base.html").read_text(encoding="utf-8")
        check("4.10", "[hidden] 压得过控制台自己的 display 规则",
              "[hidden]" in base_css and "display: none !important" in base_css,
              "按钮上有 display 规则，不加这条 [hidden] 就只是让按钮失效而不隐藏——"
              "页面看着像卡死")

        r = client.get("/v1/client/__no_such_path__?probe=1")
        tid404 = r.json().get("error", {}).get("trace_id", "")
        rows = get_connection("logs").execute(
            "SELECT event, context FROM logs WHERE trace_id = ?", (tid404,)
        ).fetchall()
        paths = [x["context"] for x in rows if "__no_such_path__" in (x["context"] or "")]
        check("9.4", "404 也查得回，且记着是哪个地址",
              r.status_code == 404 and bool(tid404) and bool(paths),
              f"trace {tid404} → {len(rows)} 条，含路径与方法"
              if paths else "404 带了 trace_id 却没有对应的日志——贴 trace 也查不出东西")

    # --- summary ---------------------------------------------------------- #
    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败")
    if failed:
        for item in failed:
            print(f"  失败：{item}")
    print("\n需要人工确认的三项：")
    print("  5   在控制台填入 Bark 地址后点测试，手机应收到推送")
    print("  8   在 NanoPi 上用 Docker 启动，容器重建后数据不丢")
    print("  4   浏览器里实际点一遍控制台各页面")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
