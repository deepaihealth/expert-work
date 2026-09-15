"""B-61 Task 5b 修复轮 1(I-1)—— ``bind_exec_spec`` 绑库里那一列,不重算。

背景:T5b 的读回宽容(``expert_work.persistence.stored_spec``)会在回滚窗口里
剔掉存量 manifest 里不认识的键。``bind_exec_spec`` 如果自己用
``compute_spec_sha256(spec)`` 现算,算出来的就是**剔完键**的哈希,与
``agent_spec_revision.spec_sha256`` 那一列(未剔键的原值)不再相等——
``run.agent_spec_sha256`` 与它的等值 join(``run_trace.py`` 模块 docstring
记为契约)就此静默断掉。这里钉住:绑的必须是调用方传入的 ``stored_sha256``
(库里那一列),重算值只用于跟它比对、发现分叉时才打日志。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from control_plane.run_trace import bind_exec_spec
from expert_work.persistence.platform_agent_template import compute_spec_sha256
from expert_work.protocol import AgentSpec
from expert_work.runtime.runs import DisconnectMode, RunInfo, RunStatus
from expert_work.runtime.runs.store import InMemoryRunStore

_SOURCE = "test_source"
_LOGGER_NAME = f"expert_work.control_plane.{_SOURCE}"

_SPEC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": "support-bot", "version": "1.0.0", "tenant": "acme"},
    "spec": {
        "tenant_config": {},
        "model": {"provider": "anthropic", "name": "claude-sonnet-4-5"},
        "system_prompt": {"template": "you are support"},
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC))


async def _seed_run(runs: InMemoryRunStore, *, tenant_id: Any) -> Any:
    run_id = uuid4()
    now = datetime.now(UTC)
    await runs.create(
        RunInfo(
            run_id=run_id,
            tenant_id=tenant_id,
            thread_id=uuid4(),
            user_id=None,
            status=RunStatus.RUNNING,
            on_disconnect=DisconnectMode.CANCEL,
            is_resume=False,
            error=None,
            created_at=now,
            updated_at=now,
            finished_at=None,
        )
    )
    return run_id


@pytest.mark.asyncio
async def test_binds_the_stored_column_not_the_recomputed_hash() -> None:
    """核心断言:分叉时写进 store 的是 ``stored_sha256``,不是现算值。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec = _spec()
    computed = compute_spec_sha256(spec)
    stored = "1" * 64
    assert stored != computed  # 前提:两者确实不同,否则这条测试测不出东西。

    await bind_exec_spec(
        runs=runs,
        run_id=run_id,
        tenant_id=tenant_id,
        spec=spec,
        stored_sha256=stored,
        source=_SOURCE,
    )

    row = await runs.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None
    assert row.agent_spec_sha256 == stored


@pytest.mark.asyncio
async def test_empty_stored_sha256_falls_back_to_the_recomputed_hash() -> None:
    """草稿试跑那一路 ``draft_sha256`` 列可能是空串 —— 空则回退现算,不是留空。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec = _spec()
    computed = compute_spec_sha256(spec)

    await bind_exec_spec(
        runs=runs, run_id=run_id, tenant_id=tenant_id, spec=spec, stored_sha256="", source=_SOURCE
    )

    row = await runs.get(run_id=run_id, tenant_id=tenant_id)
    assert row is not None
    assert row.agent_spec_sha256 == computed


@pytest.mark.asyncio
async def test_divergence_logs_a_warning_with_both_hashes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """分叉是「宽容在执行路径上生效了」的唯一信号 —— 必须留痕,且两个哈希都要
    在里面(内容哈希不是秘密,排查时缺一个都对不上)。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec = _spec()
    computed = compute_spec_sha256(spec)
    stored = "2" * 64
    assert stored != computed

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await bind_exec_spec(
            runs=runs,
            run_id=run_id,
            tenant_id=tenant_id,
            spec=spec,
            stored_sha256=stored,
            source=_SOURCE,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("spec_sha256_diverged" in m for m in messages), messages
    assert any(stored in m and computed in m for m in messages), messages


@pytest.mark.asyncio
async def test_no_divergence_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """不分叉(存量 manifest,没有宽容生效)不该打这条 warning —— 否则回滚窗口
    之外也会一直吵。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec = _spec()
    computed = compute_spec_sha256(spec)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await bind_exec_spec(
            runs=runs,
            run_id=run_id,
            tenant_id=tenant_id,
            spec=spec,
            stored_sha256=computed,
            source=_SOURCE,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert not any("spec_sha256_diverged" in m for m in messages), messages


@pytest.mark.asyncio
async def test_runs_none_is_a_no_op() -> None:
    """回归保护:没接持久化的 ``RunManager``(``runs=None``)必须直接返回,不
    能因为新增的 ``stored_sha256`` 参数就报错或改变这条早退路径。"""
    await bind_exec_spec(
        runs=None,
        run_id=uuid4(),
        tenant_id=uuid4(),
        spec=_spec(),
        stored_sha256="3" * 64,
        source=_SOURCE,
    )
