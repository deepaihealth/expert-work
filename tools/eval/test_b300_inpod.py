"""B-300 控制面里那几步的离线测试:进程内存储 + 假嵌入 + 真的整理任务类,不连库、不调模型。"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import b300_eval as cli
import b300_inpod as inpod
import b300_scenarios as sc
import pytest
from longmem.embedders import KeywordEmbedder

from expert_work.persistence import InMemoryMemoryStore, InMemoryTenantUserStore
from expert_work.runtime.runs import InMemoryRunEventStore
from expert_work.runtime.runs.event_store import RunEventRecord

TENANT = UUID("11111111-1111-4111-8111-111111111111")


class _NoTenantConfig:
    async def get(self, *, tenant_id: UUID) -> Any:
        from control_plane.tenancy import TenantConfigNotConfiguredError

        raise TenantConfigNotConfiguredError(tenant_id=tenant_id)


class _FakeAux:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **_: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            text='{"keep": false, "reject_reason": "false_cluster"}',
            model="glm-5.3-flash",
            input_tokens=100,
            output_tokens=50,
            parsed=None,
        )


def _deps(model: str = "glm-5.3-flash") -> tuple[inpod.Deps, InMemoryMemoryStore, _FakeAux]:
    from control_plane.memory_consolidator import MemoryConsolidator, make_consolidator_embedder

    store = InMemoryMemoryStore()
    embedder = KeywordEmbedder()
    fake = _FakeAux()

    def factory(max_calls: int) -> tuple[Any, inpod.CappedAux]:
        aux = inpod.CappedAux(fake, max_calls)
        return (
            MemoryConsolidator(
                memory_store=store,
                tenant_config_service=_NoTenantConfig(),  # type: ignore[arg-type]
                audit_logger=SimpleNamespace(),  # type: ignore[arg-type]
                aux_model=aux,
                embedder=make_consolidator_embedder(embedder),
            ),
            aux,
        )

    deps = inpod.Deps(
        memory=store,
        users=InMemoryTenantUserStore(),
        run_events=InMemoryRunEventStore(),
        embedder=embedder,
        consolidator_factory=factory,
        consolidation_model=model,
    )
    return deps, store, fake


async def _run(deps: inpod.Deps, payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[str] = []
    await inpod.dispatch(deps, payload, out.append)
    return [json.loads(line) for line in out]


def _c5() -> sc.Scenario:
    return next(s for s in sc.build_group("C") if s.cls == "C5")


def _seed_entry(scenario: sc.Scenario, user: str = "b300-u1") -> dict[str, Any]:
    return {
        "key": user,
        "tenant_id": str(TENANT),
        "user": user,
        "agent_name": "eval-b300-glm",
        "seeds": [asdict(s) for s in scenario.seeds],
    }


def test_inpod_imports_nothing_from_tools_eval() -> None:
    """pod 里没有 tools/eval:这份脚本只能 import 标准库和平台包。"""
    tree = ast.parse(cli.INPOD_SOURCE.read_text(encoding="utf-8"))
    modules = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    local = {p.stem for p in cli._HERE.glob("*.py")} | {"longmem"}
    assert not {m.split(".")[0] for m in modules} & local


def test_bundle_embeds_the_payload_and_stays_valid_python(tmp_path: Path) -> None:
    payload = {"op": "state", "users": [{"key": "k", "x_key": '引号"与\\反斜杠'}]}
    path = tmp_path / "b300_bundle_test.py"
    path.write_text(cli.bundle_source(payload), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("b300_bundle_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["b300_bundle_test"] = module
    try:
        spec.loader.exec_module(module)  # 不是 __main__:只加载,不跑
    finally:
        sys.modules.pop("b300_bundle_test", None)
    assert module.EMBEDDED_INPUT == payload
    with pytest.raises(cli.EvalError):
        cli.bundle_source(payload, source="no marker here")


@pytest.mark.asyncio
async def test_seed_writes_and_supersedes() -> None:
    deps, store, _ = _deps()
    scenario = _c5()
    (row,) = await _run(deps, {"op": "seed", "entries": [_seed_entry(scenario)]})
    assert set(row["memory_ids"]) == {"x", "y", "near1", "near2"} and row["embedded"] == 4
    user = UUID(row["user_id"])
    items = {
        str(m.id): m for m in await store.list_for_user(tenant_id=TENANT, user_id=user, limit=50)
    }
    assert row["memory_ids"]["y"] in items
    x = await store.list_for_user(tenant_id=TENANT, user_id=user, limit=50, as_of=datetime.now(UTC))
    assert row["memory_ids"]["x"] not in {str(m.id) for m in x}  # X 已被 Y 取代
    near = items[row["memory_ids"]["near1"]]
    assert near.created_at is not None and near.created_at < datetime.now(UTC)


@pytest.mark.asyncio
async def test_seed_is_the_same_user_as_the_external_api() -> None:
    from control_plane.api._external import resolve_external_user_id

    deps, _store, _ = _deps()
    scenario = next(s for s in sc.build_group("B") if s.cls == "B7")
    (row,) = await _run(deps, {"op": "seed", "entries": [_seed_entry(scenario, "b300-ext")]})
    same = await resolve_external_user_id(tenant_id=TENANT, user_id="b300-ext", users=deps.users)
    assert row["user_id"] == str(same)


def test_recalled_ids_from_frames() -> None:
    frames = [
        {"memory_recall": {"recalled_memories": [{"id": "a"}, {"id": "b"}], "_duration_ms": 3}},
        json.dumps({"memory_recall": {"recalled_memories": [{"id": "a"}]}}),
        {"agent": {"messages": []}},
        "not json",
    ]
    assert inpod.recalled_ids(frames) == ["a", "b"]


@pytest.mark.asyncio
async def test_recalled_reads_run_events() -> None:
    deps, _store, _ = _deps()
    run_id = uuid4()
    now = datetime.now(UTC)
    await deps.run_events.append(
        RunEventRecord(
            run_id=run_id,
            seq=1,
            event_name="updates",
            data={"memory_recall": {"recalled_memories": [{"id": "m1", "content": "secret"}]}},
            created_at_ms=0,
            created_at=now,
        )
    )
    (row,) = await _run(
        deps, {"op": "recalled", "tenant_id": str(TENANT), "run_ids": [str(run_id)]}
    )
    assert row == {"op": "recalled", "run_id": str(run_id), "memory_ids": ["m1"]}


@pytest.mark.asyncio
async def test_candidates_print_ids_only() -> None:
    deps, _store, fake = _deps()
    # 这一条在关键词假嵌入下凑得成簇(确定性的),所以下面的「只出 id」不是空转。
    scenario = next(
        s for s in sc.build_group("C") if s.cls == "C5" and s.fact_key == "auto-renewal"
    )
    (seeded,) = await _run(deps, {"op": "seed", "entries": [_seed_entry(scenario)]})
    (row,) = await _run(
        deps,
        {
            "op": "candidates",
            "users": [{"key": "b300-u1", "tenant_id": str(TENANT), "user": "b300-u1"}],
        },
    )
    assert row["min_cluster_size"] == 3 and row["similarity"] == 0.85
    assert row["clusters"], "the near-duplicate seeds should form a candidate cluster"
    assert set(row["clusters"][0]) <= set(seeded["memory_ids"].values())
    assert "content" not in json.dumps(row)
    assert fake.calls == 0  # 取候选不调模型


@pytest.mark.asyncio
async def test_consolidate_refuses_another_model() -> None:
    deps, _store, fake = _deps(model="some-other-model")
    with pytest.raises(SystemExit, match=r"glm-5\.3-flash"):
        await _run(
            deps, {"op": "consolidate", "users": [], "max_calls": 3, "model": "glm-5.3-flash"}
        )
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_consolidate_reports_calls_and_tokens() -> None:
    deps, _store, _fake = _deps()
    await _run(deps, {"op": "seed", "entries": [_seed_entry(_c5())]})
    rows = await _run(
        deps,
        {
            "op": "consolidate",
            "max_calls": 9,
            "model": "glm-5.3-flash",
            "users": [{"key": "b300-u1", "tenant_id": str(TENANT), "user": "b300-u1"}],
        },
    )
    total = rows[-1]
    assert total["op"] == "consolidate_total"
    assert total["input_tokens"] == 100 * total["calls"]


@pytest.mark.asyncio
async def test_capped_aux_stops_before_sending() -> None:
    fake = _FakeAux()
    aux = inpod.CappedAux(fake, 1)
    await aux(prompt="p")
    with pytest.raises(inpod.CallCapReachedError):
        await aux(prompt="p")
    assert fake.calls == 1 and aux.input_tokens == 100


@pytest.mark.asyncio
async def test_state_flags_keys_and_hides_plain_content() -> None:
    deps, _store, _ = _deps()
    scenario = next(s for s in sc.build_group("C") if s.cls == "C3")
    await _run(deps, {"op": "seed", "entries": [_seed_entry(scenario)]})
    (row,) = await _run(
        deps,
        {
            "op": "state",
            "users": [
                {
                    "key": "b300-u1",
                    "tenant_id": str(TENANT),
                    "user": "b300-u1",
                    "x_key": scenario.x_key,
                    "y_key": scenario.y_key,
                }
            ],
        },
    )
    (memory,) = row["rows"]
    assert memory["has_x"] is True and memory["has_y"] is False
    assert memory["summary"] is False and memory["content"] is None  # 只有汇总行带正文
