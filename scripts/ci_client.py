"""本地跑一遍 `client.yml` 做的四件事。

Core 验证 Core verification / 契约漂移 contract drift

**为什么这份脚本存在**（2026-09-19，Mac 迁移之后）：`client.yml` 那四个 job
存在的理由都是「本机没有 Swift 工具链」——r5s 是 ARM Linux、不装 Swift；
Windows 上装了但只是备用。**这两条前提在这台 Mac 上都不成立了**：
Xcode 27 自带 Swift 6.4，`swift test`、给 iOS 编译、生成契约类型，
本地都能做，而且比推一次等 CI 快得多。

**这份脚本不是要删掉 `client.yml`**，是要让日常迭代不必每次都推一次去问 CI——
CI 留着当独立的第二次验证（真的是另一台机器、另一份环境），但不再是
唯一能验的地方。四件事照抄 `client.yml` 的四个 job，一件不多一件不少：

1. Core 里不许出现界面框架（`import UIKit/SwiftUI/AppKit`）
2. `swift test`
3. Core 能不能给 iOS 目标编译，且产物真的是 iOS 平台的（读 Mach-O load command）
4. 契约类型重新生成一遍，和提交的比对——逐字节一致，而不只是抽查几个字段

**不做的事**：原 `client.yml` 的 `core` job 跑在 `swift:6.3` 的 Linux 容器里，
这里没有复刻——那是「Core 在 Linux 上也编得过」这个独立事实，本地 macOS
验证不了它，这个缺口如实记在下面的收尾提示里，不假装补上了。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"

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


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def main() -> int:
    print("=" * 62)
    print("本地版 client.yml")
    print("=" * 62)

    section("1. Core 里不许有界面框架")
    offenders = []
    for path in (CLIENT / "Sources" / "ERCore").rglob("*.swift"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*import\s+(UIKit|SwiftUI|AppKit)\b", text, re.M):
            offenders.append(str(path.relative_to(ROOT)))
    check("L0.1", "ERCore 不 import 界面框架", not offenders,
          "干净" if not offenders else "、".join(offenders))

    section("2. swift test")
    result = run(["swift", "test"], cwd=CLIENT)
    ok = result.returncode == 0
    # 只在失败时把输出倒出来——通过时几百行测试名没人要看。
    tail = "\n".join(result.stdout.splitlines()[-30:]) if not ok else ""
    check("L0.2", "swift test 全过", ok,
          "" if ok else tail or result.stderr[-2000:])

    section("3. Core 能不能给 iOS 目标编译")
    sdk = run(["xcrun", "--sdk", "iphoneos", "--show-sdk-path"])
    sdk_path = sdk.stdout.strip()
    # **`-Xswiftc -target` 在这台 Xcode 27 上不生效**（2026-09-19 实测踩到）：
    # `client.yml` 写的是那个形式，CI 锁的是 Xcode 26，两边行为不一样——
    # 那条命令会悄悄编出 platform 1（macOS），不报错、不空跑提示，只是产物不对。
    # `--triple` 加 `--sdk` 这两个顶层参数才是这台机器上唯一编得出
    # `Debug-iphoneos` 那份的写法，少一个都不行。
    # 这正是坑 §6.7 那个形状：分不出两种情况的检查等于没有检查——
    # 而这一步的检查（下面那段 Mach-O 读取）就是靠它才抓到的。
    build = run([
        "swift", "build", "--product", "ERCore",
        "--triple", "arm64-apple-ios26.0",
        "--sdk", sdk_path,
        "--scratch-path", ".build/ios",
    ], cwd=CLIENT)
    build_ok = build.returncode == 0
    check("L1.1", "给 iOS 目标编译成功", build_ok,
          "" if build_ok else build.stderr[-2000:])

    if build_ok:
        obj = CLIENT / ".build/ios/out/Products/Debug-iphoneos/ERCore.o"
        platform_ok = False
        detail = "没找到编译产物"
        if obj.exists():
            otool = run(["otool", "-l", str(obj)])
            m = re.search(r"LC_BUILD_VERSION.*?platform\s+(\d+|\w+)",
                           otool.stdout, re.S)
            platform_ok = bool(m) and m.group(1) in ("2", "ios", "IOS")
            detail = f"platform={m.group(1)}" if m else "读不到平台标记"
        check("L1.2", "产物真是给 iOS 的（不是 macOS）", platform_ok, detail)
    else:
        check("L1.2", "产物真是给 iOS 的（不是 macOS）", False, "上一步没编出来，跳过")

    section("4. 契约类型重新生成一遍，和提交的逐字节比")
    gen = run(["python3", "scripts/generate_swift_types.py"], cwd=ROOT)
    gen_ok = gen.returncode == 0
    if not gen_ok:
        check("L0.3", "契约生成器跑通", False, gen.stderr[-2000:] or gen.stdout[-2000:])
    else:
        diff = run(["git", "diff", "--stat", "--", "client/Sources/ERContract"], cwd=ROOT)
        drift = diff.stdout.strip()
        check("L0.3", "生成物和提交的一字不差", not drift,
              "逐字节一致" if not drift else drift)

    section("5. 契约快照该有的字段与向量")
    spec = json.loads((CLIENT / "openapi.json").read_text(encoding="utf-8"))
    paths, schemas = spec.get("paths", {}), spec.get("components", {}).get("schemas", {})
    check("L0.4", "契约快照里有端点和类型", bool(paths) and bool(schemas),
          f"{len(paths)} 个端点、{len(schemas)} 个类型")

    body = (CLIENT / "Sources/ERContract/Types+Components+Schemas.swift").read_text(
        encoding="utf-8") if (CLIENT / "Sources/ERContract/Types+Components+Schemas.swift").exists() else ""
    # 生成器会「警告然后跳过」——跳过等于静默删字段。抽查当年真的消失过的那几个。
    missing = [f for f in ("tokens", "glossary", "preparing", "level")
               if f"public var {f}:" not in body]
    check("L0.5", "关键字段没有被生成器静默跳过", not missing,
          "" if not missing else "、".join(missing) + " 不在生成物里")

    vectors_dir = CLIENT / "Tests/ERCoreTests/Fixtures"
    review_doc = json.loads((vectors_dir / "review-vectors.json").read_text(encoding="utf-8"))
    cases = review_doc.get("cases", [])
    names = " ".join(c["name"] for c in cases)
    check("L0.6", "复习向量覆盖决定 7 那条", len(cases) >= 8 and "第二向答错" in names,
          f"{len(cases)} 个场景")

    sched_doc = json.loads((vectors_dir / "scheduler-vectors.json").read_text(encoding="utf-8"))
    grades = sched_doc["ratings"]["cases"]
    schedules = sched_doc["schedules"]["cases"]
    sched_names = " ".join(c["name"] for c in schedules)
    seq = [round(s["interval_days"]) for c in schedules
           if len(c["reviews"]) == 5 for s in c["expected"]]
    sched_ok = (len(grades) == 32
                and all(n in sched_names for n in ("180", "封顶", "提示", "自称简单", "短期"))
                and sched_doc["settings"]["enable_fuzzing"] is False
                and seq == [2, 11, 46, 163, 180])
    check("L0.7", "排期向量完整、关着抖动、间隔序列对", sched_ok,
          f"{len(grades)} 条评级、{len(schedules)} 个场景、间隔 {seq}")

    print("\n" + "=" * 62)
    print(f"本地检查：{len(passed)} 项通过，{len(failed)} 项失败")
    if failed:
        print("  失败：" + "、".join(failed))
    print("\n没复刻的：client.yml 的 core job 跑在 swift:6.3 的 Linux 容器里，"
          "「Core 在 Linux 上也编得过」这件事本地验不了，"
          "要验这个仍然得推一次去 CI。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
