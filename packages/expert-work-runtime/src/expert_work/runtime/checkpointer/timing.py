"""Checkpoint-IO timing wrapper — Stream HX-4 (Mini-ADR HX-D3).

The LangGraph savers expose no observability seam, so the factory wraps
whatever backend it builds in :class:`TimingCheckpointSaver`: a
delegating subclass that times the hot IO methods (``aput`` /
``aput_writes`` / ``aget_tuple``) into ``expert_work_checkpoint_op_seconds{op}``
and forwards everything else verbatim. ``alist`` is a paginated admin
read, not per-step IO — delegated untimed.

The observation layer **never fails the call**: a broken metrics
registry costs the sample, not the checkpoint (fail-open axiom).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Collection, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)

from expert_work.common.observability import expert_work_histogram

logger = logging.getLogger(__name__)

_checkpoint_op_seconds = expert_work_histogram(
    "expert_work_checkpoint_op_seconds",
    "Wall-clock seconds per checkpointer IO call, labelled by operation.",
    ("op",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 10.0),
)


def _observe(op: str, started: float) -> None:
    try:
        _checkpoint_op_seconds.labels(op=op).observe(time.monotonic() - started)
    except Exception:  # pragma: no cover - registry breakage is exotic
        logger.warning("checkpoint_timing.observe_failed op=%s", op, exc_info=True)


class TimingCheckpointSaver(BaseCheckpointSaver[Any]):
    """Delegating saver that times the per-step IO of ``inner``."""

    def __init__(self, inner: BaseCheckpointSaver[Any]) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner

    # -- timed hot path ----------------------------------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        started = time.monotonic()
        try:
            return await self._inner.aget_tuple(config)
        finally:
            _observe("aget_tuple", started)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> RunnableConfig:
        started = time.monotonic()
        try:
            return await self._inner.aput(config, checkpoint, metadata, new_versions)
        finally:
            _observe("aput", started)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        started = time.monotonic()
        try:
            await self._inner.aput_writes(config, writes, task_id, task_path)
        finally:
            _observe("aput_writes", started)

    # -- untimed delegation -------------------------------------------------

    async def aget(self, config: RunnableConfig) -> Checkpoint | None:
        # The base impl routes through aget_tuple — delegate so the
        # inner saver's own override (if any) stays in effect; the
        # timing lands via our aget_tuple only when the base path runs.
        return await self._inner.aget(config)

    def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        return self._inner.alist(config, filter=filter, before=before, limit=limit)

    async def adelete_thread(self, thread_id: str) -> None:
        await self._inner.adelete_thread(thread_id)

    async def adelete_for_runs(self, run_ids: Sequence[str]) -> None:
        await self._inner.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        await self._inner.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        await self._inner.aprune(thread_ids, strategy=strategy)

    # -- sync surface (unused in our async stack; plain delegation) ---------

    def get(self, config: RunnableConfig) -> Checkpoint | None:
        return self._inner.get(config)

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self._inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: Any,
    ) -> RunnableConfig:
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._inner.put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        self._inner.delete_thread(thread_id)

    def delete_for_runs(self, run_ids: Sequence[str]) -> None:
        self._inner.delete_for_runs(run_ids)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        self._inner.copy_thread(source_thread_id, target_thread_id)

    def prune(self, thread_ids: Sequence[str], *, strategy: str = "keep_latest") -> None:
        self._inner.prune(thread_ids, strategy=strategy)

    def get_next_version(self, current: Any, channel: None = None) -> Any:
        return self._inner.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist: Collection[tuple[str, ...]]) -> BaseCheckpointSaver[Any]:
        # B-161 — strict-msgpack ``StateGraph.compile`` registers the state
        # schema's types through this. The inherited version swaps the
        # serde on a copy of *this* wrapper, but every IO call goes to
        # ``_inner`` — widen the inner saver's serializer instead.
        inner = self._inner.with_allowlist(extra_allowlist)
        if inner is self._inner:
            return self
        return TimingCheckpointSaver(inner)
