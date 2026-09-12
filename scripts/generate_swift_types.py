"""Generate the Swift contract types from `client/openapi.json`.

生成物 generated code / 无 diff no-diff

**Why the output is committed.** The alternative — generating at build time and
keeping nothing — is tidier and was rejected for one reason: I cannot read what
I do not have. A type I never see is a type I cannot reason about when the
client misbehaves, and this whole phase is written without a debugger on the
target platform. Committed output also means CI can regenerate and compare,
which turns "the backend changed and nobody noticed" into a red build.

So: run this, commit what changes, and never hand-edit the result.

**The generator is a separate tool from the runtime.** `swift-openapi-runtime`
is what the generated code depends on at build time; the generator is a program
that writes Swift. Both were verified on Windows on 2026-09-12 before any of
this was planned around them (`docs/phase-5.html` §16).

**The spec is rewritten before generating, and the reason matters.** Pydantic
writes an optional field as ``anyOf: [{…}, {"type": "null"}]``, and the
generator answers ``Schema "null" is not supported … skipping`` — then carries
on and emits a type with that field missing. Measured 2026-09-12: **70 fields
vanished this way**, including every one of an article's tokens, glossary and
body, and the only sign was a warning in a log nobody was reading. So this
script does two things about it: it rewrites those into the plain
not-in-``required`` form, which the generator does understand, and it **treats
any remaining "not supported" warning as a failure**. A tool that warns and
carries on is a tool that will delete something quietly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "client"
SPEC = CLIENT / "openapi.json"
CONFIG = CLIENT / "openapi-generator-config.yaml"
OUT = CLIENT / "Sources" / "ERContract"

#: The generator, cloned and pinned. Not a package dependency of anything:
#: SwiftPM prunes a package no target takes a product from, and the generator
#: only offers a plugin and an executable — so the dependency was resolved away
#: and `swift run swift-openapi-generator` answered "no executable product".
#: Cloning the repository sidesteps that: in its own package the executable is
#: a root product.
TOOL_DIR = ROOT / ".tooling" / "swift-openapi-generator"

#: Pinned, deliberately. An unpinned checkout means the generated Swift can
#: change under us on any Tuesday, and the no-diff check would go red for a
#: reason that has nothing to do with our contract.
GENERATOR_VERSION = "1.13.1"
GENERATOR_REPO = "https://github.com/apple/swift-openapi-generator"


def _swift_env() -> dict[str, str]:
    """The environment a Swift command needs on this machine.

    Two rules, each learned the hard way on 2026-09-12 (`phase-5.html` §16):

    * **`NO_PROXY` must be set.** Without it a request to `127.0.0.1` goes
      through the local proxy, which turns "nothing is listening" into a 503.
    * **Only the upper-case one.** Windows environment variables are
      case-insensitive, and Foundation builds a dictionary from them — two keys
      differing only in case is a fatal error before any code of ours runs.
    """
    env = dict(os.environ)
    for name in ("no_proxy", "No_Proxy", "nO_pRoXy"):
        env.pop(name, None)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    return env


def _strip_null_branches(node: Any) -> Any:
    """Rewrite Pydantic's nullable form into the one the generator supports.

    ``anyOf: [X, {"type": "null"}]`` becomes plain ``X``. The field is already
    absent from ``required``, which is how OpenAPI says "optional", and the
    generated Swift is then ``X?`` — an explicit JSON ``null`` decodes to nil
    just as a missing key does.

    What is lost is the distinction between "present and null" and "absent".
    Nothing in this contract relies on it: the server always sends the key, and
    a client that treats both as nil is right either way. What would be lost by
    *not* doing this is the field itself.
    """
    if isinstance(node, list):
        return [_strip_null_branches(v) for v in node]
    if not isinstance(node, dict):
        return node

    node = {k: _strip_null_branches(v) for k, v in node.items()}

    # **A field that was nullable must not come out required.** Dropping the
    # null branch turns "always present, may be null" into "always present,
    # never null" — and the generated Swift is then a non-optional that throws
    # on the first real null. Caught on 2026-09-12 by decoding a captured
    # response: `tags` is null for the word `an`, and the decode failed at
    # `articles[0].glossary.an.tags`. Removing it from `required` is what makes
    # the field optional, which is what it always was.
    properties = node.get("properties")
    required = node.get("required")
    if isinstance(properties, dict) and isinstance(required, list):
        nullable = {name for name, prop in properties.items()
                    if isinstance(prop, dict) and _was_nullable(prop)}
        if nullable:
            node["required"] = [name for name in required if name not in nullable]
            if not node["required"]:
                node.pop("required")

    branches = node.get("anyOf")
    if isinstance(branches, list):
        kept = [b for b in branches
                if not (isinstance(b, dict) and b.get("type") == "null")]
        if len(kept) < len(branches):
            if len(kept) == 1:
                # Splice the single surviving branch in, keeping the siblings
                # (title, description) that describe the property itself.
                siblings = {k: v for k, v in node.items() if k != "anyOf"}
                node = {**kept[0], **siblings}
                node.pop("anyOf", None)
                node[_NULLABLE_MARK] = True
            else:
                node["anyOf"] = kept
                node[_NULLABLE_MARK] = True
    return node


#: Left on a property while rewriting so the enclosing object can see that it
#: used to be nullable, then removed before the spec is written out.
_NULLABLE_MARK = "x-was-nullable"


def _was_nullable(prop: dict[str, Any]) -> bool:
    return bool(prop.get(_NULLABLE_MARK))


def _drop_marks(node: Any) -> Any:
    if isinstance(node, list):
        return [_drop_marks(v) for v in node]
    if isinstance(node, dict):
        return {k: _drop_marks(v) for k, v in node.items() if k != _NULLABLE_MARK}
    return node


def _property_count(spec: dict[str, Any]) -> int:
    return sum(len(s.get("properties") or {})
               for s in (spec.get("components", {}).get("schemas") or {}).values())


def _ensure_generator() -> None:
    """Clone the pinned generator if it is not already here.

    Kept out of version control (`.tooling/` is ignored): it is a build tool,
    not a source of this project, and a few hundred files of someone else's
    repository in our history would be noise.
    """
    if (TOOL_DIR / "Package.swift").exists():
        head = subprocess.run(
            ["git", "describe", "--tags", "--exact-match"],
            cwd=TOOL_DIR, capture_output=True, text=True,
        ).stdout.strip()
        if head == GENERATOR_VERSION:
            print(f"生成器已就位：{GENERATOR_VERSION}")
            return
        print(f"生成器版本不对（{head or '未知'}），重新取 {GENERATOR_VERSION}")
        shutil.rmtree(TOOL_DIR, ignore_errors=True)

    TOOL_DIR.parent.mkdir(parents=True, exist_ok=True)
    print(f"取生成器 {GENERATOR_VERSION}…")
    _run(
        ["git", "clone", "--depth", "1", "--branch", GENERATOR_VERSION,
         GENERATOR_REPO, str(TOOL_DIR)],
        cwd=ROOT,
    )


def _run(args: list[str], cwd: Path) -> str:
    print(f"  $ {' '.join(args[:4])} …")
    result = subprocess.run(args, cwd=cwd, env=_swift_env(), text=True,
                            capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:])
        sys.stderr.write(result.stderr[-4000:])
        raise SystemExit(f"失败（退出码 {result.returncode}）")
    return (result.stdout or "") + (result.stderr or "")


def main() -> int:
    if shutil.which("swift") is None:
        # Told apart from a real failure on purpose: a missing toolchain and
        # broken code look identical in a log otherwise, and that mistake costs
        # an hour of editing code that was never wrong (坑 §1.2).
        print("找不到 swift。工具链装在 D:\\Software\\Swift；"
              "新开的终端会自动带上 PATH 和 SDKROOT，见 CLAUDE.md。")
        return 2
    if not SPEC.exists():
        print(f"没有 {SPEC.relative_to(ROOT)}，先跑 scripts/export_contract.py")
        return 2

    _ensure_generator()

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.swift"):
        # Removed rather than overwritten: the generator emits a fixed set of
        # file names, and a file it stops emitting would otherwise sit there
        # compiling forever against a contract that no longer mentions it.
        stale.unlink()

    original = json.loads(SPEC.read_text(encoding="utf-8"))
    adapted = _drop_marks(_strip_null_branches(original))
    before, after = _property_count(original), _property_count(adapted)
    if before != after:
        raise SystemExit(f"改写把字段弄丢了：{before} -> {after}")
    adapted_path = TOOL_DIR.parent / "openapi-adapted.json"
    adapted_path.write_text(
        json.dumps(adapted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"把 {before} 个字段里的可空写法改写成生成器认得的形式")

    print(f"生成 Swift 契约类型（生成器 {GENERATOR_VERSION}）…")
    output = _run(
        ["swift", "run", "-c", "release", "swift-openapi-generator", "generate",
         str(adapted_path), "--config", str(CONFIG), "--output-directory", str(OUT)],
        cwd=TOOL_DIR,
    )

    # A tool that warns and carries on will delete something quietly. On
    # 2026-09-12 that was 70 fields, and the build stayed green.
    skipped = [line.strip() for line in output.splitlines()
               if "is not supported" in line or "skipping" in line]
    if skipped:
        print("生成器跳过了这些，等于静默删字段：")
        for line in skipped[:20]:
            print(f"  {line[:190]}")
        raise SystemExit(f"{len(skipped)} 处被跳过——契约不完整，不要提交")

    written = sorted(p.name for p in OUT.glob("*.swift"))
    total = sum(p.stat().st_size for p in OUT.glob("*.swift"))
    print(f"\n{len(written)} 个文件，{total / 1024:.0f} KB -> "
          f"{OUT.relative_to(ROOT)}")
    for name in written:
        print(f"  {name}")
    print("\n生成物要提交进仓库。改动它没有意义——CI 会重新生成并比对。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
