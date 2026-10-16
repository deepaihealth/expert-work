"""GHCR 镜像仓 workflow(.github/workflows/mirror-images.yml)—— X-8 收官。

mirror 的 tag 手写在 workflow 的 matrix 里,compose 的 ``image:`` 也是手写的 ——
两处手写必然漂:compose 把 pgbouncer 升到 v1.25 而 mirror 没跟上,integration
会在 40 分钟后用一句 ``manifest unknown`` 告诉你。这里在 Lint 时就把两边对上。

对照用「以 ``/<name>:<tag>`` 结尾」而不是整串相等:切换前 compose 写的是
裸的 Docker Hub 名(``<上游>/pgvector:pg16``),切换后是 ``ghcr.io/deepaihealth/mirror/pgvector:pg16``,
两种状态下这条测试都得成立,否则它会在两个 PR 之间的窗口里红。

2026-10-16 起 matrix 也复制 Docker 官方镜像(源码里写 ECR 名,CI 改拉 GHCR)。它们
不一定出现在 compose 里(沙箱 / 服务 Dockerfile 的 FROM、测试直接 ``docker run`` 的),
所以对照的范围扩大到仓库里所有的镜像引用;反方向也要钉:仓库里出现的每个 ECR 官方
引用都得在 matrix 里,否则 CI 对它退回匿名 ECR,又一条迟早的假红。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "mirror-images.yml"
_COMPOSE = _REPO_ROOT / "infra" / "docker-compose.yml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_SANDBOX_DOCKERFILE = _REPO_ROOT / "infra" / "sandbox-image" / "Dockerfile"
_ECR_OFFICIAL = "public.ecr.aws/docker/library/"
_ECR_REF_RE = re.compile(r"public\.ecr\.aws/docker/library/[a-z0-9]+:[A-Za-z0-9._-]+")
#: 与 tools/ci/check_image_registry.py 同一扫描口径(后缀 + Dockerfile,跳过 worktree / 依赖)。
_SCAN_SUFFIXES = (".yml", ".yaml", ".py", ".sh")
_SKIP_PARTS = (".git", ".claude", "node_modules", ".venv", "dist", "__pycache__", ".mypy_cache")


def _workflow() -> dict[str, Any]:
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "workflow 顶层不是 mapping —— YAML 坏了"
    return data


def _matrix() -> list[dict[str, str]]:
    entries = _workflow()["jobs"]["mirror"]["strategy"]["matrix"]["include"]
    assert entries, "matrix 为空 —— workflow 什么都不 mirror"
    return list(entries)


def _compose_image_refs() -> list[str]:
    return re.findall(r"^\s+image:\s*(\S+)", _COMPOSE.read_text(encoding="utf-8"), re.MULTILINE)


def _repo_ecr_official_refs() -> set[str]:
    refs: set[str] = set()
    for path in _REPO_ROOT.rglob("*"):
        if not path.is_file() or any(part in _SKIP_PARTS for part in path.parts):
            continue
        if path.suffix not in _SCAN_SUFFIXES and not path.name.startswith("Dockerfile"):
            continue
        refs.update(_ECR_REF_RE.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return refs


def test_every_mirrored_tag_matches_a_reference_in_the_repo() -> None:
    """源码升了 tag 而 matrix 没跟上(或反过来)—— 这里红。"""
    refs = [*_compose_image_refs(), *_repo_ecr_official_refs()]
    for entry in _matrix():
        suffix = f"/{entry['name']}:{entry['tag']}"
        assert any(ref.endswith(suffix) for ref in refs), (
            f"mirror-images.yml 要复制 {entry['upstream']}:{entry['tag']},"
            f"但仓库里没有任何镜像引用以 {suffix} 结尾 —— 两边 tag 漂了,同一个 PR 改齐"
        )


def test_every_ecr_official_reference_in_the_repo_is_mirrored() -> None:
    """新加一个 ``public.ecr.aws/docker/library/…`` 引用却没进 matrix —— 这里红。"""
    mirrored = {
        f"{_ECR_OFFICIAL}{entry['upstream'].removeprefix('library/')}:{entry['tag']}"
        for entry in _matrix()
        if not entry.get("registry") and entry["upstream"].startswith("library/")
    }
    missing = sorted(_repo_ecr_official_refs() - mirrored)
    assert not missing, (
        f"这些 ECR 官方镜像没进 .github/workflows/mirror-images.yml 的 matrix:{missing} —— "
        "CI 会对它们退回匿名 ECR(10-16 `Data limit exceeded` 一天三次假红)"
    )


def test_the_ci_sandbox_prebuild_overrides_the_dockerfile_base_image() -> None:
    """沙箱 Dockerfile 换了基础镜像 tag 而 ci.yml 的 build-contexts 没跟上:覆盖静默
    失效,预构建又去匿名 ECR 解析 FROM —— 这里红。"""
    match = re.search(r"^FROM\s+(\S+)", _SANDBOX_DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert match is not None, "沙箱 Dockerfile 没有 FROM"
    base = match.group(1)
    tag = base.rsplit(":", 1)[1]
    name = base.removeprefix(_ECR_OFFICIAL).split(":", 1)[0]
    expected = f"{base}=docker-image://ghcr.io/deepaihealth/mirror/{name}:{tag}"
    assert expected in _CI.read_text(encoding="utf-8"), (
        f"ci.yml 的沙箱预构建应带 build-contexts `{expected}`"
    )


def test_the_workflow_can_actually_push_to_ghcr() -> None:
    """没有 packages: write,login 成功、imagetools create 在 push 那一步 403。"""
    assert _workflow()["permissions"]["packages"] == "write"
