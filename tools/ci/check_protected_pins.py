"""受保护钉版 CI 卫兵 —— PROD-7 / X-14 P2(e2b 事故复盘产物)。

断言 ``tools/ci/protected_pins.toml`` 里登记的每条 ``match`` 子串仍**逐字**
出现在对应文件里。钉版被改而清单没同步改 → 非零退出,失败信息带上登记的
兼容性理由与 ROADMAP 迁移项,评审想漏都难。

设计取「子串断言」而不是「解析依赖规约」:卫兵的职责只是把"这行不能悄悄
变"变成结构性事实,解析器反而引入自己的漂移面(uv/PEP 508 方言、注释、
排序)。合法升级 = 同一 PR 改依赖 + 改清单,子串断言对此零阻力。

Usage::

    uv run python tools/ci/check_protected_pins.py [--root REPO_ROOT]
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

_MANIFEST = Path("tools/ci/protected_pins.toml")


def check(root: Path) -> list[str]:
    """Return one violation message per broken pin (empty = all held)."""
    manifest_path = root / _MANIFEST
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    violations: list[str] = []
    pins = data.get("pin", [])
    if not pins:
        return [f"{_MANIFEST}: 清单为空 —— 卫兵形同虚设,至少登记既有钉版"]
    for pin in pins:
        rel = str(pin["file"])
        match = str(pin["match"])
        target = root / rel
        if not target.is_file():
            violations.append(f"{rel}: 文件不存在(清单条目失效?同 PR 更新清单)")
            continue
        if match not in target.read_text(encoding="utf-8"):
            violations.append(
                f"{rel}: 受保护钉版被改动 —— 期望逐字包含 {match}\n"
                f"    理由: {pin.get('reason', '?')}\n"
                f"    迁移: {pin.get('roadmap', '?')}\n"
                f"    合法升级 = 同一 PR 更新 tools/ci/protected_pins.toml 对应条目"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Protected-pins CI guard (PROD-7 / X-14 P2).")
    parser.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = parser.parse_args(argv)
    violations = check(Path(args.root))
    if violations:
        print("protected-pins guard FAILED:\n", file=sys.stderr)
        for v in violations:
            print(f"  {v}\n", file=sys.stderr)
        return 1
    print("protected-pins guard OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
