"""B-168 —— ``InMemoryMemoryWritebackJobStore`` 跑共用契约(见 ``conftest.py``)。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from expert_work.persistence.memory.writeback_job import (
    InMemoryMemoryWritebackJobStore,
    MemoryWritebackJobStore,
)

Scenario = Callable[[MemoryWritebackJobStore], Awaitable[None]]


@pytest.mark.asyncio
async def test_writeback_job_contract(writeback_job_scenarios: tuple[Scenario, ...]) -> None:
    for scenario in writeback_job_scenarios:
        await scenario(InMemoryMemoryWritebackJobStore())
