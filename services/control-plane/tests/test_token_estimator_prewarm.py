"""B-106 —— 分词表预热:lifespan 就绪前、线程里,加上镜像里打好分词表。

测试环境实证:run 里第一次估算 token 时在事件循环线程上同步下载
``o200k_base``(56~63s),存活探针把 pod 杀掉。修法两半:镜像构建期把分词表
下到 ``TIKTOKEN_CACHE_DIR``,lifespan 在就绪前经 ``asyncio.to_thread`` 预热
run 用的那个进程级单例。这里钉住两半各自的形状。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import control_plane.app as app_module
from control_plane.settings import Settings
from expert_work.common.lifecycle import Lifecycle, ShutdownState
from tests.auth_fixtures import build_test_jwt_verifier

_CP_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
_CACHE_DIR = "/app/tiktoken-cache"


class _PrewarmSpy:
    def __init__(self, lifecycle: Lifecycle, *, result: bool) -> None:
        self._lifecycle = lifecycle
        self._result = result
        self.calls = 0
        self.state_at_call: ShutdownState | None = None
        self.had_running_loop: bool | None = None

    def __call__(self) -> bool:
        self.calls += 1
        self.state_at_call = self._lifecycle.state
        try:
            asyncio.get_running_loop()
            self.had_running_loop = True
        except RuntimeError:
            self.had_running_loop = False
        return self._result


def _app_with_spy(
    monkeypatch: pytest.MonkeyPatch, *, result: bool
) -> tuple[TestClient, Lifecycle, _PrewarmSpy]:
    lifecycle = Lifecycle()
    spy = _PrewarmSpy(lifecycle, result=result)
    monkeypatch.setattr(app_module, "warm_default_estimator", spy)
    app = app_module.create_app(
        settings=Settings(_env_file=None),  # type: ignore[call-arg]
        lifecycle=lifecycle,
        jwt_verifier=build_test_jwt_verifier(),
    )
    return TestClient(app), lifecycle, spy


def test_prewarm_runs_off_loop_before_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    client, lifecycle, spy = _app_with_spy(monkeypatch, result=True)
    with client:
        assert lifecycle.state is ShutdownState.RUNNING
    assert spy.calls == 1
    # 就绪之前:预热时进程还在 STARTING,队列 worker 等都还没接活。
    assert spy.state_at_call is ShutdownState.STARTING
    # to_thread:加载发生的线程里没有正在跑的事件循环。
    assert spy.had_running_loop is False


def test_prewarm_failure_warns_but_does_not_block_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client, lifecycle, spy = _app_with_spy(monkeypatch, result=False)
    with caplog.at_level(logging.WARNING), client:
        assert lifecycle.state is ShutdownState.RUNNING
    assert spy.calls == 1
    assert any("token_estimator_prewarm_failed" in r.getMessage() for r in caplog.records)


def _stages(text: str) -> tuple[str, str]:
    builder, runtime = re.split(r"^FROM .*$", text, flags=re.MULTILINE)[1:]
    return builder, runtime


def test_dockerfile_bakes_both_encodings_at_build_time() -> None:
    builder, _ = _stages(_CP_DOCKERFILE.read_text(encoding="utf-8"))
    assert f"ENV TIKTOKEN_CACHE_DIR={_CACHE_DIR}" in builder
    download = re.search(r"^RUN .*tiktoken\.get_encoding.*$", builder, flags=re.MULTILINE)
    assert download is not None, "builder 阶段没有构建期下载分词表的 RUN"
    for name in ("o200k_base", "cl100k_base"):
        assert f"'{name}'" in download.group(0), f"构建期没下载 {name}"


def test_dockerfile_runtime_reads_baked_cache() -> None:
    _, runtime = _stages(_CP_DOCKERFILE.read_text(encoding="utf-8"))
    assert re.search(
        rf"^COPY --from=builder \S* ?{re.escape(_CACHE_DIR)} {re.escape(_CACHE_DIR)}$",
        runtime,
        flags=re.MULTILINE,
    ), "runtime 阶段没有把构建期的分词表目录拷过来"
    assert f"ENV TIKTOKEN_CACHE_DIR={_CACHE_DIR}" in runtime
