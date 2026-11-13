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

另有 ``writeback_usage``(带写回的评测智能体每个评测用户的后台写回实际用了多少 token,只出数字)
与 ``preflight``(评测租户的记忆清理 / 到期复核开关,只出数字与开关)。

这个文件**不能 import ``tools/eval`` 里的任何东西**(pod 里没有);平台包在函数里按需 import。
引导(:func:`bootstrap`)**不建 app、不进 lifespan**,只拼要用的那几样,装配前后都查一遍没有
后台任务 —— 否则这个进程可能去认领真租户排队的运行或写回任务(审查 H1)。
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

OPS = ("seed", "recalled", "candidates", "consolidate", "state", "writeback_usage", "preflight")
#: 平台的写回把用量记成这一类(``token_usage_store.PLATFORM_OVERHEAD_USAGE_KIND``)。
PLATFORM_OVERHEAD = "platform_overhead"


@dataclass
class Deps:
    memory: Any  # MemoryStore
    users: Any  # TenantUserStore
    run_events: Any  # RunEventStore
    embedder: Any  # orchestrator Embedder(.embed(texts, tenant_id=...))
    consolidator_factory: Callable[[int], Any]  # max_calls -> (consolidator, CappedAux)
    consolidation_model: str
    token_usage: Any = None  # TokenUsageStore
    tenant_config: Any = None  # TenantConfigService
    consolidation_interval_s: float | None = None


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
            # 平台自己的整理每 4 小时扫一轮,评测租户关不掉(没有按租户的开关):我们这一步之前就已
            # 被整理过的,前提不成立(审查 M5)。
            live = await deps.memory.list_for_user(tenant_id=tenant_id, user_id=user_id, limit=200)
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
                    "already_consolidated": any(bool(m.consolidated_from) for m in live),
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
        #: 要了结构化输出却没拿到 ``parsed`` 的次数:路由器在里面最多再试 2 次,那几次的 token
        #: 回包里没有,本地按上限补记。
        self.unparsed = 0

    async def __call__(self, **kwargs: Any) -> Any:
        if self.calls >= self.max_calls:
            raise CallCapReachedError(f"b300: consolidation call cap {self.max_calls} reached")
        self.calls += 1
        reply = await self._inner(**kwargs)
        self.input_tokens += int(getattr(reply, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(reply, "output_tokens", 0) or 0)
        self.models.add(str(getattr(reply, "model", "")))
        if kwargs.get("output_schema") is not None and getattr(reply, "parsed", None) is None:
            self.unparsed += 1
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
                "nonce": payload.get("nonce"),
                "calls": aux.calls,
                "unparsed_calls": aux.unparsed,
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


# --------------------------------------------------------------------------- 写回用量 / 预检


async def writeback_usage(
    deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]
) -> None:
    """每个评测用户:带写回的评测智能体在 ``since`` 之后记下的平台开销(后台写回)token,按型号合计。
    只出数字(审查 H4:本地按它结算写回的预留,不再按上限)。"""
    since = datetime.fromisoformat(payload["since"])
    now = datetime.now(UTC)
    for entry in payload["users"]:
        tenant_id = UUID(entry["tenant_id"])
        user_id = await _user_id(deps, tenant_id, entry["user"])
        with _scope(tenant_id, user_id):
            rows = await deps.token_usage.list_for_tenant_window(
                tenant_id=tenant_id, start=since, end=now, user_id=user_id
            )
        totals: dict[str, dict[str, int]] = {}
        for row in rows:
            if row.usage_kind != PLATFORM_OVERHEAD or row.agent_name != entry["agent_name"]:
                continue
            t = totals.setdefault(
                row.model,
                {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "rows": 0},
            )
            t["input_tokens"] += int(row.input_tokens)
            t["output_tokens"] += int(row.output_tokens)
            t["cache_read_tokens"] += int(row.cache_read_tokens)
            t["rows"] += 1
        emit(
            json.dumps(
                {
                    "op": "writeback_usage",
                    "key": entry["key"],
                    "models": [{"model": m, **v} for m, v in sorted(totals.items())],
                }
            )
        )


async def preflight(deps: Deps, payload: Mapping[str, Any], emit: Callable[[str], None]) -> None:
    """评测租户的记忆维护开关(只出开关与数字,审查 M5)。整理(凑簇汇总)**没有按租户的开关**,
    平台每隔 ``consolidation_interval_s`` 秒扫一轮;记忆清理(``memory_purge_enabled``)与到期复核
    (``memory_predictive_review_enabled``)有,评测要求这两项都关。"""
    from control_plane.tenancy import TenantConfigNotConfiguredError

    tenant_id = UUID(payload["tenant_id"])
    try:
        with _scope(tenant_id):
            cfg = await deps.tenant_config.get(tenant_id=tenant_id)
        purge, min_age, review = (
            bool(cfg.memory_purge_enabled),
            int(cfg.memory_purge_min_age_days),
            bool(cfg.memory_predictive_review_enabled),
        )
        configured = True
    except TenantConfigNotConfiguredError:
        purge, min_age, review, configured = True, 30, False, False
    emit(
        json.dumps(
            {
                "op": "preflight",
                "tenant_configured": configured,
                "memory_purge_enabled": purge,
                "memory_purge_min_age_days": min_age,
                "memory_predictive_review_enabled": review,
                "consolidation_switch_per_tenant": False,
                "consolidation_interval_s": deps.consolidation_interval_s,
                "ok": not purge and not review,
            }
        )
    )


def assert_no_background_tasks(*, baseline: set[Any]) -> None:
    """装配完以后不许有任何后台任务(审查 H1):有就是某个 worker / sweep 起来了 —— 它可能去认领
    真租户排队的运行或写回任务。直接退出,不往下做。"""
    current = asyncio.current_task()
    extra = [t for t in asyncio.all_tasks() if t is not current and t not in baseline]
    if extra:
        names = sorted({t.get_name() for t in extra})
        raise SystemExit(
            f"b300 in-pod: background task(s) started during bootstrap {names}; stopped"
        )


@contextlib.asynccontextmanager
async def bootstrap() -> AsyncIterator[Deps]:
    """控制面进程里的最小装配(审查 H1):**不建 app、不进 lifespan**,所以不会起排队运行 worker、
    后台写回 worker、写回失败重试、质量监控、知识库恢复、webhook、评测 worker、工作区清理、
    审批 / 对话镜像 sweep 等任何后台任务。只按 ``create_app`` 同样的取法拼出这几样:存储
    (``_build_sql_stores``)、密钥库、租户配置、平台凭据与凭据解析、嵌入配置与嵌入、审计、
    token 用量;整理任务类只在要用时建、从不 ``start()``。装配前后各查一次后台任务。"""
    import httpx

    from control_plane.app import _build_secret_store, _build_sql_stores
    from control_plane.audit import TenantConfigPiiResolver, build_default_audit_logger
    from control_plane.aux_model_adapter import make_llm_router_aux_model
    from control_plane.credential_value_cache import CredentialValueCache
    from control_plane.memory_consolidator import MemoryConsolidator, make_consolidator_embedder
    from control_plane.platform_embedding_config import PlatformEmbeddingConfigService
    from control_plane.platform_secrets import PlatformSecretsService
    from control_plane.runtime import DynamicResolvingEmbedder
    from control_plane.settings import Settings
    from control_plane.tenancy import TenantConfigService
    from control_plane.tenant_secret_overlay import TenantOverlayCredentialsResolver

    baseline = set(asyncio.all_tasks())
    settings = Settings()
    stores = _build_sql_stores(settings)
    secret_store = _build_secret_store(settings, stores)
    pii = TenantConfigPiiResolver()
    audit = build_default_audit_logger(store=stores.audit_log, pii_fields_resolver=pii)
    ttl = float(settings.tenant_config_cache_ttl_s)
    tenant_config = TenantConfigService(store=stores.tenant_config, audit_logger=audit, ttl_s=ttl)
    pii.bind(tenant_config)
    secrets = PlatformSecretsService(store=stores.platform_secret, settings=settings, ttl_s=ttl)
    resolver = TenantOverlayCredentialsResolver(
        tenant_provider_view=secrets.effective_provider_credentials_for,
        tenant_tool_view=secrets.effective_tool_credentials_for,
        tenant_provider_keys_view=secrets.effective_provider_secret_refs_for,
        platform_provider_credentials=settings.effective_platform_provider_credentials,
        platform_tool_credentials=settings.effective_platform_tool_credentials,
        tenant_config_getter=tenant_config,
        platform_provider_getter=secrets.effective_provider_credentials,
        platform_tool_getter=secrets.effective_tool_credentials,
    )
    cache = CredentialValueCache()
    http = httpx.AsyncClient()
    embedder = DynamicResolvingEmbedder(
        config_service=PlatformEmbeddingConfigService(
            store=stores.platform_embedding_config, settings=settings, ttl_seconds=ttl
        ),
        resolver=resolver,
        secret_store=secret_store,
        http=http,
        secret_cache=cache,
        max_input_bytes=settings.embedding_max_input_bytes,
    )

    def factory(max_calls: int) -> tuple[Any, CappedAux]:
        inner = make_llm_router_aux_model(
            resolver=resolver,
            secret_store=secret_store,
            default_provider=settings.memory_consolidator_default_aux_provider,
            default_model=settings.memory_consolidator_default_aux_model,
            secret_cache=cache,
        )
        aux = CappedAux(inner, max_calls)
        consolidator = MemoryConsolidator(
            memory_store=stores.memory,
            tenant_config_service=tenant_config,
            audit_logger=audit,
            aux_model=aux,
            embedder=make_consolidator_embedder(embedder),
            default_aux_model_name=settings.memory_consolidator_default_aux_model,
            # 与线上同样落 token_usage(usage_kind = memory_consolidation)。
            usage_store=stores.token_usage,
        )
        return consolidator, aux

    assert_no_background_tasks(baseline=baseline)
    try:
        yield Deps(
            memory=stores.memory,
            users=stores.tenant_user,
            run_events=stores.run_event,
            embedder=embedder,
            consolidator_factory=factory,
            consolidation_model=settings.memory_consolidator_default_aux_model,
            token_usage=stores.token_usage,
            tenant_config=tenant_config,
            consolidation_interval_s=float(settings.memory_consolidator_interval_s),
        )
        assert_no_background_tasks(baseline=baseline)
    finally:
        await http.aclose()
        await stores.engine.dispose()


_HANDLERS = {
    "seed": seed,
    "recalled": recalled,
    "candidates": candidates,
    "consolidate": consolidate,
    "state": state,
    "writeback_usage": writeback_usage,
    "preflight": preflight,
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
