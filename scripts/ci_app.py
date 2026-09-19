"""本地跑一遍 `app.yml` 做的事：App 编得过、装得上、起得来、不闪退。

模拟器 simulator / 闪退 crash

**为什么这份脚本存在**：同 `ci_client.py`，`app.yml` 存在的理由是「本机没有
Mac」——现在有了，模拟器和 Xcode 就在这台机器上，不用每次改完界面都推一次、
等 CI 排队编译再看结果。

**四步和 app.yml 完全对应**：生成工程、给 iPhone 17 模拟器编译、装上启动、
截图看一眼。**不接管道、不写 `|| 兜底`**——理由是坑 §6.6 那次：xcodebuild
死了、后面的命令返回 0，job 绿了 37 秒一行没编译。这里同样每一步都单独判断
returncode，不让错误被中间任何一层吞掉。

用法::

    uv run python scripts/ci_app.py            # 编译、装、启动、截图
    uv run python scripts/ci_app.py --no-launch  # 只编译，不占用模拟器
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "client" / "App"
DEVICE_NAME = "iPhone 17"
BUNDLE_ID = "com.optick.englishreader"

passed: list[str] = []
failed: list[str] = []


def check(item: str, title: str, ok: bool, detail: str = "") -> bool:
    line = f"  [{'通过' if ok else '失败'}] {item} {title}"
    if detail:
        line += f" — {detail}"
    print(line)
    (passed if ok else failed).append(item)
    return ok


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def find_device_udid() -> str | None:
    out = run(["xcrun", "simctl", "list", "devices", "available"]).stdout
    for line in out.splitlines():
        # 「iPhone 17 (」后面紧跟左括号，不会误中 iPhone 17 Pro。
        if f"{DEVICE_NAME} (" in line:
            m = re.search(r"\(([0-9A-Fa-f-]{36})\)", line)
            if m:
                return m.group(1)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-launch", action="store_true",
                         help="只编译，不装进模拟器、不启动")
    args = parser.parse_args()

    print("=" * 62)
    print("本地版 app.yml")
    print("=" * 62)

    print("\n1. 生成工程")
    gen = run(["xcodegen", "generate"], cwd=APP_DIR)
    check("A1", "xcodegen generate", gen.returncode == 0,
          "" if gen.returncode == 0 else gen.stderr[-1000:])
    if gen.returncode != 0:
        print(f"\n{len(passed)} 项通过，{len(failed)} 项失败")
        return 1

    print("\n2. 编译（iPhone 17 模拟器）")
    build = run([
        "xcodebuild", "build",
        "-project", "EnglishReader.xcodeproj",
        "-scheme", "EnglishReader",
        "-destination", f"platform=iOS Simulator,name={DEVICE_NAME}",
        "-derivedDataPath", "DerivedData",
        "CODE_SIGNING_ALLOWED=NO",
    ], cwd=APP_DIR, timeout=600)
    build_ok = build.returncode == 0
    if not build_ok:
        errors = sorted(set(
            line for line in build.stdout.splitlines() if "error:" in line))[:20]
        check("A2", "编译成功", False, "\n    " + "\n    ".join(errors))
        print(f"\n{len(passed)} 项通过，{len(failed)} 项失败")
        return 1
    check("A2", "编译成功", True)

    app_path = next((APP_DIR / "DerivedData").rglob("EnglishReader.app"), None)
    check("A3", "产物在，且是模拟器的", app_path is not None,
          str(app_path) if app_path else "没找到 EnglishReader.app——上一步可能是空跑")
    if not app_path:
        return 1
    binary = app_path / "EnglishReader"
    otool = run(["otool", "-l", str(binary)])
    m = re.search(r"LC_BUILD_VERSION.*?platform\s+(\d+|\w+)", otool.stdout, re.S)
    sim_ok = bool(m) and m.group(1) in ("7", "iossimulator")
    check("A3.1", "Mach-O 平台标记是 iOS 模拟器", sim_ok,
          f"platform={m.group(1)}" if m else "读不到平台标记")

    if args.no_launch:
        print(f"\n--no-launch，跳过装机与启动")
        print(f"\n{len(passed)} 项通过，{len(failed)} 项失败")
        return 1 if failed else 0

    print("\n3. 装进模拟器并启动")
    udid = find_device_udid()
    check("A4", f"镜像里有 {DEVICE_NAME} 模拟器", udid is not None, udid or "")
    if not udid:
        return 1

    run(["xcrun", "simctl", "boot", udid])
    run(["xcrun", "simctl", "bootstatus", udid, "-b"], timeout=120)
    install = run(["xcrun", "simctl", "install", udid, str(app_path)])
    check("A4.1", "装得上", install.returncode == 0, install.stderr[-500:])

    launch = run(["xcrun", "simctl", "launch", udid, BUNDLE_ID])
    launch_ok = launch.returncode == 0 and ":" in launch.stdout
    pid = launch.stdout.strip().rsplit(":", 1)[-1].strip() if launch_ok else ""
    check("A4.2", "起得来", launch_ok, launch.stdout.strip() or launch.stderr[-500:])

    if launch_ok:
        time.sleep(6)
        alive = subprocess.run(["ps", "-p", pid], capture_output=True).returncode == 0
        check("A4.3", "起来之后没有立刻崩掉", alive,
              "" if alive else f"pid {pid} 已经不在了")

        # 一次性诊断产物，放临时目录——不进仓库目录。
        shot = Path("/tmp/ci_app_launch.png")
        run(["xcrun", "simctl", "io", udid, "screenshot", str(shot)])
        check("A4.4", "截图存了一张", shot.exists(), str(shot) if shot.exists() else "")

    print("\n" + "=" * 62)
    print(f"本地检查：{len(passed)} 项通过，{len(failed)} 项失败")
    if failed:
        print("  失败：" + "、".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
