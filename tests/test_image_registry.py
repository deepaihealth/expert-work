"""镜像来源卫兵(tools/ci/check_image_registry.py)—— X-8。

卫兵本身要能被证明**会咬**。重点是第二组:裸引用有三种形态(Dockerfile 的
``FROM``、compose 的 ``image:``、Python 字符串),漏掉任何一种,卫兵就只是
看起来在守。

素材里的裸引用一律**运行时拼**(``_bare()``),不写成字面量 —— 否则本文件
自己就是违规,得给卫兵开一条豁免,而每一条豁免都是真违规能藏身的地方。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

from check_image_registry import MIRROR, check

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _bare(name: str, tag: str) -> str:
    """拼一个裸引用。见模块 docstring:不能写成字面量。"""
    return f"{name}:{tag}"


def test_current_repo_pulls_every_official_image_from_the_mirror() -> None:
    """真仓库上零违规 —— 这条红 = 有人新写了裸引用(卫兵的本职)。"""
    assert check(_REPO_ROOT) == []


@pytest.mark.parametrize(
    ("filename", "template", "name", "tag"),
    [
        ("Dockerfile", "FROM {ref}\nRUN true\n", "python", "3.12-slim"),
        ("Dockerfile", "FROM --platform=$BUILDPLATFORM {ref} AS build\n", "node", "22-alpine"),
        ("docker-compose.yml", "services:\n  cache:\n    image: {ref}\n", "redis", "7-alpine"),
        ("conftest.py", 'IMAGE = "{ref}"\n', "postgres", "16-alpine"),
    ],
    ids=["dockerfile-from", "dockerfile-from-platform", "compose-image", "python-string"],
)
def test_each_bare_reference_shape_is_caught(
    tmp_path: Path, filename: str, template: str, name: str, tag: str
) -> None:
    ref = _bare(name, tag)
    (tmp_path / filename).write_text(template.format(ref=ref), encoding="utf-8")

    violations = check(tmp_path)

    assert len(violations) == 1, f"{filename} 里的 {ref} 没被逮到"
    assert f"{MIRROR}/{ref}" in violations[0], "失败信息要直接给出改成什么"


def test_the_mirrored_form_is_not_flagged(tmp_path: Path) -> None:
    """已经改对的写法不能再报 —— 否则卫兵一上线就永远红,只能被关掉。"""
    ref = _bare("python", "3.12-slim")
    (tmp_path / "Dockerfile").write_text(f"FROM {MIRROR}/{ref}\n", encoding="utf-8")

    assert check(tmp_path) == []


@pytest.mark.parametrize(
    ("repo", "tag"),
    [
        ("pgvector/pgvector", "pg16"),
        ("minio/minio", "RELEASE.2025-09-07T16-13-09Z"),
        ("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim"),
        ("quay.io/keycloak/keycloak", "25.0"),
    ],
    ids=["pgvector", "minio", "ghcr", "quay"],
)
def test_non_official_images_are_left_alone(tmp_path: Path, repo: str, tag: str) -> None:
    """非官方镜像不在管辖内:它们各有上游,ECR Public 的 ``docker/library``
    里没有对应条目(pgvector 的三个公共源都探过,都没有)。误伤会逼着人把
    卫兵关掉。"""
    body = f"services:\n  x:\n    image: {repo}:{tag}\n"
    (tmp_path / "docker-compose.yml").write_text(body, encoding="utf-8")

    assert check(tmp_path) == []


def test_worktree_copies_are_not_scanned(tmp_path: Path) -> None:
    """``.claude/worktrees/`` 下有整份仓库副本 —— 扫进去会让违规数翻十几倍,
    且那些副本不参与任何构建。"""
    nested = tmp_path / ".claude" / "worktrees" / "agent-x"
    nested.mkdir(parents=True)
    (nested / "Dockerfile").write_text(f"FROM {_bare('python', '3.12-slim')}\n", encoding="utf-8")

    assert check(tmp_path) == []
