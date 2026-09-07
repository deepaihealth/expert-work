"""``explain_compose_pull_failure`` —— X-8 余项的诊断助手。

这个 helper 存在的唯一理由,就是让「compose pull 挂了」这句话带上**是哪个镜像**。
所以它自己必须被证明真的会把 stderr 吐出来 —— 否则下次照样只剩一句
「returned non-zero exit status 1」,而我们以为有诊断了。
"""

from __future__ import annotations

import subprocess
from typing import Any

from expert_work.testing import explain_compose_pull_failure


def _runner(*, returncode: int, stdout: str = "", stderr: str = "") -> Any:
    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    return run


def test_a_failed_pull_surfaces_the_registry_error() -> None:
    """限流那句话必须原样出现在结果里 —— 这才是整个 helper 的目的。"""
    err = (
        "pgvector/pgvector:pg16: toomanyrequests: You have reached your unauthenticated "
        "pull rate limit. https://www.docker.com/increase-rate-limit"
    )

    out = explain_compose_pull_failure("/infra", runner=_runner(returncode=1, stderr=err))

    assert "toomanyrequests" in out
    assert "pgvector/pgvector:pg16" in out
    assert "exit=1" in out


def test_a_succeeding_rerun_says_the_previous_failure_was_transient() -> None:
    """重跑成功本身是结论,不是「没查出来」——别让它显示成空白。"""
    out = explain_compose_pull_failure("/infra", runner=_runner(returncode=0))

    assert "偶发" in out


def test_the_helper_never_raises_on_top_of_the_original_failure() -> None:
    """它跑在一个已经失败的测试的收尾路径上。自己再抛一次只会盖掉原始错误。"""

    def boom(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd="docker", timeout=600)

    out = explain_compose_pull_failure("/infra", runner=boom)

    assert "重跑 pull 也失败了" in out


def test_long_output_is_truncated_not_dumped_whole() -> None:
    """CI 日志里贴一兆 pull 进度条,等于把真正的报错埋掉。"""
    out = explain_compose_pull_failure(
        "/infra", runner=_runner(returncode=1, stdout="x" * 10_000, stderr="y" * 10_000)
    )

    assert len(out) < 4_000
    # 截的是**尾部** —— 报错在最后,进度条在前面。
    assert out.endswith("y" * 100)
