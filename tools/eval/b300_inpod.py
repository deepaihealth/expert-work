"""B-300 评测在**控制面进程里**才能做的几件事(设计 §8.3「往记忆库种记忆」、§8.7 C5 / §9.3 T13)。

第三方开放 API 没有记忆端点,整理任务没有触发端点,召回帧对外已剔除(#2098),整理取候选的
``MemoryConsolidator._find_candidate_clusters``(``memory_consolidator.py:908``)是私有方法、要连库
和整理任务的装配 —— 所以这几步跑在控制面的环境里:

* **测试环境**:``kubectl exec`` 进控制面 pod,脚本从 stdin 喂给 pod 里的 python(同
  ``compaction_retention.py``)。本地 ``b300_eval.py bundle`` 把这份脚本和这一步的输入拼成一个
  自包含文件,输入不进命令行参数::

      POD=$(kubectl -n expert-work get pod -l app.kubernetes.io/name=control-plane \\
            --field-selector=status.phase=Running -o name | head -1)
      kubectl -n expert-work exec -i "$POD" -- python - < bundle.py > out.jsonl

* **本地栈**:在控制面的虚拟环境里 ``python tools/eval/b300_inpod.py --input step.json``。

五个操作(输入 ``{"op": ..., ...}``,输出 JSON 行):

* ``seed``:按场景给评测用户种记忆(:func:`seed`),打印每条种子的记忆 id。
* ``recalled``:读 ``run_event`` 里召回节点那一帧,打印每个 run 召回到的记忆 **id**。
* ``candidates``:C5 / T13 的前提记录器 —— 调整理任务取候选簇的那个函数,只打印每个候选簇的
  成员 **id**(不打印内容)。
* ``consolidate``:只对给定用户做一轮整理(``_sweep_user``),整理调用数有上限,打印次数与 token。
* ``state``:评测用户当前有效的记忆:id、类型、来源会话、是否汇总行、是否含关键串;只有
  **汇总行**带正文(C5 ① 要判官判;正文全部来自合成的种子)。

这个文件**不能 import ``tools/eval`` 里的任何东西**(pod 里没有);平台包在函数里按需 import。
引导(:func:`bootstrap`)用 ``create_app(enable_scheduler=False, ...)`` 进一次 lifespan 拿到与线上
同一套装配(嵌入、凭据解析、存储);lifespan 里总会起的后台 worker(如写回失败重试)在这个短命进程里
也会起 —— 已知副作用,未在 pod 里验证过。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

#: ``bundle`` 生成的文件在这里塞进这一步的输入;直接跑本文件时为 ``None``,用 ``--input``。
EMBEDDED_INPUT: Mapping[str, Any] | None = None

OPS = ("seed", "recalled", "candidates", "consolidate", "state")


@dataclass
class Deps:
    memory: Any  # MemoryStore
    users: Any  # TenantUserStore
    run_events: Any  # RunEventStore
    embedder: Any  # orchestrator Embedder(.embed(texts, tenant_id=...))
    consolidator_factory: Callable[[int], Any]  # max_calls -> (consolidator, CappedAux)
    consolidation_model: str


# --------------------------------------------------------------------------- RLS 作用域


@contextlib.contextmanager
def _scope(tenant_id: UUID, user_id: UUID | None = None) -> Any:
    """与整理任务同一个作用域(``memory_consolidator._scope``):逐租户 / 逐用户,不 bypass。"""
    from control_plane.memory_consolidator import _scope as consolidator_scope

    with consolidator_scope(tenant_id, user_id):
        yield


async def _user_id(deps: Deps, tenant_id: UUID, external_user: str) -> UUID:
    """与对外平面同一个取法(``api/_external.resolve_external_user_id``):评测用户第一次出现时建行,
    之后对外 API 用同一个 ``user_id`` 字符串跑对话,落到同一个人。"""
    from control_plane.api._external import resolve_external_user_id

    with _scope(tenant_id):
        return await resolve_external_user_id(
            tenant_id=tenant_id, user_id=external_user, users=deps.users
        )


# --------------------------------------------------------------------------- seed


def _memory_item(
    *,
    seed: Mapping[str, Any],
    tenant_id: UUID,
    user_id: UUID,
    embedding: Sequence[float],
    agent_name: str | None,
    now: datetime,
) -> Any:
    from expert_work.protocol import MemoryItem

    created = now - timedelta(days=int(seed["days_ago"]))
    fields: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": tenant_id,
        "user_id": user_id,
        "kind": seed["kind"],
        # 经历类只有写它的智能体召回得到(§1 #7);事实类共享。
        "agent_name": agent_name if seed["kind"] == "episodic" else None,
        "content": seed["content"],
        "embedding": tuple(float(v) for v in embedding),
        "importance": 0.7,
        "confidence": float(seed["confidence"]),
        "created_at": created,
        "last_used_at": created,
        "valid_at": created,
    }
    # PR 2 之前的提交上没有 ``origin``:种子的出处只在正文里(基线就是这样)。
    if seed.get("origin") is not None and "origin" in MemoryItem.model_fields:
        fields["origin"] = seed["origin"]
    return MemoryItem(**fields)


async def seed(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    """``payload["entries"]``:``{key, tenant_id, user, agent_name, seeds: [...]}``。

    被取代的种子(``superseded_by``)先写,再用存储的 ``supersede`` 换成取代它的那条 —— 与去重合并
    判「更新」时同一条路(P5b append-only)。"""
    now = datetime.now(UTC)
    for entry in payload["entries"]:
        tenant_id = UUID(entry["tenant_id"])
        user_id = await _user_id(deps, tenant_id, entry["user"])
        seeds = list(entry["seeds"])
        vectors = await deps.embedder.embed([s["content"] for s in seeds], tenant_id=tenant_id)
        items = {
            s["key"]: _memory_item(
                seed=s,
                tenant_id=tenant_id,
                user_id=user_id,
                embedding=vec,
                agent_name=entry.get("agent_name"),
                now=now,
            )
            for s, vec in zip(seeds, vectors, strict=True)
        }
        replacing = {s["superseded_by"] for s in seeds if s.get("superseded_by")}
        ids: dict[str, str] = {}
        with _scope(tenant_id, user_id):
            plain = [items[s["key"]] for s in seeds if s["key"] not in replacing]
            await deps.memory.write(plain)
            for s in seeds:
                if s["key"] not in replacing:
                    ids[s["key"]] = str(items[s["key"]].id)
            for s in seeds:
                target = s.get("superseded_by")
                if not target:
                    continue
                new = await deps.memory.supersede(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    old_id=items[s["key"]].id,
                    new_item=items[target],
                )
                ids[target] = str(new.id) if new is not None else ""
        emit(
            json.dumps(
                {
                    "op": "seed",
                    "key": entry["key"],
                    "user": entry["user"],
                    "user_id": str(user_id),
                    "memory_ids": ids,
                    "embedded": len(seeds),
                }
            )
        )


# --------------------------------------------------------------------------- recalled


def recalled_ids(frames: Sequence[Any]) -> list[str]:
    """``updates`` 帧里召回节点写的 ``recalled_memories`` → 记忆 id(去重、保序)。"""
    out: list[str] = []
    for frame in frames:
        data = frame.data if hasattr(frame, "data") else frame
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                continue
        node = data.get("memory_recall") if isinstance(data, dict) else None
        memories = node.get("recalled_memories") if isinstance(node, dict) else None
        for item in memories or []:
            mid = item.get("id") if isinstance(item, dict) else None
            if isinstance(mid, str) and mid not in out:
                out.append(mid)
    return out


async def recalled(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    tenant_id = UUID(payload["tenant_id"])
    for run_id in payload["run_ids"]:
        with _scope(tenant_id):
            frames = await deps.run_events.list(
                run_id=UUID(run_id), limit=1000, event_names=("updates",)
            )
        emit(json.dumps({"op": "recalled", "run_id": run_id, "memory_ids": recalled_ids(frames)}))


# --------------------------------------------------------------------------- candidates


async def candidates(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    """C5 / T13 的前提:整理**之前**,这一片会不会被取成候选簇。只出 id。"""
    consolidator, _aux = deps.consolidator_factory(0)
    for entry in payload["users"]:
        tenant_id = UUID(entry["tenant_id"])
        user_id = await _user_id(deps, tenant_id, entry["user"])
        with _scope(tenant_id):
            cfg = await consolidator._resolve_thresholds(tenant_id)
        with _scope(tenant_id, user_id):
            clusters = await consolidator._find_candidate_clusters(
                tenant_id=tenant_id,
                user_id=user_id,
                min_cluster_size=cfg.min_cluster_size,
                similarity=cfg.similarity,
            )
        emit(
            json.dumps(
                {
                    "op": "candidates",
                    "key": entry["key"],
                    "user_id": str(user_id),
                    "min_cluster_size": cfg.min_cluster_size,
                    "similarity": cfg.similarity,
                    "clusters": [[str(m.id) for m in cluster] for cluster in clusters],
                }
            )
        )


# --------------------------------------------------------------------------- consolidate


class CallCapReachedError(RuntimeError):
    """这一步的整理调用数到上限了 —— 不再发。"""


class CappedAux:
    """整理用的辅助模型外面套一层:超过 ``max_calls`` 就不发;记下每次的 token。"""

    def __init__(self, inner: Any, max_calls: int) -> None:
        self._inner = inner
        self.max_calls = max_calls
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.models: set[str] = set()

    async def __call__(self, **kwargs: Any) -> Any:
        if self.calls >= self.max_calls:
            raise CallCapReachedError(f"b300: consolidation call cap {self.max_calls} reached")
        self.calls += 1
        reply = await self._inner(**kwargs)
        self.input_tokens += int(getattr(reply, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(reply, "output_tokens", 0) or 0)
        self.models.add(str(getattr(reply, "model", "")))
        return reply


async def consolidate(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    """只整理给定的评测用户(不跑整轮 ``run_once``:那会扫到所有租户的所有人)。"""
    from control_plane.memory_consolidator import ConsolidatorRunSummary

    expected = payload["model"]
    if deps.consolidation_model != expected:
        raise SystemExit(
            f"this deployment consolidates with {deps.consolidation_model!r}, "
            f"the eval expects {expected!r} (design §12 ②); stopped before any call"
        )
    consolidator, aux = deps.consolidator_factory(int(payload["max_calls"]))
    for entry in payload["users"]:
        tenant_id = UUID(entry["tenant_id"])
        user_id = await _user_id(deps, tenant_id, entry["user"])
        summary = ConsolidatorRunSummary()
        before = aux.calls
        error = None
        with _scope(tenant_id):
            cfg = await consolidator._resolve_thresholds(tenant_id)
        try:
            with _scope(tenant_id, user_id):
                await consolidator._sweep_user(
                    tenant_id=tenant_id, user_id=user_id, cfg=cfg, summary=summary
                )
        except CallCapReachedError as exc:
            error = str(exc)
        emit(
            json.dumps(
                {
                    "op": "consolidate",
                    "key": entry["key"],
                    "user_id": str(user_id),
                    "calls": aux.calls - before,
                    "candidates": summary.cluster_candidates,
                    "consolidated": summary.consolidated,
                    "rejected": summary.cluster_rejected,
                    "errors": summary.errors + (1 if error else 0),
                    "error": error,
                }
            )
        )
    emit(
        json.dumps(
            {
                "op": "consolidate_total",
                "calls": aux.calls,
                "input_tokens": aux.input_tokens,
                "output_tokens": aux.output_tokens,
                "models": sorted(aux.models),
            }
        )
    )


# --------------------------------------------------------------------------- state


def _norm(text: str) -> str:
    return "".join(text.split()).lower()


async def state(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    for entry in payload["users"]:
        tenant_id = UUID(entry["tenant_id"])
        user_id = await _user_id(deps, tenant_id, entry["user"])
        with _scope(tenant_id, user_id):
            items = await deps.memory.list_for_user(tenant_id=tenant_id, user_id=user_id, limit=200)
        x_key, y_key = _norm(entry.get("x_key", "")), _norm(entry.get("y_key", ""))
        rows = []
        for item in items:
            text = _norm(item.content)
            summary = bool(item.consolidated_from)
            rows.append(
                {
                    "id": str(item.id),
                    "kind": item.kind,
                    "source_thread_id": item.source_thread_id,
                    "summary": summary,
                    "origin": getattr(item, "origin", None),
                    "has_x": bool(x_key) and x_key in text,
                    "has_y": bool(y_key) and y_key in text,
                    # 只有汇总行带正文:C5 ① 要判官判它有没有把 X 说成现在的。
                    "content": item.content if summary else None,
                }
            )
        emit(
            json.dumps({"op": "state", "key": entry["key"], "user_id": str(user_id), "rows": rows})
        )


# --------------------------------------------------------------------------- 引导与入口


@contextlib.asynccontextmanager
async def bootstrap() -> AsyncIterator[Deps]:
    """控制面进程里的装配:与线上同一套 ``create_app``,不起调度器(不起整理任务、触发器等)。"""
    from control_plane.app import create_app
    from control_plane.aux_model_adapter import make_llm_router_aux_model
    from control_plane.memory_consolidator import MemoryConsolidator, make_consolidator_embedder

    app = create_app(enable_reaper=False, enable_scheduler=False, enable_curation_worker=False)
    async with app.router.lifespan_context(app):
        st = app.state
        settings = st.settings
        missing = [
            name
            for name in ("memory_repo", "tenant_user_repo", "run_event_store", "embedder")
            if getattr(st, name, None) is None
        ]
        if missing:
            raise SystemExit(f"app.state has no {missing}; this release wires them differently")

        def factory(max_calls: int) -> tuple[Any, CappedAux]:
            resolver = getattr(st, "credentials_resolver", None)
            inner = (
                make_llm_router_aux_model(
                    resolver=resolver,
                    secret_store=st.secret_store,
                    default_provider=settings.memory_consolidator_default_aux_provider,
                    default_model=settings.memory_consolidator_default_aux_model,
                    secret_cache=getattr(st, "credential_value_cache", None),
                )
                if resolver is not None
                else None
            )
            aux = CappedAux(inner, max_calls)
            consolidator = MemoryConsolidator(
                memory_store=st.memory_repo,
                tenant_config_service=st.tenant_config_service,
                audit_logger=st.audit_logger,
                aux_model=aux,
                embedder=make_consolidator_embedder(st.embedder),
                default_aux_model_name=settings.memory_consolidator_default_aux_model,
            )
            return consolidator, aux

        yield Deps(
            memory=st.memory_repo,
            users=st.tenant_user_repo,
            run_events=st.run_event_store,
            embedder=st.embedder,
            consolidator_factory=factory,
            consolidation_model=settings.memory_consolidator_default_aux_model,
        )


_HANDLERS = {
    "seed": seed,
    "recalled": recalled,
    "candidates": candidates,
    "consolidate": consolidate,
    "state": state,
}


async def dispatch(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    op = payload.get("op")
    if op not in _HANDLERS:
        raise SystemExit(f"unknown op {op!r}; one of {OPS}")
    await _HANDLERS[op](deps, payload, emit)


async def _main_async(payload: Mapping[str, Any]) -> None:
    async with bootstrap() as deps:
        await dispatch(deps, payload, lambda line: print(line, flush=True))


def main(argv: Sequence[str] | None = None) -> int:
    if EMBEDDED_INPUT is not None:
        payload: Mapping[str, Any] = EMBEDDED_INPUT
    else:
        parser = argparse.ArgumentParser(description="B-300 eval steps inside the control plane")
        parser.add_argument("--input", type=Path, required=True)
        args = parser.parse_args(argv)
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    asyncio.run(_main_async(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
