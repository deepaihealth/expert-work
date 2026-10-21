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

import os
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
#: 这套机制自己的测试里写着示例引用,算作「仓库引用」会让 matrix 里过时的一格照样配得上。
_FIXTURE_FILES = {
    Path("tests/test_prepull_images.py"),
    Path("tests/test_mirror_images_workflow.py"),
}


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
    # 剪枝只看仓库根以下的目录名:根自己的绝对路径里有 ``.claude``(agent worktree)不算,
    # 否则一个文件都不扫、正向测试永远绿 —— check_image_registry.py 09-08 修过的同一个坑。
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS]
        for name in filenames:
            path = Path(dirpath) / name
            if path.relative_to(_REPO_ROOT) in _FIXTURE_FILES:
                continue
            if path.suffix not in _SCAN_SUFFIXES and not name.startswith("Dockerfile"):
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


def _sandbox_image_build_steps() -> list[tuple[str, dict[str, Any]]]:
    """每个 workflow 里用 build-push-action 构建 ``infra/sandbox-image`` 的步骤。"""
    steps: list[tuple[str, dict[str, Any]]] = []
    for path in sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (data.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                uses = str(step.get("uses", ""))
                with_ = step.get("with") or {}
                if uses.startswith("docker/build-push-action@") and with_.get("context") == (
                    "infra/sandbox-image"
                ):
                    steps.append((f"{path.name}:{job_name}:{step.get('name', '?')}", with_))
    return steps


def test_every_sandbox_image_build_overrides_the_dockerfile_base_image() -> None:
    """构建沙箱镜像的步骤漏了 build-contexts,FROM 就去匿名 ECR 解析 —— 10-20 / 10-21
    sandbox-image.yml 与 platform-skills.yml 一共 6 次 ``429 Too Many Requests``,
    其中两次在 push main,#1838 的镜像因此没推上 ACR。以前这里只查 ci.yml 一处;
    沙箱 Dockerfile 换基础镜像 tag 而某处 build-contexts 没跟上,同样在这里红。"""
    match = re.search(r"^FROM\s+(\S+)", _SANDBOX_DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert match is not None, "沙箱 Dockerfile 没有 FROM"
    base = match.group(1)
    tag = base.rsplit(":", 1)[1]
    name = base.removeprefix(_ECR_OFFICIAL).split(":", 1)[0]
    expected = f"{base}=docker-image://ghcr.io/deepaihealth/mirror/{name}:{tag}"
    steps = _sandbox_image_build_steps()
    covered = {where.split(":", 1)[0] for where, _ in steps}
    # 扫描本身坏了(一个步骤都没认出来)时这条会空转成绿 —— 至少要认出这两处。
    assert {"ci.yml", "sandbox-image.yml"} <= covered, f"只认出了 {sorted(covered)}"
    missing = [
        where for where, with_ in steps if expected not in str(with_.get("build-contexts", ""))
    ]
    assert not missing, f"这些构建沙箱镜像的步骤缺 build-contexts `{expected}`:{missing}"


def test_the_workflow_can_actually_push_to_ghcr() -> None:
    """没有 packages: write,login 成功、imagetools create 在 push 那一步 403。"""
    assert _workflow()["permissions"]["packages"] == "write"
