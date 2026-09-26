"""Layer 2 (spec §6.2): run in_image/case_*.py inside the real sandbox image.

Skipped unless EXPERT_WORK_SKILLS_TEST_IMAGE is set. Mirrors production
sandbox conditions: read-only root, no network, writable /workspace, /tmp,
/home/agent; skills materialized under /opt/skills/<name>/.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

IMAGE = os.environ.get("EXPERT_WORK_SKILLS_TEST_IMAGE", "")
CASES_DIR = Path(__file__).parent / "in_image"
CASES = sorted(p.name for p in CASES_DIR.glob("case_*.py"))

pytestmark = pytest.mark.skipif(
    not IMAGE, reason="EXPERT_WORK_SKILLS_TEST_IMAGE not set (layer-2 tests)"
)


@pytest.mark.parametrize("case", CASES)
def test_case_in_sandbox_image(case: str, unpacked_skills: Path) -> None:
    docker = shutil.which("docker")
    assert docker, "docker not found"
    cmd = [
        docker,
        "run",
        "--rm",
        "--platform",
        "linux/amd64",
        "--read-only",
        "--network",
        "none",
        "--tmpfs",
        "/tmp:rw,size=512m,mode=1777",  # noqa: S108 — docker --tmpfs mount spec, not a temp-file usage
        "--tmpfs",
        "/home/agent:rw,size=256m,mode=1777",
        "--tmpfs",
        "/workspace:rw,size=512m,mode=1777",
        "-v",
        f"{unpacked_skills}:/opt/skills:ro",
        "-v",
        f"{CASES_DIR}:/opt/cases:ro",
        "-e",
        "EXPERT_WORK_SKILLS_DIR=/opt/skills",
        "-e",
        "PYTHONPATH=/opt/cases",
        "-w",
        "/workspace",
        "--entrypoint",
        "python",
        IMAGE,
        f"/opt/cases/{case}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, check=False)  # noqa: S603
    assert proc.returncode == 0, f"{case}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert f"PASS {case.removesuffix('.py')}" in proc.stdout
