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

import hashlib
import json
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
    """空串回退这条分支在本仓库里今天到不了(``AgentSpecDraft`` /
    ``AgentSpecRecord`` 的 ``spec_sha256`` 都钉了 ``min_length=64``,调用方拿到的
    永远要么是合法 64 位值要么在读回那一步就先抛了)。这里钉的是它的**契约**:
    真出现空串,必须回退现算,不能把空串写进 ``run.agent_spec_sha256``。"""
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


class _BoomSpec:
    """一个 ``model_dump`` 会抛的 spec 桩 —— 只为钉住 ``try`` 的覆盖范围。"""

    def model_dump(self, **_kwargs: object) -> None:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_hashing_failure_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """修复轮 2(N-3)—— ``compute_spec_sha256`` 必须在 ``try`` 里面。

    这个函数的契约是「绑不上只记日志,不影响 run」(见模块 docstring:「软
    失败:两个回写都只记日志」)。``compute_spec_sha256`` 现实中不会抛
    (对一个已校验模型做 ``json.dumps(model_dump(...))``),但它曾经被挪到过
    ``try`` 外面,一旦真的抛出就会一路窜到调用方,弄死一条本来能跑的 run。
    这里用一个 ``model_dump`` 会抛的桩钉住这条边界:算哈希失败也必须落进
    ``spec_bind_failed``,不能让异常逃出 ``bind_exec_spec``。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await bind_exec_spec(
            runs=runs,
            run_id=run_id,
            tenant_id=tenant_id,
            spec=_BoomSpec(),  # type: ignore[arg-type]
            stored_sha256="4" * 64,
            source=_SOURCE,
        )

    messages = [r.getMessage() for r in caplog.records]
    assert any("spec_bind_failed" in m for m in messages), messages


def _legacy_non_anthropic_spec_and_stored_sha() -> tuple[AgentSpec, str]:
    """B-105 前落库的非 Anthropic manifest:每个 ModelSpec 节点都物化了旧默认 4096,
    列里的哈希按这个形态算。旧形态用 4097 占位再换回 4096 构造,不借实现本身。"""
    raw = deepcopy(_SPEC)
    raw["spec"]["model"] = {
        "provider": "qwen",
        "name": "qwen3-max",
        "max_tokens": 4096,
        "fallback": [{"provider": "glm", "name": "glm-5.3", "max_tokens": 4096}],
    }
    spec = AgentSpec.model_validate(raw)
    placeholder = deepcopy(raw)
    placeholder["spec"]["model"]["max_tokens"] = 4097
    placeholder["spec"]["model"]["fallback"][0]["max_tokens"] = 4097
    old_dump = AgentSpec.model_validate(placeholder).model_dump(by_alias=True, mode="json")
    old_dump["spec"]["model"]["max_tokens"] = 4096
    old_dump["spec"]["model"]["fallback"][0]["max_tokens"] = 4096
    stored = hashlib.sha256(
        json.dumps(old_dump, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return spec, stored


@pytest.mark.asyncio
async def test_legacy_4096_normalisation_alone_does_not_log_divergence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """B-105 —— 存量非 Anthropic manifest 的 4096 加载时被归一成空;指纹的规范形态把空
    上限按 4096 算,所以现算哈希就等于列,不打 warning。绑的仍是列。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec, stored = _legacy_non_anthropic_spec_and_stored_sha()
    assert stored == compute_spec_sha256(spec)

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
    assert not any("spec_sha256_diverged" in m for m in messages), messages
    info = await runs.get(run_id=run_id, tenant_id=tenant_id)
    assert info is not None
    assert info.agent_spec_sha256 == stored


@pytest.mark.asyncio
async def test_legacy_4096_spec_with_a_real_divergence_still_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """同一份存量 manifest,列与内容真不一致(这里换成别的哈希)时照样要打。"""
    tenant_id = uuid4()
    runs = InMemoryRunStore()
    run_id = await _seed_run(runs, tenant_id=tenant_id)
    spec, _ = _legacy_non_anthropic_spec_and_stored_sha()
    stored = "3" * 64

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


def test_digest_form_does_not_change_the_persisted_shape() -> None:
    """4096 只在算指纹时写回;存库的 ``model_dump`` 照旧省略。"""
    spec, _ = _legacy_non_anthropic_spec_and_stored_sha()
    dumped = spec.model_dump(by_alias=True, mode="json")
    assert "max_tokens" not in dumped["spec"]["model"]
    assert "max_tokens" not in dumped["spec"]["model"]["fallback"][0]
