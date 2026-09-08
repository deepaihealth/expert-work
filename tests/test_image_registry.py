"""镜像来源卫兵(tools/ci/check_image_registry.py)—— X-8。

卫兵本身要能被证明**会咬**。重点是第二组:裸引用有三种形态(Dockerfile 的
``FROM``、compose 的 ``image:``、Python 字符串),漏掉任何一种,卫兵就只是
看起来在守。第二条规矩(只在 Docker Hub 上有、已镜像到 GHCR 的两个镜像)
再多一种形态:``docker ps --filter ancestor=...`` 那种不带引号紧贴的写法。

素材里的裸引用一律**运行时拼**(``_bare()``),不写成字面量 —— 否则本文件
自己就是违规,得给卫兵开一条豁免,而每一条豁免都是真违规能藏身的地方。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

from check_image_registry import MIRROR, MIRRORED, check

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
        ("minio/minio", "RELEASE.2025-09-07T16-13-09Z"),
        ("ghcr.io/astral-sh/uv", "python3.12-bookworm-slim"),
        ("quay.io/keycloak/keycloak", "25.0"),
    ],
    ids=["minio", "ghcr", "quay"],
)
def test_non_official_images_are_left_alone(tmp_path: Path, repo: str, tag: str) -> None:
    """非官方镜像不在管辖内:它们各有上游,ECR Public 的 ``docker/library``
    里没有对应条目。误伤会逼着人把卫兵关掉。"""
    body = f"services:\n  x:\n    image: {repo}:{tag}\n"
    (tmp_path / "docker-compose.yml").write_text(body, encoding="utf-8")

    assert check(tmp_path) == []


@pytest.mark.parametrize(
    ("name", "tag"),
    [("pgvector/pgvector", "pg16"), ("edoburu/pgbouncer", "v1.24.1-p1")],
    ids=["pgvector", "pgbouncer"],
)
@pytest.mark.parametrize(
    ("filename", "template"),
    [
        ("docker-compose.yml", "services:\n  db:\n    image: {ref}\n"),
        ("conftest.py", 'container = PostgresContainer("{ref}")\n'),
        ("drill.py", '    "ancestor={ref}",\n'),
        ("docker-compose.yml", "services:\n  db:\n    image: docker.io/{ref}\n"),
    ],
    ids=["compose-image", "python-string", "docker-ps-filter", "docker.io-prefixed"],
)
def test_bare_reference_to_a_ghcr_mirrored_image_is_caught(
    tmp_path: Path, filename: str, template: str, name: str, tag: str
) -> None:
    """只在 Docker Hub 上有的两个镜像已镜像到 GHCR(mirror-images.yml),裸名
    = 又回去匿名拉 Docker Hub。四种形态各一,含 ``docker.io/`` 全称那种绕法。"""
    ref = _bare(name, tag)
    (tmp_path / filename).write_text(template.format(ref=ref), encoding="utf-8")

    violations = check(tmp_path)

    assert len(violations) == 1, f"{filename} 里的 {ref} 没被逮到"
    assert f"{MIRRORED[name]}:{tag}" in violations[0], "失败信息要直接给出改成什么"


def test_the_ghcr_mirror_form_is_not_flagged(tmp_path: Path) -> None:
    """改对的写法不能再报 —— 两个镜像的 mirror 引用各放一条。"""
    body = "services:\n" + "".join(
        f"  s{i}:\n    image: {target}:pg16\n" for i, target in enumerate(MIRRORED.values())
    )
    (tmp_path / "docker-compose.yml").write_text(body, encoding="utf-8")

    assert check(tmp_path) == []


def test_worktree_copies_are_not_scanned(tmp_path: Path) -> None:
    """``.claude/worktrees/`` 下有整份仓库副本 —— 扫进去会让违规数翻十几倍,
    且那些副本不参与任何构建。"""
    nested = tmp_path / ".claude" / "worktrees" / "agent-x"
    nested.mkdir(parents=True)
    (nested / "Dockerfile").write_text(f"FROM {_bare('python', '3.12-slim')}\n", encoding="utf-8")

    assert check(tmp_path) == []


def test_a_checkout_that_itself_lives_under_a_skipped_name_is_still_scanned(
    tmp_path: Path,
) -> None:
    """跳过规则只看 root **以下**的路径段。root 自己在 ``.claude/worktrees/`` 里
    (agent worktree 就是)时,原实现把每个文件都跳掉,卫兵在那种 checkout 里
    永远绿 —— 2026-09-08 变异自证时逮到的。"""
    root = tmp_path / ".claude" / "worktrees" / "agent-x"
    root.mkdir(parents=True)
    (root / "Dockerfile").write_text(f"FROM {_bare('python', '3.12-slim')}\n", encoding="utf-8")

    assert len(check(root)) == 1
