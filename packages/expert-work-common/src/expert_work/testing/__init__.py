"""Reusable testing helpers — importable from any test module.

These were previously declared inside ``tests/conftest.py``; moving them to a
real package lets package-level tests (e.g. ``packages/expert-work-persistence/tests/``)
type-hint and import them without sys.path gymnastics.

Pytest fixtures themselves still live in the root ``conftest.py``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FakeCompletion:
    """Pretend LLM response. Add fields as production LLM contract grows."""

    content: str
    tokens_used: int = 0
    cached: bool = False


@dataclass
class MockLLM:
    """Deterministic LLM stub.

    Default behavior: any prompt returns ``FakeCompletion(content="ok")``.
    Configure overrides via ``.expect(prompt_prefix, response)``.
    All prompts are recorded in ``.calls`` for assertion.
    """

    default: FakeCompletion = field(default_factory=lambda: FakeCompletion(content="ok"))
    expectations: list[tuple[str, FakeCompletion]] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def expect(self, prompt_prefix: str, response: FakeCompletion) -> None:
        """Register a response for prompts starting with ``prompt_prefix``."""
        self.expectations.append((prompt_prefix, response))

    async def complete(self, prompt: str) -> FakeCompletion:
        """Return the first matching expectation, or the default."""
        self.calls.append(prompt)
        for prefix, response in self.expectations:
            if prompt.startswith(prefix):
                return response
        return self.default


@dataclass
class InMemorySecretStore:
    """Dict-backed SecretStore for tests.

    When ``expert_work.runtime.secrets.SecretStore`` Protocol lands in
    Stream A.x (per ADR-0007), this class will explicitly implement it.
    """

    _store: dict[str, str] = field(default_factory=dict)

    async def get(self, name: str, *, version: str | None = None) -> str:
        del version  # not modelled in in-memory store
        if name not in self._store:
            raise KeyError(f"secret not found: {name}")
        return self._store[name]

    async def put(self, name: str, value: str) -> None:
        self._store[name] = value

    async def delete(self, name: str) -> None:
        self._store.pop(name, None)


def explain_compose_pull_failure(
    infra_dir: str | os.PathLike[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """把 ``docker compose pull`` 的真实报错捞回来,给测试的失败信息用。

    ``testcontainers`` 起栈走的是 ``check_call``:失败时 ``CalledProcessError``
    身上**没有 output**,pytest 报出来的只剩一句「returned non-zero exit
    status 1」——拉失败的是哪个镜像、为什么,全在被丢掉的 stderr 里。
    2026-08-31 为这个查过一轮:13 条 integration 用例同时红,而唯一能推的只有
    「1.4 秒就失败,所以不是超时」,拿不到实据(同 #1372 给 integration 卡死
    补位置信息那条教训)。

    所以这里带 ``capture_output`` 重跑同一条命令:要么复现出真报错,要么这次
    成功——后者本身就说明上一次是偶发/限流,同样是有用的信息。

    ``runner`` 可注入,好让这个函数自己能被单测证明真的会把 stderr 吐出来。
    """
    try:
        probe = runner(
            ["docker", "compose", "-f", "docker-compose.yml", "pull"],
            cwd=str(infra_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(带输出重跑 pull 也失败了:{exc})"
    if probe.returncode == 0:
        return "(带输出重跑同一条 pull 这次成功了 —— 上一次多半是偶发/限流)"
    return (
        f"exit={probe.returncode}\n"
        f"stdout:\n{(probe.stdout or '')[-1500:]}\n"
        f"stderr:\n{(probe.stderr or '')[-1500:]}"
    )


__all__ = ["FakeCompletion", "InMemorySecretStore", "MockLLM", "explain_compose_pull_failure"]
