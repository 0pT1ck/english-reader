"""Phase 5 的验收：客户端内核。

**这一轮没有人工项**（决定 23）。P5 没有界面，人没法「用」一个 Swift 库，
所谓人工验收也是跑命令看输出——那本来就是 AI 的活。代价一并记住：
写代码和验收是同一个 AI，共有盲区看不出来。缓解它的是三道「判断标准不来自我」
的防线（测试向量来自服务端真实代码、样本来自真实响应、命令行客户端在线对拍），
但它们守的是「客户端和服务端是否一致」——**如果我对契约本身的理解就错了，
而那个理解同时进了 Core、CLI 和测试，三道防线一条都不会响。**

**一条命令跑完两种语言**（决定 24）。`swift test` 在这里被调用，结果汇总成一份。
但「环境不可用」和「测试失败」要分开报：环境坏掉和代码写错在日志里长得一模一样
（坑 §1.2），不分开的话一次 SDKROOT 没设就会伪装成验收不过。

**两条脚本自己要守的规矩：**

* 不许依赖「今天碰巧有数据」（坑 §4.1）。自己种探针、用完删掉，前后各清一次。
* 整段包进 `notifications.muted()`（坑 §4.4）。这里要故意造失败的事件，
  而告警处理器挂在 logging 上——不静音就是往真实告警通道里灌假警报，
  等于亲手训练用户忽略它，而它是夜间任务失败的唯一出口。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CLIENT = ROOT / "client"

PROBE_DEVICE = "verify.p5"
PROBE_WORD = "__p5probe__"

passed: list[str] = []
failed: list[str] = []


def check(item: str, title: str, ok: bool, detail: str = "") -> bool:
    line = f"  [{'通过' if ok else '失败'}] {item} {title}"
    if detail:
        line += f" — {detail}"
    print(line)
    (passed if ok else failed).append(item)
    return ok


def section(title: str) -> None:
    print(f"\n{title}")


def swift_env() -> dict[str, str]:
    """Two rules, each of which cost an hour on 2026-09-12 (`phase-5.html` §16).

    `NO_PROXY` must be set or a request to 127.0.0.1 goes through the proxy,
    which answers 503 for a port nothing is listening on. And **only the
    upper-case one**: Windows environment variables are case-insensitive and
    Foundation builds a dictionary from them, so having both is a fatal error
    before any of our code runs.
    """
    env = dict(os.environ)
    for name in ("no_proxy", "No_Proxy"):
        env.pop(name, None)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


# --------------------------------------------------------------------------- #
# A. 环境与构建
# --------------------------------------------------------------------------- #


def section_a() -> bool:
    section("A. 环境与构建")

    swift = shutil.which("swift")
    if not check("A1.1", "Swift 工具链可用", swift is not None,
                 swift or "找不到 swift——工具链装在 D:\\Software\\Swift，见 CLAUDE.md"):
        print("\n  环境不可用，Swift 那半没有跑。**这不是测试失败**——"
              "环境坏掉和代码写错在日志里长得一模一样，所以分开报。")
        return False

    version = subprocess.run([swift, "--version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             env=swift_env())
    first = (version.stdout or version.stderr).strip().splitlines()[0:1]
    check("A1.2", "打印出工具链版本", bool(first),
          first[0] if first else "")

    check("A1.3", "Core 里没有界面框架",
          not _grep_ui(CLIENT / "Sources" / "ERCore"),
          "换成安卓也一模一样的归 Core，会长得不一样的归界面——这条 grep 就是那道守卫")
    return True


def _grep_ui(directory: Path) -> list[str]:
    banned = ("import UIKit", "import SwiftUI", "import AppKit")
    hits = []
    for path in directory.rglob("*.swift"):
        text = path.read_text(encoding="utf-8")
        for name in banned:
            if name in text:
                hits.append(f"{path.name}: {name}")
    return hits


# --------------------------------------------------------------------------- #
# B. 契约
# --------------------------------------------------------------------------- #


def section_b() -> None:
    section("B. 契约")
    from backend.main import create_app

    app = create_app()
    spec = app.openapi()
    client_paths = [p for p in spec["paths"] if p.startswith("/v1/client")]
    untyped = []
    for path in client_paths:
        for method, op in spec["paths"][path].items():
            schema = (op.get("responses", {}).get("200", {})
                      .get("content", {}).get("application/json", {}).get("schema", {}))
            if "$ref" not in schema:
                untyped.append(f"{method.upper()} {path}")
    check("B1.1", "客户端端点全部有响应类型", not untyped,
          f"{len(client_paths)} 个端点"
          + (f"，还有 {untyped} 没类型" if untyped else "——"
             "没有类型的话生成出来是 [String: Any]，等于没生成"))

    snapshot = CLIENT / "openapi.json"
    check("B1.2", "契约快照在仓库里", snapshot.exists(), str(snapshot.relative_to(ROOT)))

    if snapshot.exists():
        before = snapshot.read_text(encoding="utf-8")
        subprocess.run([sys.executable, str(ROOT / "scripts" / "export_contract.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=ROOT)
        after = snapshot.read_text(encoding="utf-8")
        check("B1.3", "重新导出契约之后没有 diff", before == after,
              "服务端改了契约这里就会红——那正是它存在的意义")

    generated = CLIENT / "Sources" / "ERContract"
    files = sorted(p.name for p in generated.glob("*.swift"))
    check("B1.4", "生成的 Swift 类型在仓库里", len(files) >= 5, f"{len(files)} 个文件")

    # 生成器会「警告然后跳过」，而跳过等于静默删字段：2026-09-12 有 70 个字段
    # 这样没的，构建还是绿的。所以抽查几个当时消失过的字段还在不在。
    if files:
        schemas = (generated / "Types+Components+Schemas.swift").read_text(encoding="utf-8")
        sentinels = {
            "ArticleResponse.tokens": "public var tokens:",
            "ArticleResponse.glossary": "public var glossary:",
            "ArticleResponse.preparing": "public var preparing:",
            "Learner.level": "public var level:",
        }
        missing = [name for name, needle in sentinels.items() if needle not in schemas]
        check("B1.5", "可空字段没有被生成器悄悄丢掉", not missing,
              "抽查四个 2026-09-12 消失过的字段"
              + (f"，现在缺 {missing}" if missing else ""))


# --------------------------------------------------------------------------- #
# C. 服务端那几处改动
# --------------------------------------------------------------------------- #


def section_c() -> None:
    section("C. 服务端：重复上报、拼写批量、今日包篇数")
    from backend.core import auth, notifications, runtime_config
    from backend.core.db import get_connection
    from backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    conn = get_connection("learning")

    def clean() -> None:
        conn.execute("DELETE FROM client_events WHERE idem_key LIKE 'p5probe-%'")
        for table in ("study_states", "study_marks", "spelling_attempts"):
            try:
                conn.execute(f"DELETE FROM {table} WHERE item_key = ?", (PROBE_WORD,))
            except Exception:  # noqa: BLE001 - a missing column must not strand the lock
                conn.rollback()
        conn.execute("DELETE FROM devices WHERE name = ?", (PROBE_DEVICE,))
        conn.commit()

    clean()  # 前后各清一次：中断的上一轮也不会污染这一轮
    token = auth.create_device(PROBE_DEVICE)
    headers = {"Authorization": f"Bearer {token}"}

    try:
        # 这一段要故意制造失败，所以整段静音——但只静音推送，日志照旧落盘。
        with notifications.muted():
            key = f"p5probe-{uuid.uuid4()}"
            # 缺 kind 会让 _apply 抛异常：存档成功、执行失败。
            broken = {"events": [{"idem_key": key, "type": "word.marked",
                                  "payload": {"headword": PROBE_WORD, "sense_id": 0}}]}
            first = client.post("/v1/client/events", headers=headers, json=broken)
            status = first.json()["results"][0]["status"]
            state = conn.execute(
                "SELECT pool FROM study_states WHERE item_key = ?", (PROBE_WORD,)).fetchone()
            check("C1.1", "注定失败的事件报 failed，且没有副作用",
                  status == "failed" and state is None, f"status={status}")

            # 阳性对照的另一半：修好原因再发同一个键，必须真的执行。
            fixed = {"events": [{"idem_key": key, "type": "word.marked",
                                 "payload": {"headword": PROBE_WORD, "sense_id": 0,
                                             "kind": "unknown"}}]}
            second = client.post("/v1/client/events", headers=headers, json=fixed)
            status2 = second.json()["results"][0]["status"]
            state2 = conn.execute(
                "SELECT pool FROM study_states WHERE item_key = ?", (PROBE_WORD,)).fetchone()
            check("C1.2", "同一个幂等键重发时真的重新执行了",
                  status2 == "accepted" and state2 is not None,
                  f"status={status2}，池={state2['pool'] if state2 else None}"
                  "——不重试的话，这条标记永远不会生效，而且全程无声")

            # 阴性对照：成功过的不许重复执行。
            third = client.post("/v1/client/events", headers=headers, json=fixed)
            check("C1.3", "已经成功的事件重发仍是 duplicate",
                  third.json()["results"][0]["status"] == "duplicate",
                  "缺了这一半就分不清是修好了还是把所有事件都重跑了")

        # 拼写批量端点
        spell_key = f"p5probe-{uuid.uuid4()}"
        body = {"spellings": [{"idem_key": spell_key, "item_key": PROBE_WORD,
                               "typed": PROBE_WORD}]}
        r1 = client.post("/v1/client/reviews/spellings", headers=headers, json=body)
        r2 = client.post("/v1/client/reviews/spellings", headers=headers, json=body)
        check("C2.1", "拼写有批量端点，带幂等键",
              r1.status_code == 200 and r1.json()["accepted"] == 1
              and r2.json()["duplicates"] == 1,
              "P4 给作答加了批量，拼写漏了——地铁里最后一步还是要联网")

        single = client.post("/v1/client/reviews/spelling", headers=headers,
                             json={"item_key": PROBE_WORD, "typed": "wrong"})
        check("C2.2", "原来的单条端点没被动过",
              single.status_code == 200 and single.json()["correct"] is False,
              "铁律 5 只增不减")

        # 今日包篇数
        today = client.get("/v1/client/today", headers=headers)
        package = today.json()
        wanted = int(runtime_config.get("today_article_count"))
        check("C3.1", "今日包正好装配置里那么多篇",
              len(package["articles"]) <= wanted,
              f"{len(package['articles'])} 篇（配置 {wanted}）")
        check("C3.2", "加餐取消了，但 extra_articles 字段还在",
              "extra_articles" in package and package["extra_articles"] == [],
              "字段留着是铁律 5；空是因为往期本身就是加餐（决定 20）")
        check("C3.3", "今日包声明自己不含真题",
              package.get("excludes_exam_papers") is True,
              "写在响应里而不只写在文档里——不然客户端会静默藏起 452 篇真题")
    finally:
        clean()


# --------------------------------------------------------------------------- #
# D. 复习状态机的向量
# --------------------------------------------------------------------------- #


def section_d() -> None:
    section("D. 复习状态机：向量")
    vectors = CLIENT / "Tests" / "ERCoreTests" / "Fixtures" / "review-vectors.json"
    if not check("D1.1", "向量在仓库里", vectors.exists()):
        return

    before = vectors.read_text(encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "export_review_vectors.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    after = vectors.read_text(encoding="utf-8")
    check("D1.2", "重新导出向量之后没有 diff", before == after,
          "服务端的规则一改这里就红，客户端那份镜像要跟着改"
          if before == after else result.stderr[-200:])

    document = json.loads(after)
    cases = document.get("cases", [])
    check("D1.3", "向量覆盖了那几条写下来的规则", len(cases) >= 8,
          f"{len(cases)} 个场景")
    names = " ".join(c["name"] for c in cases)
    check("D1.4", "第二向答错会重新上锁这条在里面", "第二向答错" in names,
          "决定 7，最容易写漏的一条")


# --------------------------------------------------------------------------- #
# E. Swift 那一半
# --------------------------------------------------------------------------- #


def section_e() -> None:
    section("E. Core（swift test）")
    swift = shutil.which("swift")
    if swift is None:
        check("E1.1", "Swift 测试", False, "没有工具链")
        return

    result = subprocess.run([swift, "test"], cwd=CLIENT, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            env=swift_env())
    output = (result.stdout or "") + (result.stderr or "")
    line = next((l.strip() for l in reversed(output.splitlines())
                 if "Test run with" in l), "")
    check("E1.1", "Core 的测试全过", result.returncode == 0,
          line or output.strip().splitlines()[-1:] or "")
    if result.returncode != 0:
        for text in output.splitlines():
            if "✘" in text or "error:" in text or "×" in text:
                print(f"      {text.strip()[:160]}")


# --------------------------------------------------------------------------- #


def main() -> int:
    print("=" * 62)
    print("Phase 5 验收：客户端内核")
    print("（全部由 AI 执行，没有人工项——决定 23。代价见 phase-5.html §17）")
    print("=" * 62)

    environment_ok = section_a()
    section_b()
    section_c()
    section_d()
    if environment_ok:
        section_e()

    print("\n" + "=" * 62)
    print(f"自动检查：{len(passed)} 项通过，{len(failed)} 项失败")
    if failed:
        print("失败：" + "、".join(failed))
    if not environment_ok:
        print("**Swift 那一半没有跑**——是环境不可用，不是测试失败。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
