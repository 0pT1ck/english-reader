"""Ask an App Store Connect API key what it can do.

密钥 API key / 角色 role / 套装 ID bundle id

**为什么要这个脚本。**帮忙的那位在网页上建密钥时要选一个角色，而这件事
**选错了不会当场报错**——App Manager 级的密钥照样能传构建，只是申请不了分发
证书，于是签名要人手动导出、一年过期一次、每次都要再麻烦对方一趟。
错误会在第一次证书到期时才出现，那时已经隔了一年。

与其让人去网页上找一列表格，不如问密钥自己。三件事一次问清：

* 密钥是不是 **Admin** —— 用 ``GET /v1/users`` 探，那个端点只有 Admin 读得了
* **App 记录建了没有** —— 按套装 ID 找
* 顺带把团队里能看到的 App 都列出来，名字对不对一眼就知道

**这是一个能区分两种情况的检查**（坑 §6.7）：403 和 200 指向不同的原因，
而「上传失败」那条日志两种都长得一样。

**不装任何新依赖。**ES256 的签名走系统的 ``openssl``，因为这台机器上没有
``pyjwt``/``cryptography``，而为一次性的诊断往项目环境里塞两个包不划算。

用法::

    python scripts/check_asc_key.py --p8 路径 --key-id XXXX --issuer-id UUID
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE = "https://api.appstoreconnect.apple.com"
BUNDLE_ID = "com.optick.englishreader"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_es256(signing_input: bytes, p8: Path) -> bytes:
    """ES256 签名。

    **openssl 出来的是 DER，JWT 要的是裸的 r||s。**两者不换算的话，苹果那边
    一律回 401，而 401 看起来跟「密钥不对」一模一样——又一个分不出两种情况的
    症状，所以这一步单独写出来。
    """
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(signing_input)
        payload_path = tmp.name
    try:
        der = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(p8), payload_path],
            capture_output=True, check=True,
        ).stdout
    finally:
        Path(payload_path).unlink(missing_ok=True)

    # DER: 30 len 02 rlen r 02 slen s
    if der[0] != 0x30:
        raise SystemExit("openssl 签出来的不是 DER 序列，签名步骤有问题")
    idx = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    values = []
    for _ in range(2):
        if der[idx] != 0x02:
            raise SystemExit("DER 里找不到整数字段")
        length = der[idx + 1]
        values.append(int.from_bytes(der[idx + 2: idx + 2 + length], "big"))
        idx += 2 + length
    return b"".join(v.to_bytes(32, "big") for v in values)


def make_token(key_id: str, issuer_id: str, p8: Path) -> str:
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    now = int(time.time())
    claims = {
        "iss": issuer_id,
        "iat": now,
        # 苹果规定最长 20 分钟，超了直接 401。
        "exp": now + 600,
        "aud": "appstoreconnect-v1",
    }
    parts = [
        b64url(json.dumps(header, separators=(",", ":")).encode()),
        b64url(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(parts).encode()
    return ".".join(parts) + "." + b64url(sign_es256(signing_input, p8))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--p8", required=True, help=".p8 文件的路径")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--issuer-id", required=True)
    args = parser.parse_args()

    p8 = Path(args.p8).expanduser()
    if not p8.is_file():
        print(f"找不到密钥文件：{p8}")
        return 1

    token = make_token(args.key_id, args.issuer_id, p8)
    headers = {"Authorization": f"Bearer {token}"}
    # 这台机器出网走代理，但苹果这个域名要不要走代理由环境决定，
    # 所以让 httpx 自己读环境变量，不在这里写死。
    client = httpx.Client(headers=headers, timeout=30)

    print("=" * 56)

    # ---- 一、密钥本身通不通 ----
    apps = client.get(f"{BASE}/v1/apps", params={"limit": 200})
    if apps.status_code == 401:
        print("密钥不对：苹果回 401。")
        print("  Issuer ID、Key ID、.p8 三样必须是同一次生成的那一组。")
        return 1
    if apps.status_code != 200:
        print(f"读 App 列表失败：HTTP {apps.status_code}")
        print(apps.text[:400])
        return 1
    print("密钥能用（苹果认了这把钥匙）")

    # ---- 二、角色是不是 Admin ----
    # /v1/users 只有 Admin 读得了。App Manager、Developer 一律 403。
    users = client.get(f"{BASE}/v1/users", params={"limit": 1})
    print()
    if users.status_code == 200:
        print("角色：**Admin**  ← 正是要的")
        print("  云签名能自己申请分发证书，以后证书过期不用再找人。")
    elif users.status_code == 403:
        print("角色：**不是 Admin**（读用户列表被拒）")
        print("  传构建照样能传，但**申请不了分发证书**——")
        print("  签名要对方手动导出，而且一年过期一次，每次都得再找他。")
        print("  建议让他把这把撤销、重建一把 Admin 的。")
    else:
        print(f"角色查不出来：HTTP {users.status_code}")
        print("  **查不出来要当成没查**，别假设它是 Admin。")
        print(users.text[:300])

    # ---- 三、App 记录建了没有 ----
    data = apps.json().get("data", [])
    print()
    print(f"这个团队里能看到 {len(data)} 个 App：")
    found = None
    for app in data:
        attrs = app.get("attributes", {})
        bid = attrs.get("bundleId")
        mark = "  ← 就是它" if bid == BUNDLE_ID else ""
        print(f"  · {attrs.get('name')}  [{bid}]{mark}")
        if bid == BUNDLE_ID:
            found = app
    print()
    if found:
        print(f"App 记录已建：{found['attributes'].get('name')}")
    else:
        print(f"**App 记录还没建**——列表里没有 {BUNDLE_ID}。")
        print("  这一步要 Admin 或 App Manager，Developer 角色建不了。")

    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
