"""本地跑一遍 `testflight.yml` 做的事：归档、签名、（可选）传上去。

云签名 cloud signing / 证书 certificate

**为什么这份脚本存在，以及它和 CI 那条路真正的区别**（2026-09-19）：
`testflight.yml` 用的云签名（`-allowProvisioningUpdates`）在 CI 上每推一次就
烧一张证书——不是因为云签名本身有问题，是因为 **CI 的 runner 每次都是新的，
钥匙串永远是空的**，Xcode 找不到已有证书，只能问 Apple 要一张新的。本地的
钥匙串是持久的：装过一次的证书会留在这台 Mac 上，下次 Xcode 找得到就直接用，
不会再问 Apple 要。2026-09-19 实测过:本地连跑两次归档，第二次原样复用了
第一次建的那张，账号上「我们的」证书数量没有再往上涨。

**默认只导出，不上传。** 上传是外部可见的动作——号主和内测的人都会看到新
版本，所以这一步不跟着 `ci_testflight.py` 一起顺手做，要传必须显式加
`--upload`。这跟 `testflight.yml` 的默认值反过来（那边默认就传），因为这里
没有「点 Run workflow 之前先看一眼参数」那道人工关卡，脚本自己的默认值就是
唯一的关卡。

**证书与密钥不进版本控制**，放在 `data/signing/`（整个 `data/` 都在
`.gitignore` 里）。凭证一次性核对方法见 `scripts/asc_certificates.py` 和
`scripts/check_asc_key.py`。

用法::

    uv run python scripts/ci_testflight.py                 # 归档 + 本地导出
    uv run python scripts/ci_testflight.py --upload         # 归档 + 传 TestFlight
    uv run python scripts/ci_testflight.py --notes "说明文字"
"""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "client" / "App"
CREDENTIALS = ROOT / "data" / "signing" / "asc_credentials.json"


def run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fail(msg: str) -> int:
    print(f"::error:: {msg}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload", action="store_true",
                         help="真的传上 TestFlight。不加就只导出到本地，谁都看不到")
    parser.add_argument("--notes", default="", help="给测试员看的说明")
    args = parser.parse_args()

    if not CREDENTIALS.exists():
        return fail(f"缺 {CREDENTIALS.relative_to(ROOT)}——先按下面这个形状建一份：\n"
                     '  {"key_id": "...", "issuer_id": "...", "team_id": "...", '
                     '"key_path": "data/signing/AuthKey_XXXX.p8"}')
    creds = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
    key_path = ROOT / creds["key_path"]
    if not key_path.exists():
        return fail(f"凭证里写的 {creds['key_path']} 这个文件不存在")
    print(f"密钥齐了：{creds['key_id']}，团队 {creds['team_id']}")

    print("\n1. 生成工程")
    gen = run(["xcodegen", "generate"], cwd=APP_DIR)
    if gen.returncode != 0:
        return fail("xcodegen generate 失败：" + gen.stderr[-1000:])

    print("\n2. 定版本号")
    # github.run_number 在本地没有对应物；用时间戳，单调递增、不会撞。
    build_number = time.strftime("%Y%m%d%H%M")
    plist_path = APP_DIR / "Info.plist"
    with open(plist_path, "rb") as f:
        info = plistlib.load(f)
    short_version = info.get("CFBundleShortVersionString", "?")
    run(["/usr/libexec/PlistBuddy", "-c",
         f"Set :CFBundleVersion {build_number}", str(plist_path)])
    print(f"  构建号 {build_number}（版本 {short_version}）")

    auth_args = [
        "-allowProvisioningUpdates",
        "-authenticationKeyPath", str(key_path),
        "-authenticationKeyID", creds["key_id"],
        "-authenticationKeyIssuerID", creds["issuer_id"],
    ]

    print("\n3. 归档")
    archive_path = Path("/tmp/EnglishReader-testflight.xcarchive")
    archive = run([
        "xcodebuild", "archive",
        "-project", "EnglishReader.xcodeproj",
        "-scheme", "EnglishReader",
        "-destination", "generic/platform=iOS",
        "-archivePath", str(archive_path),
        *auth_args,
        f"DEVELOPMENT_TEAM={creds['team_id']}",
    ], cwd=APP_DIR, timeout=900)
    if archive.returncode != 0:
        errors = [l for l in archive.stdout.splitlines() if "error:" in l][:20]
        return fail("归档失败：\n    " + "\n    ".join(errors))
    print("  归档成功")

    app_bundle = archive_path / "Products" / "Applications" / "EnglishReader.app"
    if not app_bundle.exists():
        return fail("归档里没有 .app——上一步可能是空跑")
    otool = run(["otool", "-l", str(app_bundle / "EnglishReader")])
    m = re.search(r"LC_BUILD_VERSION.*?platform\s+(\d+|\w+)", otool.stdout, re.S)
    if not (m and m.group(1) in ("2", "ios")):
        return fail(f"产物不是给 iOS 设备的（platform={m.group(1) if m else '读不到'}）"
                     "——可能编成了模拟器包")
    print("  确认：iOS 设备包，签过名")

    print(f"\n4. {'导出并上传' if args.upload else '仅本地导出'}")
    export_options = {
        "method": "app-store-connect",
        "destination": "upload" if args.upload else "export",
        "teamID": creds["team_id"],
    }
    if args.upload:
        export_options["uploadSymbols"] = True
        export_options["manageAppVersionAndBuildNumber"] = False
    options_path = Path("/tmp/ExportOptions-testflight.plist")
    with open(options_path, "wb") as f:
        plistlib.dump(export_options, f)

    export_path = Path("/tmp/export-testflight")
    export = run([
        "xcodebuild", "-exportArchive",
        "-archivePath", str(archive_path),
        "-exportOptionsPlist", str(options_path),
        "-exportPath", str(export_path),
        *auth_args,
    ], cwd=APP_DIR, timeout=900)
    if export.returncode != 0:
        errors = [l for l in export.stdout.splitlines() if "error:" in l][:20]
        return fail(f"{'上传' if args.upload else '导出'}失败：\n    " + "\n    ".join(errors))

    if args.upload:
        print(f"\n传上去了。构建号 {build_number}，版本 {short_version}。")
        print("内部测试员不用等审核，App Store Connect 处理完（通常几分钟）TestFlight 里就能装。")
        if args.notes:
            print(f"说明：{args.notes}")
    else:
        ipa = next(export_path.glob("*.ipa"), None)
        print(f"\n导出到本地：{ipa}")
        print("没有传，号主和测试员看不到这一版。要传加 --upload。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
