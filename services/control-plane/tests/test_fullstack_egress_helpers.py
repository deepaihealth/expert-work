"""``test_fullstack_egress_e2e`` 的 docker helper 单元测试(无 Docker 依赖)。

被测对象是那个 e2e 模块里的 ``_docker``:它替整个 #60 子栈跑 ``docker``
CLI(镜像 build、compose up/down)。此前它调 ``subprocess.run`` **不带
timeout** —— 拉取一卡就无限等,只能等 GitHub 的 40 分钟 job timeout 把
整个 job 砍掉,日志里只留一句 ``The operation was canceled``,不指向任何
一条测试。2026-08-28 一天内因此烧掉三次 40 分钟(main 的 push run 一次、
PR 两次),而 integration job 的 pytest 又不像 unit job 那样带
``--timeout``,没有第二道闸。

这些用例住在**单独的文件**里,因为 e2e 模块整体挂 ``pytest.mark.integration``
——模块级 mark 加得上、取不下,超时路径就永远只能在装了 Docker 的机器上验。
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from .test_fullstack_egress_e2e import _DOCKER_BUILD_TIMEOUT_S, _docker


def test_docker_passes_a_timeout_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认与显式 timeout 都必须落到 ``subprocess.run`` 上。

    这是本修复的要害:少了这个 kwarg,卡住的 ``docker build`` 就没有任何
    东西会打断它。
    """
    seen: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, "out", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    _docker("version", "--format", "{{.Server.Version}}")
    _docker("build", "-t", "x", ".", timeout=_DOCKER_BUILD_TIMEOUT_S)

    assert len(seen) == 2
    assert all(call["timeout"] is not None for call in seen)
    assert seen[0]["argv"] == ["docker", "version", "--format", "{{.Server.Version}}"]
    assert seen[1]["timeout"] == _DOCKER_BUILD_TIMEOUT_S
    # build 档必须比探测档宽松,否则一次正常的镜像构建会被自己的闸打死。
    assert seen[1]["timeout"] > seen[0]["timeout"]


def test_docker_timeout_fails_the_test_naming_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时要变成一条点名到命令的失败,而不是挂死。

    断言消息里同时出现子命令和超时秒数——没有这两样,CI 上看到的还是
    「不知道卡在哪」。

    用 ``AssertionError`` 而非 ``pytest.fail``:两者在 pytest 里都判失败,
    但后者的 ``NoReturn`` 语义 CodeQL 读不出来,会把 except 分支当成 fall
    through 而报「explicit returns mixed with implicit returns」。
    """

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=float(kwargs["timeout"]))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(AssertionError) as excinfo:
        _docker("build", "-t", "expert-work-sandbox:dev", ".", timeout=7.0)

    message = str(excinfo.value)
    assert "build" in message
    assert "7" in message


def test_docker_returns_the_completed_process_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没超时就原样返回——调用方要读 returncode / stderr 决定 skip 还是 fail。"""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "stdout here", "stderr here")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _docker("compose", "up")
    assert result.returncode == 1
    assert result.stdout == "stdout here"
    assert result.stderr == "stderr here"
