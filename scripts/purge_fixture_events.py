"""一次性：把已经积在会合点里的验收夹具清掉。

**为什么要有这个脚本，而不是只让 `verify_phase2` 自己清。**
那个清理是 2026-09-18 才加的，而夹具从 2026-09-07 就在往里攒——
`verify_phase2` 清得掉它自己那几种形状，清不掉早年一次性探针留下的
（`k-*`、`repro*`、`rv-*`，它们没有主人）。存量要单独走一趟。

规则在 `fixture_events.py`，和验收脚本共用一份:**删事件，
而派生出来的标记只有在没有任何真事件支持它时才删**——
`verify_phase2` 标的是真文章里的一个真词，无条件删会删掉用户自己标的那一笔。

跑之前会自己备份 `events.db`。**只读要先看清楚**:不带 `--apply` 就只报告。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def main() -> int:
    from backend.core.db import backup_database, get_connection
    from backend.main import create_app
    import fixture_events

    create_app()
    conn = get_connection("events")

    before = fixture_events.survey(conn)
    print(f"会合点 {before['total']} 条事件，其中夹具 {before['fixtures']} 条，"
          f"**会被设备收下的 {before['pullable']} 条**")
    if not before["fixtures"]:
        print("没有夹具，什么都不用做")
        return 0

    if "--apply" not in sys.argv:
        print("\n这是只读的一趟。要真的删，加 --apply")
        return 0

    snapshot = backup_database("events", "before-fixture-purge")
    print(f"先备份到 {snapshot.name}")

    result = fixture_events.purge(conn)
    print(f"删掉事件 {result['removed']} 条，还剩 {result['leftover']} 条")
    print(f"派生的标记删了 {len(result['marks_dropped'])} 个"
          f"{'：' + '、'.join(result['marks_dropped']) if result['marks_dropped'] else ''}")
    print(f"**留下 {len(result['marks_kept'])} 个有真事件支持的**"
          f"{'：' + '、'.join(result['marks_kept']) if result['marks_kept'] else ''}")

    after = fixture_events.survey(conn)
    print(f"\n现在：{after['total']} 条事件，夹具 {after['fixtures']} 条，"
          f"会被设备收下的夹具 {after['pullable']} 条")
    return 0 if after["fixtures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
