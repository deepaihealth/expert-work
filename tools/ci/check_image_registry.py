"""镜像来源卫兵 —— X-8(Docker Hub 限流假红复盘产物)。

**规矩:Docker 官方镜像(Docker Hub 的 ``library/`` 命名空间)一律从
``public.ecr.aws/docker/library/`` 拉,不写裸名。**

为什么不是「多等一会儿」的小事:GitHub 托管 runner 出口 IP 是共享的,
Docker Hub 的匿名限流按 IP 算,所以我们的配额被全世界一起花。实测代价 ——
2026-08-20 一小时 8 条并发 run 撞限速把 integration 从 7-15 分钟拖到 24:36+
集体超时;08-27 一天四次;08-29~30 两次;08-31 又一次(PR #1398 的
integration 跑满 30 分钟,593 passed 里唯一那条 error 就是 sandbox 镜像
``docker build`` 拉基础镜像超 600s)。**每次都是假红,每次都要人去看一眼。**

ECR Public 的 ``docker/library`` 是 AWS 对 Docker 官方镜像的镜像站,同
digest、匿名可拉、不按共享 IP 掐我们。实测从国内冷拉 ``python:3.12-slim``
约 1 秒。

卫兵扫的是**源码里的引用**,不是运行时行为 —— 加一条新的 ``FROM python:...``
本身不会红任何测试(它照样能构建),只会在某个繁忙的早上变成又一条假红。
所以要有这一道。

例外只有 ``pgvector/pgvector``:它不是官方镜像,ECR Public / GHCR / quay
都没有第二个公共源,只能留在 Docker Hub(见 ROADMAP X-8 余项)。非官方
镜像(``minio/`` ``grafana/`` ``prom/`` ``searxng/`` ``edoburu/``
``clickhouse/`` ``langfuse/`` 等)不在本卫兵管辖内 —— 它们各有各的上游,
统一搬运是另一件事。

Usage::

    uv run python tools/ci/check_image_registry.py [--root REPO_ROOT]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: 官方镜像该走的前缀。
MIRROR = "public.ecr.aws/docker/library"

#: 我们实际用到的官方镜像名。刻意是白名单而不是「任何看起来像官方镜像的
#: 名字」—— 后者要靠猜「有没有斜杠」,而 ``minio/minio`` 与 ``python`` 的
#: 区别不该由一条正则的运气来判。用到新的官方镜像时,同一个 PR 加进来。
OFFICIAL = ("python", "node", "nginx", "postgres", "redis", "alpine", "busybox", "debian")

#: 扫这些后缀;``.md`` 刻意不扫 —— 文档里的示例片段不拉镜像,把它们一起
#: 改会让这条卫兵变成文风检查。
SUFFIXES = (".yml", ".yaml", ".py", ".sh")
DOCKERFILE_NAMES = ("Dockerfile",)

#: 不扫:worktree 副本、依赖、构建产物。
SKIP_PARTS = (".git", ".claude", "node_modules", ".venv", "dist", "__pycache__", ".mypy_cache")

# 裸引用的三种写法:Dockerfile 的 FROM、compose 的 image:、Python 字符串。
_PATTERNS = (
    re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(?P<ref>(?P<name>[a-z0-9]+):[\w.\-]+)", re.M),
    re.compile(r"^\s*image:\s*[\"']?(?P<ref>(?P<name>[a-z0-9]+):[\w.\-]+)", re.M),
    re.compile(r"[\"'](?P<ref>(?P<name>[a-z0-9]+):[\w.\-]+)[\"']"),
)


def _files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.suffix in SUFFIXES or path.name in DOCKERFILE_NAMES:
            out.append(path)
    return sorted(out)


def check(root: Path) -> list[str]:
    """Return one violation per bare Docker-Hub official-image reference."""
    violations: list[str] = []
    for path in _files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in _PATTERNS:
            for m in pattern.finditer(text):
                if m.group("name") not in OFFICIAL:
                    continue
                line = text[: m.start()].count("\n") + 1
                rel = path.relative_to(root)
                violations.append(
                    f"{rel}:{line}: 官方镜像写成了裸名 {m.group('ref')} —— "
                    f"会从 Docker Hub 拉,共享 runner IP 上按小时撞限流。\n"
                    f"    改成: {MIRROR}/{m.group('ref')}"
                )
    return sorted(set(violations))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    violations = check(args.root.resolve())
    if violations:
        print("镜像来源卫兵:发现裸引用的 Docker 官方镜像\n", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
