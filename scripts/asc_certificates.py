"""清点、并按 id 吊销 App Store Connect 上的签名证书。

证书 certificate / 云签名 cloud signing / 上限 cap

**为什么需要它**（2026-09-18 撞上的）:`CODE_SIGN_STYLE: Automatic` 加
`-allowProvisioningUpdates` 是云签名——证书由 Xcode 拿 API 密钥**现场申请**。
而 CI 的机器每次都是新的、钥匙串是空的，私钥不可能跟着走，
**所以每推一次构建就烧掉一张证书**。推到第 12 次，账号到顶了：

    error: Choose a certificate to revoke. Your account has reached
           the maximum number of certificates.

**这个账号是别人的。** 他借给我们走 TestFlight 内测（`docs/testflight.html`），
上面可能有他自己项目在用的证书。所以这个脚本分成两步，而且默认只做第一步：

* **清点**（默认）:只读。把每张证书的 id、类型、名字、序列号后四位、到期日
  打出来，并**由到期日反推出它是哪天建的**（Apple 的证书一年有效，
  而 API 不返回创建日期）。
* **吊销**（要 `--revoke <id>,<id>` 加 `--apply`）:逐个 id 吊销，
  **而且只吊销清点里真的存在的那些**——id 打错一个字符就拒绝，不去猜。

**绝不打印 `certificateContent`**，那是证书本体；也绝不打印密钥。
输出里只有认得出是哪一张所需的最少信息。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api.appstoreconnect.apple.com/v1"

#: 我们自己的构建是从这一天起跑的（`推 TestFlight` 第 1 次）。
#: **比这一天早的证书不是我们的**——账号在借给我们之前就存在。
OURS_FROM = datetime(2026, 9, 13, tzinfo=timezone.utc)

#: Apple 的签名证书一年有效。用它从到期日反推创建日。
VALIDITY = timedelta(days=365)


def _token() -> str:
    """一个只活 10 分钟的 ES256 JWT。密钥只在这里读，不落地、不打印。"""
    try:
        import jwt  # noqa: PLC0415 - runner 上临时装的
    except ImportError:  # pragma: no cover
        print("要先 pip install 'pyjwt[crypto]'", file=sys.stderr)
        raise

    key_id = os.environ["ASC_KEY_ID"]
    issuer = os.environ["ASC_ISSUER_ID"]
    private_key = os.environ["ASC_KEY_PEM"]
    now = int(time.time())
    return jwt.encode(
        {"iss": issuer, "iat": now, "exp": now + 600, "aud": "appstoreconnect-v1"},
        private_key,
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )


def _call(method: str, path: str, token: str) -> dict:
    request = urllib.request.Request(BASE + path, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"{method} {path} → HTTP {error.code}\n{detail}") from error


def fetch(token: str) -> list[dict]:
    """全部证书。**一次性取完**，不分页取一半就下判断。"""
    out: list[dict] = []
    path = "/certificates?limit=200"
    while path:
        page = _call("GET", path, token)
        out.extend(page.get("data", []))
        nxt = (page.get("links") or {}).get("next")
        path = nxt.replace(BASE, "") if nxt else ""
    return out


def _created(attributes: dict) -> datetime | None:
    raw = attributes.get("expirationDate")
    if not raw:
        return None
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return expires - VALIDITY


def describe(certificates: list[dict]) -> list[dict]:
    rows = []
    for item in certificates:
        attributes = item.get("attributes") or {}
        created = _created(attributes)
        serial = str(attributes.get("serialNumber") or "")
        rows.append({
            "id": item.get("id", ""),
            "type": attributes.get("certificateType") or "?",
            "name": attributes.get("displayName") or attributes.get("name") or "?",
            # 序列号只留后四位:够认出是哪一张，而整串没有必要出现在输出里。
            "serial_tail": serial[-4:] if serial else "????",
            "expires": (attributes.get("expirationDate") or "")[:10],
            "created": created.date().isoformat() if created else "不详",
            "ours": bool(created and created >= OURS_FROM),
        })
    return sorted(rows, key=lambda r: (r["created"], r["type"]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revoke", default="",
                        help="要吊销的证书 id，逗号分隔")
    parser.add_argument("--apply", action="store_true",
                        help="真的吊销。不加就只说要动哪几张")
    args = parser.parse_args()

    token = _token()
    rows = describe(fetch(token))

    print(f"账号上一共 {len(rows)} 张签名证书\n")
    print(f"{'id':<12} {'类型':<28} {'建于':<12} {'到期':<12} {'序列尾':<7} 名字")
    for row in rows:
        mark = "◀ 我们的" if row["ours"] else ""
        print(f"{row['id']:<12} {row['type']:<28} {row['created']:<12} "
              f"{row['expires']:<12} {row['serial_tail']:<7} {row['name']} {mark}")

    ours = [r for r in rows if r["ours"]]
    theirs = [r for r in rows if not r["ours"]]
    print(f"\n我们的（{OURS_FROM.date()} 起建的）{len(ours)} 张；"
          f"在那之前就有的 {len(theirs)} 张——**那些是他自己的，一张都不动**")

    if not args.revoke:
        print("\n这是只读的一趟。要吊销就传 --revoke <id>,<id> --apply")
        return 0

    wanted = [i.strip() for i in args.revoke.split(",") if i.strip()]
    by_id = {r["id"]: r for r in rows}

    unknown = [i for i in wanted if i not in by_id]
    if unknown:
        print(f"\n拒绝：这几个 id 不在上面的清单里——{unknown}。"
              "\nid 打错一个字符就可能吊销别的证书，所以这里不猜。")
        return 1

    protected = [i for i in wanted if not by_id[i]["ours"]]
    if protected:
        print(f"\n拒绝：这几张不是我们建的——"
              f"{[by_id[i]['name'] + '/' + by_id[i]['created'] for i in protected]}。"
              "\n这个账号是借来的，早于我们的证书一张都不动。")
        return 1

    print(f"\n要吊销这 {len(wanted)} 张：")
    for i in wanted:
        row = by_id[i]
        print(f"  {i}  {row['type']}  建于 {row['created']}  尾号 {row['serial_tail']}")

    if not args.apply:
        print("\n没加 --apply，什么都没做。")
        return 0

    done = 0
    for i in wanted:
        _call("DELETE", f"/certificates/{i}", token)
        print(f"  已吊销 {i}")
        done += 1

    left = describe(fetch(token))
    print(f"\n吊销了 {done} 张。账号上现在 {len(left)} 张，"
          f"其中我们的 {sum(1 for r in left if r['ours'])} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
