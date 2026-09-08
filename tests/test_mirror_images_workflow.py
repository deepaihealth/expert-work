"""GHCR 镜像仓 workflow(.github/workflows/mirror-images.yml)—— X-8 收官。

mirror 的 tag 手写在 workflow 的 matrix 里,compose 的 ``image:`` 也是手写的 ——
两处手写必然漂:compose 把 pgbouncer 升到 v1.25 而 mirror 没跟上,integration
会在 40 分钟后用一句 ``manifest unknown`` 告诉你。这里在 Lint 时就把两边对上。

对照用「以 ``/<name>:<tag>`` 结尾」而不是整串相等:切换前 compose 写的是
``pgvector/pgvector:pg16``,切换后是 ``ghcr.io/deepaihealth/mirror/pgvector:pg16``,
两种状态下这条测试都得成立,否则它会在两个 PR 之间的窗口里红。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "mirror-images.yml"
_COMPOSE = _REPO_ROOT / "infra" / "docker-compose.yml"


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


def test_every_mirrored_tag_matches_the_compose_reference() -> None:
    """compose 升了 tag 而 matrix 没跟上(或反过来)—— 这里红。"""
    refs = _compose_image_refs()
    for entry in _matrix():
        suffix = f"/{entry['name']}:{entry['tag']}"
        assert any(ref.endswith(suffix) for ref in refs), (
            f"mirror-images.yml 要复制 {entry['upstream']}:{entry['tag']},"
            f"但 infra/docker-compose.yml 里没有任何 image: 以 {suffix} 结尾 —— "
            f"两边 tag 漂了,同一个 PR 改齐"
        )


def test_the_workflow_can_actually_push_to_ghcr() -> None:
    """没有 packages: write,login 成功、imagetools create 在 push 那一步 403。"""
    assert _workflow()["permissions"]["packages"] == "write"
