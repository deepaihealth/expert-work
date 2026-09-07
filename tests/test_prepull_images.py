"""集成套件的预拉脚本(tools/ci/prepull_images.py)。

三件事各要一条能红的断言:镜像集合是**算**出来的(不是手抄)、退避真的会等、
测试代码里的镜像字面量没有漏在集合之外。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

from prepull_images import (
    BACKOFF_S,
    COMPOSE_FILE,
    COMPOSE_PROFILES,
    DOCKERFILES,
    EXTRA_REFS,
    collect_refs,
    compose_image_refs,
    dockerfile_base_refs,
    pull_with_backoff,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 2026-09-07 真实 `docker compose --profile full --profile proxy config --images`
_COMPOSE_OUTPUT = """\
edoburu/pgbouncer:v1.24.1-p1
expert-work-control-plane:dev
expert-work-control-plane:dev
infra-credential-proxy
pgvector/pgvector:pg16
public.ecr.aws/docker/library/nginx:1.27-alpine
quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z
"""

_DOCKERFILE = """\
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
RUN echo build
FROM --platform=$BUILDPLATFORM public.ecr.aws/docker/library/node:22-alpine AS web
FROM builder AS again
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm
"""


def test_compose_output_keeps_registry_refs_and_drops_locally_built_ones() -> None:
    refs = compose_image_refs(_COMPOSE_OUTPUT)

    assert "pgvector/pgvector:pg16" in refs
    assert "public.ecr.aws/docker/library/nginx:1.27-alpine" in refs
    assert "expert-work-control-plane:dev" not in refs
    assert "infra-credential-proxy" not in refs
    assert len(refs) == len(set(refs)), "重复的引用会被拉两次"


def test_dockerfile_from_lines_yield_base_images_not_stage_aliases() -> None:
    refs = dockerfile_base_refs(_DOCKERFILE)

    assert refs == [
        "ghcr.io/astral-sh/uv:python3.12-bookworm-slim",
        "public.ecr.aws/docker/library/node:22-alpine",
        "public.ecr.aws/docker/library/python:3.12-slim-bookworm",
    ]


def test_collect_merges_three_sources_without_duplicates() -> None:
    refs = collect_refs(
        compose_output=_COMPOSE_OUTPUT,
        dockerfile_texts=[_DOCKERFILE, _DOCKERFILE],
        extra=("pgvector/pgvector:pg16", "x/y:z"),
    )

    assert refs.count("pgvector/pgvector:pg16") == 1
    assert refs[-1] == "x/y:z"


class _Docker:
    """假 docker:每个镜像前 N 次 pull 失败,之后成功;记录 sleep 序列。"""

    def __init__(self, fail_times: dict[str, int], *, present: set[str] | None = None) -> None:
        self.fail_times = dict(fail_times)
        self.present = present or set()
        self.pulls: list[str] = []
        self.sleeps: list[float] = []

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0 if args[3] in self.present else 1, "", "")
        assert args[:3] == ["docker", "pull", "--quiet"], args
        ref = args[3]
        self.pulls.append(ref)
        if self.fail_times.get(ref, 0) > 0:
            self.fail_times[ref] -= 1
            return subprocess.CompletedProcess(args, 1, "", "toomanyrequests: Rate exceeded")
        return subprocess.CompletedProcess(args, 0, "", "")

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_pull_backs_off_between_retries_and_succeeds_when_the_registry_relents() -> None:
    docker = _Docker({"a/b:1": 2})

    failed = pull_with_backoff(
        ["a/b:1"], runner=docker.run, sleeper=docker.sleep, log=lambda _: None
    )

    assert failed == []
    assert docker.pulls == ["a/b:1"] * 3
    assert docker.sleeps == [BACKOFF_S[0], BACKOFF_S[1]], "退避要真的等,而且按表递增"


def test_pull_gives_up_after_the_backoff_table_is_exhausted_and_reports_it() -> None:
    docker = _Docker({"a/b:1": 99})
    lines: list[str] = []

    failed = pull_with_backoff(["a/b:1"], runner=docker.run, sleeper=docker.sleep, log=lines.append)

    assert failed == ["a/b:1"]
    assert len(docker.pulls) == len(BACKOFF_S) + 1
    assert list(docker.sleeps) == list(BACKOFF_S)
    assert any(line.startswith("FAIL") and "a/b:1" in line for line in lines), "放弃必须说出来"
    assert any("Rate exceeded" in line for line in lines), "registry 的原话要进日志"


def test_images_already_present_are_not_pulled_again() -> None:
    docker = _Docker({}, present={"a/b:1"})

    failed = pull_with_backoff(
        ["a/b:1", "c/d:2"], runner=docker.run, sleeper=docker.sleep, log=lambda _: None
    )

    assert failed == []
    assert docker.pulls == ["c/d:2"]
    assert docker.sleeps == []


_LITERAL_RE = re.compile(r"public\.ecr\.aws/[A-Za-z0-9_./-]+:[A-Za-z0-9_.-]+")


def _refs_in_repo_static() -> set[str]:
    """不靠 docker:compose 文件全部 ``image:`` + Dockerfile 全部 ``FROM`` + EXTRA。"""
    refs: set[str] = set(EXTRA_REFS)
    refs.update(
        m.group(1).strip()
        for m in re.finditer(
            r"^\s+image:\s*(\S+)", COMPOSE_FILE.read_text(encoding="utf-8"), re.MULTILINE
        )
    )
    for path in DOCKERFILES:
        refs.update(dockerfile_base_refs(path.read_text(encoding="utf-8")))
    return refs


@pytest.mark.parametrize("subtree", ["packages", "services", "tools"])
def test_every_ecr_literal_in_test_code_is_covered(subtree: str) -> None:
    """测试里直接 ``docker run`` 一个新镜像却没登记进 EXTRA_REFS —— 这里红。"""
    covered = _refs_in_repo_static()
    missing: dict[str, str] = {}
    for path in (_REPO_ROOT / subtree).rglob("*.py"):
        if "node_modules" in path.parts or (
            "test" not in path.name and "conftest" not in path.name
        ):
            continue
        for m in _LITERAL_RE.finditer(path.read_text(encoding="utf-8")):
            if m.group(0) not in covered:
                missing[m.group(0)] = str(path.relative_to(_REPO_ROOT))
    assert not missing, f"测试代码里的镜像没被预拉集合覆盖(登记进 EXTRA_REFS):{missing}"


_PROFILE_USERS = (
    "services/control-plane/tests/test_fullstack_egress_e2e.py",
    "tools/deploy/test_deploy_integration.py",
)


def test_every_profile_the_integration_tests_bring_up_is_prepulled() -> None:
    """测试换了 profile(比如 mock-upstream 挪到别的 profile)而预拉集合没跟上 ——
    这里红。2026-09-07 第一版就漏了 ``e2e``,mock-upstream 照样在 pytest 里撞限流。"""
    used: set[str] = set()
    for rel in _PROFILE_USERS:
        text = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        used.update(re.findall(r'"--profile",\s*"([a-z0-9_-]+)"', text))
    assert used, "两个测试文件里一个 --profile 都没抠到 —— 正则或文件路径过时了"
    assert used <= set(COMPOSE_PROFILES), (
        f"测试用到但没预拉的 profile:{used - set(COMPOSE_PROFILES)}"
    )
