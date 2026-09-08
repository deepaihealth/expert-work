"""镜像来源卫兵 —— X-8(Docker Hub 限流假红复盘产物)。

**规矩:Docker 官方镜像(Docker Hub 的 ``library/`` 命名空间)一律从
``public.ecr.aws/docker/library/`` 拉,不写裸名。**

为什么不是「多等一会儿」的小事:GitHub 托管 runner 出口 IP 是共享的,
Docker Hub 的匿名限流按 IP 算,所以我们的配额被全世界一起花。实测代价 ——
2026-08-20 一小时 8 条并发 run 撞限速把 integration 从 7-15 分钟拖到 24:36+
集体超时;08-27 一天四次;08-29~30 两次;08-31 又一次(PR #1398 的
integration 跑满 30 分钟,593 passed 里唯一那条 error 就是 sandbox 镜像
``docker build`` 拉基础镜像超 600s)。**每次都是假红,每次都要人去看一眼。**

ECR Public 的 ``docker/library`` 是 AWS 对 Docker 官方镜像的镜像站,同 digest、
匿名可拉。实测从国内冷拉 ``python:3.12-slim`` 约 1 秒(Docker Hub 是 KB/s)——
**这条规矩真正稳的收益是构建速度,尤其 release.sh 从国内跑的时候。**

**订正(2026-08-31 当天,写下这条规矩几小时后)**:最初这里写的是「不按共享 IP
掐我们」—— **错的**。ECR Public 的匿名拉取同样按 IP 限流:同一天 CI 日志逐字给出
``mock-upstream Pulling`` → ``toomanyrequests: Rate exceeded``,而 mock-upstream
正是本规矩把 ``python:3.12-alpine`` 换过去的那个服务。所以换源**没有**解决 CI 的
假红,只是把限流方从 Docker Hub 换成了 AWS。CI 的真解是带凭据或自建镜像仓
(见 ROADMAP X-8 余项),不是换一个匿名公共源。

卫兵扫的是**源码里的引用**,不是运行时行为 —— 加一条新的 ``FROM python:...``
本身不会红任何测试(它照样能构建),只会在某个繁忙的早上变成又一条假红。
所以要有这一道。

**第二条规矩(X-8 收官)**:只在 Docker Hub 上有、没有第二个公共源的
``pgvector/pgvector`` 与 ``edoburu/pgbouncer``,一律写
``ghcr.io/deepaihealth/mirror/<name>`` —— ``.github/workflows/mirror-images.yml``
每周把上游 manifest 原样复制过去,CI 用 ``GITHUB_TOKEN`` 拉。这两个名字的
裸引用(含 ``docker.io/`` 全称)同样违规;唯一合法提到上游的地方是那个
workflow,它把名字和 tag 拆成两个字段在运行时拼,所以卫兵仍然零豁免。

其余非官方镜像(``minio/`` ``grafana/`` ``prom/`` ``searxng/``
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

#: 只在 Docker Hub 上有、已自建 GHCR 镜像的镜像:Docker Hub 名 → 该走的引用
#: (不含 tag)。改这里要同步 .github/workflows/mirror-images.yml 的 matrix。
MIRRORED = {
    "pgvector/pgvector": "ghcr.io/deepaihealth/mirror/pgvector",
    "edoburu/pgbouncer": "ghcr.io/deepaihealth/mirror/pgbouncer",
}

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

# 已镜像到 GHCR 的两个名字:任何 ``name:tag`` 形态都算(compose 的 image:、
# Python 字符串、``docker ps --filter ancestor=name:tag`` 那种不带引号紧贴的),
# 含 ``docker.io/`` 全称。前面不能是路径字符 —— 那是别的 registry 下的同名段。
_MIRRORED_PATTERN = re.compile(
    r"(?<![\w.\-/])(?P<ref>(?:docker\.io/)?(?P<name>"
    + "|".join(re.escape(name) for name in MIRRORED)
    + r"):(?P<tag>[\w.\-]+))"
)


def _files(root: Path) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # 只看 root 以下的路径段:root 自己的绝对路径里出现 ``.claude`` / ``dist``
        # 之类(agent worktree 就在 ``.claude/worktrees/`` 下)不算 —— 否则卫兵
        # 在那种 checkout 里一个文件都不扫、永远绿(2026-09-08 变异自证时逮到)。
        if any(part in SKIP_PARTS for part in path.relative_to(root).parts):
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
        for m in _MIRRORED_PATTERN.finditer(text):
            line = text[: m.start()].count("\n") + 1
            rel = path.relative_to(root)
            violations.append(
                f"{rel}:{line}: {m.group('ref')} 直接从 Docker Hub 拉 —— 它已镜像到 GHCR"
                f"(.github/workflows/mirror-images.yml),裸名又回到匿名 per-IP 限流。\n"
                f"    改成: {MIRRORED[m.group('name')]}:{m.group('tag')}"
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
