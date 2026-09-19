"""Export the client contract and a few real responses.

契约快照 contract snapshot / 样本 fixture / 无 diff no-diff

Two products, and **only one of them is checked for drift**:

* ``client/openapi.json`` — the client half of the OpenAPI document, derived
  entirely from the code. Re-running this must produce no diff, and CI says so.
  The Swift types are generated from this file rather than from a live server,
  so generation works on a machine with no database.
* ``client/Tests/ERCoreTests/Fixtures/*.json`` — real responses, captured
  whole, and **frozen**. Written only when asked for with ``--fixtures``.
  坑 §6.1 is why they are captured rather than written by hand: guessing the
  shape of data someone else produced is how ``target_words`` ended up parsed
  by comma, and how an article read perfectly while recording nothing.

**Why fixtures are not in the no-diff check.** They capture whichever articles
the library happened to hold, so tomorrow's export would pick different ones
and the check would go red for a reason that has nothing to do with the
contract — 坑 §4.1, a check that only passes on some days is not a check.
Re-capturing is a decision, not a verification.

**Why only the client half.** The admin surface is 60-odd endpoints the phone
will never call, and every one of them would become Swift types nobody uses.

**Nothing in the output may carry a timestamp or an id that moves.** The whole
value of these files is that re-exporting them produces no diff; anything that
changes per run turns the check into noise, and a noisy check gets deleted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC_OUT = ROOT / "client" / "openapi.json"
FIXTURE_DIR = ROOT / "client" / "Tests" / "ERCoreTests" / "Fixtures"

CLIENT_PREFIX = "/v1/client"


def _client_spec(app) -> dict[str, Any]:
    """The OpenAPI document with only the client surface left in it."""
    spec = app.openapi()
    paths = {p: ops for p, ops in spec["paths"].items() if p.startswith(CLIENT_PREFIX)}

    # Keep only the schemas those paths can reach. A whole-document copy would
    # drag in every admin model, and the generated Swift would carry types for
    # endpoints a phone can never call.
    wanted: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[-1]
                if name not in wanted:
                    wanted.add(name)
                    visit(spec["components"]["schemas"].get(name, {}))
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(paths)
    return {
        "openapi": spec["openapi"],
        "info": {
            "title": "English Reader 客户端接口",
            "version": spec["info"]["version"],
            "description":
                "只含 /v1/client。由 scripts/export_contract.py 导出，不要手改。\n\n"
                "**只增不减**（架构铁律 5）：可以加字段，不能删字段或改字段含义。"
                "侧载安装的客户端更新滞后，手机上大概率长期跑着旧版本。\n\n"
                "**capabilities 区分「没数据」和「没实现」**：一个 null 字段本身是歧义的，"
                "`root_known: null` 可能是「这个词没有词根」，也可能是「水平估计还没做」，"
                "而客户端要把这两种画成不同的样子。\n\n"
                "**幂等键重复上报算正常，不算错误**：离线客户端会把拿不准的都重发一遍。\n\n"
                "**今日包不含真题**：真题走 /library?source=cet4|cet6|kaoyan。",
        },
        "paths": paths,
        "components": {
            "schemas": {
                name: spec["components"]["schemas"][name]
                for name in sorted(wanted)
                if name in spec["components"]["schemas"]
            }
        },
    }


def _scrub(value: Any) -> Any:
    """Blank the fields that move between runs.

    Timestamps and progress would make every export differ, and a file that
    always differs is one nobody diffs. The shape is what the fixture is for;
    the exact instant is not.
    """
    moving = {
        "prepared_at", "read_at", "created_at", "updated_at", "received_at",
        "processed_at", "occurred_at", "started_at", "finished_at", "spelling_at",
        "simulated_now", "real_now", "due_at", "asked_at", "day",
    }
    if isinstance(value, dict):
        # **Only strings get blanked.** Some of these names are also English
        # words, and the glossary is keyed by headword — so a naive match
        # replaced the whole entry for `day` with a placeholder and the fixture
        # stopped decoding. Caught 2026-09-12 by the Swift decode test, which
        # is the entire reason that test reads a real capture.
        return {
            k: ("<时间已抹去>" if k in moving and isinstance(v, str) and v else _scrub(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def main(argv: list[str]) -> int:
    want_fixtures = "--fixtures" in argv

    from backend.core import auth
    from backend.core.db import get_connection
    from backend.main import create_app
    from fastapi.testclient import TestClient

    app = create_app()
    client = TestClient(app)
    conn = get_connection("ops")

    SPEC_OUT.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    spec = _client_spec(app)
    # `newline="\n"` is not fussiness. Python's text mode turns \n into \r\n on
    # Windows while `.gitattributes` asks for LF, so without it every export
    # leaves a file that differs from the committed one in line endings alone —
    # and the whole point of this file is that re-exporting it produces no diff.
    # Two sources of truth disagreeing is how a real change goes unnoticed.
    SPEC_OUT.write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"契约 -> {SPEC_OUT.relative_to(ROOT)}"
          f"（{len(spec['paths'])} 个端点，{len(spec['components']['schemas'])} 个类型）")
    for path in sorted(spec["paths"]):
        for method in spec["paths"][path]:
            print(f"  {method.upper():5s} {path}")

    if not want_fixtures:
        print("样本没有重新抓取。要重抓加 --fixtures——"
              "它们是冻结的样本，重抓是一个决定，不是一次校验。")
        return 0

    conn.execute("DELETE FROM devices WHERE name = '契约导出'")
    conn.commit()
    token = auth.create_device("契约导出")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        # No separate article capture: `today.json` already carries three whole
        # ones, so a fourth would be the same shape again for another half a
        # megabyte.
        captures = [
            ("today.json", "/v1/client/today"),
            ("library.json", "/v1/client/library?shelf=fresh"),
            ("reviews.json", "/v1/client/reviews"),
        ]
        for name, url in captures:
            response = client.get(url, headers=headers)
            if response.status_code != 200:
                print(f"  跳过 {name}：HTTP {response.status_code}")
                continue
            body = _scrub(response.json())
            out = FIXTURE_DIR / name
            # Compact, unlike the spec: these are data, and indenting a megabyte
            # of JSON doubles it for a diff nobody is going to read anyway.
            out.write_text(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8", newline="\n",
            )
            print(f"样本 -> {out.relative_to(ROOT)}（{out.stat().st_size / 1024:.0f} KB）")
    finally:
        conn.execute("DELETE FROM devices WHERE name = '契约导出'")
        conn.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
