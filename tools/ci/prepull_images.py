"""集成测试开跑前把镜像拉齐,拉不动就退避重试 —— 限流的账不记在 pytest 时钟上。

2026-09-07 一天里 integration 红了五次,日志逐字都是 ``toomanyrequests: Rate
exceeded``(nginx / mock-upstream / python 基础镜像 —— 全是 ECR Public,匿名拉取
按 runner 出口 IP 限流;X-8 把官方镜像搬离 Docker Hub 只是换了一家限流的)。
每次都是同一批 PR 在十几分钟内一起触发 CI,四五个 job 从同一个出口并发拉。

这里做三件事:
1. 把集成套件会用到的镜像**算**出来(compose ``config --images`` + 测试里会
   ``docker build`` 的 Dockerfile 的 ``FROM`` + 测试代码直接 ``docker run`` 的
   常量),不手抄清单 —— 手抄的会漂;
2. 已经在本机的跳过;其余逐个 ``docker pull``,失败按 ``BACKOFF_S`` 退避重试;
3. 最终还拉不下来的只 WARN 不拦 job —— 测试自己会带着诊断失败
   (``expert_work.testing.explain_compose_pull_failure``),那才是判定处。

配合两个 compose fixture 改成 ``pull=False``:镜像已在本机时 ``compose pull``
仍会去 registry 核对 manifest,限流一到照样失败,本地缓存等于白拉。

只在 Docker Hub 上有的 pgvector / pgbouncer 已改从自托管 GHCR mirror 拉
(``.github/workflows/mirror-images.yml``,CI 用 ``GITHUB_TOKEN`` 登录,不吃匿名
per-IP 限流)。

2026-10-16 起 ECR Public 的官方镜像也**先从 GHCR mirror 拉**,再 ``docker tag`` 成
源码里写的 ECR 名 —— 后面的 compose / ``docker build`` / ``docker run`` 认的是本地
那个名字,源码一行不改(国内构建继续走 ECR)。起因:ECR 匿名拉取开始报
``toomanyrequests: Data limit exceeded``,按出口 IP 的流量配额,一天三次 integration
假红、每次 6 轮退避全拉不动。映射从 mirror workflow 的 matrix 读,不手抄;mirror
里没有(或拉不下来)的照旧走 ECR + 退避。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "infra" / "docker-compose.yml"
#: 集成测试真正起过的 profile:test_deploy_integration 是 ``full`` + ``proxy``,
#: test_fullstack_egress_e2e 是 ``full`` + ``e2e``(mock-upstream 只在 e2e 里)。
#: 别的 profile(观测栈、langfuse、auth)没有测试碰,拉了只是多一次撞限流的
#: 机会。tests/test_prepull_images.py 从那两个测试文件里把 ``--profile`` 参数
#: 抠出来对照这里 —— 测试改了 profile 而这里没跟上,那条测试红。
COMPOSE_PROFILES: tuple[str, ...] = ("full", "proxy", "e2e")
#: 集成测试会在 runner 上 ``docker build`` 的 Dockerfile —— 它们的 ``FROM`` 也要
#: 先拉下来,否则 build 卡在拉基础镜像上(2026-09-07 沙箱镜像 build 600s 没回)。
DOCKERFILES: tuple[Path, ...] = (
    REPO_ROOT / "infra" / "sandbox-image" / "Dockerfile",
    REPO_ROOT / "services" / "control-plane" / "Dockerfile",
    REPO_ROOT / "services" / "credential-proxy" / "Dockerfile",
    REPO_ROOT / "services" / "sandbox-supervisor" / "Dockerfile",
)
#: 测试代码直接 ``docker run`` 的镜像。tests/test_prepull_images.py 断言测试里
#: 每个 ``public.ecr.aws/...`` 字面量都被 compose / Dockerfile / 这里三者之一
#: 覆盖 —— 新加一个就得在这里登记,否则那条测试红。
EXTRA_REFS: tuple[str, ...] = (
    # tools/persistence/test_restore_volume_drill.py
    "public.ecr.aws/docker/library/debian:bookworm-slim",
)
#: 每次失败后等多久再试;共 5 次重试,最坏 5m15s。ECR Public 的限流是短窗口
#: 令牌桶形态(同一条 pull 几秒后重试就成功过,#1403),不是 Docker Hub 那种
#: 六小时配额,所以退避以分钟计就够。
BACKOFF_S: tuple[int, ...] = (15, 30, 60, 90, 120)
#: GHCR mirror 的 workflow —— 官方镜像 ECR 名 → mirror 名的映射从它的 matrix 读。
MIRROR_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "mirror-images.yml"
ECR_OFFICIAL = "public.ecr.aws/docker/library/"
GHCR_MIRROR = "ghcr.io/deepaihealth/mirror/"

_FROM_RE = re.compile(r"^FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE | re.MULTILINE)

Runner = Callable[..., subprocess.CompletedProcess[str]]
Sleeper = Callable[[float], None]
Logger = Callable[[str], None]


def _dedupe(refs: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            out.append(ref)
    return out


def compose_image_refs(config_images_output: str) -> list[str]:
    """从 ``docker compose config --images`` 的输出里挑出**可拉取**的引用。

    本地 build 的服务也会出现在这份输出里(``expert-work-control-plane:dev`` /
    ``infra-credential-proxy``),它们没有 registry/namespace 段 —— 按「含 ``/``」
    区分。
    """
    return _dedupe(line.strip() for line in config_images_output.splitlines() if "/" in line)


def dockerfile_base_refs(dockerfile_text: str) -> list[str]:
    """``FROM [--platform=…] <ref> [AS name]`` 里的 ``<ref>``;``FROM builder`` 这种
    引用前一阶段别名的跳过(别名里没有 ``/`` 也没有 ``:``)。"""
    refs = [m.group(1) for m in _FROM_RE.finditer(dockerfile_text)]
    return _dedupe(ref for ref in refs if "/" in ref or ":" in ref)


def collect_refs(
    *,
    compose_output: str,
    dockerfile_texts: Sequence[str],
    extra: Sequence[str] = EXTRA_REFS,
) -> list[str]:
    refs = list(compose_image_refs(compose_output))
    for text in dockerfile_texts:
        refs.extend(dockerfile_base_refs(text))
    refs.extend(extra)
    return _dedupe(refs)


def official_mirrors(workflow_text: str) -> dict[str, str]:
    """源码里的 ECR 官方引用 → GHCR mirror 引用。

    只收 Docker 官方镜像那几格(``upstream`` 是 ``library/<name>``、没写 ``registry``);
    pgvector / minio 这类本来就直接写 ghcr 引用,不需要改名。
    """
    data = yaml.safe_load(workflow_text)
    mirrors: dict[str, str] = {}
    for entry in data["jobs"]["mirror"]["strategy"]["matrix"]["include"]:
        upstream = str(entry["upstream"])
        if entry.get("registry") or not upstream.startswith("library/"):
            continue
        tag = str(entry["tag"])
        ecr = f"{ECR_OFFICIAL}{upstream.removeprefix('library/')}:{tag}"
        mirrors[ecr] = f"{GHCR_MIRROR}{entry['name']}:{tag}"
    return mirrors


def _tail(text: str | None, n: int = 300) -> str:
    return (text or "").strip()[-n:]


def pull_via_mirror(ref: str, mirror: str, *, runner: Runner, log: Logger) -> bool:
    """从 GHCR mirror 拉 ``mirror``,再 tag 成 ``ref``;任一步失败返回 ``False``(调用方退回 ECR)。"""
    pulled = runner(["docker", "pull", "--quiet", mirror], capture_output=True, text=True)
    if pulled.returncode != 0:
        log(f"WARN  {ref} 的 GHCR mirror 拉不下来,退回 ECR: {_tail(pulled.stderr)}")
        return False
    tagged = runner(["docker", "tag", mirror, ref], capture_output=True, text=True)
    if tagged.returncode != 0:
        log(f"WARN  {mirror} → {ref} 改名失败,退回 ECR: {_tail(tagged.stderr)}")
        return False
    log(f"ok    {ref} (← {mirror})")
    return True


def pull_with_backoff(
    refs: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    sleeper: Sleeper = time.sleep,
    backoff: Sequence[int] = BACKOFF_S,
    log: Logger = print,
    mirrors: Mapping[str, str] | None = None,
) -> list[str]:
    """逐个拉;返回**最终没拉下来**的引用(调用方决定要不要拦)。

    ``mirrors`` 里有的引用先走 GHCR mirror(带凭据,不吃匿名限流),不成再走下面的
    ECR 退避。
    """
    failed: list[str] = []
    for ref in refs:
        present = runner(["docker", "image", "inspect", ref], capture_output=True, text=True)
        if present.returncode == 0:
            log(f"skip  {ref} (已在本机)")
            continue
        mirror = (mirrors or {}).get(ref)
        if mirror is not None:
            if pull_via_mirror(ref, mirror, runner=runner, log=log):
                continue
            # 退回能跑通,所以不出声就没人发现 mirror 坏了 —— 直到下一次限流。
            log(f"::warning::{ref} 没走 GHCR mirror,退回匿名 ECR(mirror 同步坏了或包没权限)")
        for attempt, delay in enumerate((0, *backoff), start=1):
            if delay:
                log(f"      {ref} 退避 {delay}s 后第 {attempt} 次")
                sleeper(delay)
            pulled = runner(["docker", "pull", "--quiet", ref], capture_output=True, text=True)
            if pulled.returncode == 0:
                log(f"ok    {ref}" + (f" (第 {attempt} 次)" if attempt > 1 else ""))
                break
            log(f"WARN  {ref} 第 {attempt} 次失败: {_tail(pulled.stderr)}")
        else:
            failed.append(ref)
            log(f"FAIL  {ref} 重试 {len(backoff)} 次仍失败 —— 交给测试自己报")
    return failed


def _compose_config_images(runner: Runner) -> str:
    args = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    for profile in COMPOSE_PROFILES:
        args += ["--profile", profile]
    args += ["config", "--images"]
    out = runner(args, capture_output=True, text=True, check=True)
    return out.stdout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dry-run", action="store_true", help="只打印会拉哪些镜像")
    ns = parser.parse_args(argv)

    refs = collect_refs(
        compose_output=_compose_config_images(subprocess.run),
        dockerfile_texts=[p.read_text(encoding="utf-8") for p in DOCKERFILES],
    )
    try:
        mirrors = official_mirrors(MIRROR_WORKFLOW.read_text(encoding="utf-8"))
    except Exception as exc:  # 这一步不许拦 job:读不出 mirror 就全走 ECR
        print(f"::warning::读不出 mirror-images.yml 的 matrix,全部走 ECR: {exc!r}")
        mirrors = {}
    print(f"== prepull: {len(refs)} 个镜像 ==")
    if ns.dry_run:
        for ref in refs:
            print(f"      {ref}" + (f"  (← {mirrors[ref]})" if ref in mirrors else ""))
        return 0
    failed = pull_with_backoff(refs, mirrors=mirrors)
    print(
        f"== prepull 完成: {len(refs) - len(failed)}/{len(refs)} 就绪"
        + (f",{len(failed)} 个交给测试" if failed else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
