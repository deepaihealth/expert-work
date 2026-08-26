"""受保护钉版卫兵(tools/ci/check_protected_pins.py)—— PROD-7 / X-14 P2。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

from check_protected_pins import check

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_current_repo_holds_every_protected_pin() -> None:
    """真仓库上全部钉版在位 —— 这条红 = 有人改了钉版没同步清单(卫兵的本职)。"""
    assert check(_REPO_ROOT) == []


def test_tampered_pin_is_caught(tmp_path: Path) -> None:
    """把 e2b 钉版抬一版(2026-08-21 事故的精确形态)—— 卫兵必须红,且失败
    信息携带理由与迁移项。"""
    root = tmp_path / "repo"
    (root / "tools" / "ci").mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "tools" / "ci" / "protected_pins.toml", root / "tools" / "ci")
    for rel in (
        "services/orchestrator/pyproject.toml",
        "services/control-plane/pyproject.toml",
        "services/sandbox-supervisor/pyproject.toml",
    ):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(_REPO_ROOT / rel, dst)

    target = root / "services" / "orchestrator" / "pyproject.toml"
    target.write_text(target.read_text().replace('"e2b==2.24.0"', '"e2b==2.39.1"'))

    violations = check(root)
    assert len(violations) == 1
    assert "e2b==2.24.0" in violations[0]
    assert "X-13" in violations[0]
    assert "protected_pins.toml" in violations[0]


def test_missing_file_is_a_violation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "tools" / "ci").mkdir(parents=True)
    shutil.copy(_REPO_ROOT / "tools" / "ci" / "protected_pins.toml", root / "tools" / "ci")
    violations = check(root)
    assert violations and all("文件不存在" in v for v in violations)
