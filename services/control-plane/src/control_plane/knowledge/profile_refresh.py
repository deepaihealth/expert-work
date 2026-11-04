"""B-267 step 2 —— running base-profile refreshes (and the probe calibration).

:class:`ProfileRefreshWorker` is its own loop (not part of the ingest crash
recovery): each pass claims due refreshes across tenants — row security is
bypassed for the claim only — and runs them one by one under the base's tenant.

A refresh (:class:`ProfileRefresher`):

* **profile** — skipped when the digest is the one the stored profile came
  from (no spend, nothing rewritten; a stale mark is cleared, since that
  profile cannot describe a document outside its digest). Otherwise ONE
  plain call of the auxiliary model with an output cap
  (:data:`~control_plane.knowledge.profile.PROFILE_OUTPUT_TOKENS_MAX`), under
  a timeout shorter than the claim's lease, billed to the tenant as
  ``knowledge_ingest`` whatever the answer; the answer is validated here, so
  no schema retry calls are billed unseen. An unusable answer fails the
  refresh, which is retried once at most.
* **calibration** — the per-turn probe's vector threshold: the 90th
  percentile of the top scores a fixed list of generic, content-free queries
  reaches in the base. Only while the base's vectors are the live platform
  model's. It runs after — and apart from — the profile: a failed calibration
  keeps the paid profile and is retried alone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol
from uuid import UUID

from control_plane.knowledge.profile import (
    CALIBRATION_QUERIES,
    PROFILE_DETAILED_DOCUMENTS,
    PROFILE_MAX_HEADINGS,
    PROFILE_MAX_TITLES,
    PROFILE_OUTPUT_TOKENS_MAX,
    AuxModelChoice,
    parse_profile_reply,
    percentile,
    profile_prompt,
    render_digest,
)
from control_plane.knowledge.usage import IngestUsageRecorder, billed_embeddings
from control_plane.memory_consolidator import ConsolidatorLLMReply
from expert_work.common.observability import expert_work_counter
from expert_work.persistence import KnowledgeStore
from expert_work.persistence.knowledge.profiles import PROFILE_LEASE, ProfileClaim
from expert_work.persistence.rls import bypass_rls_var, current_tenant_id_var
from expert_work.protocol import KnowledgeBase
from expert_work.protocol.knowledge_profile import KnowledgeProfile, ProbeCalibration
from orchestrator.llm import Embedder

logger = logging.getLogger(__name__)

#: The profile call and the calibration each get this long — together well
#: inside the claim's lease, so a slow vendor cannot outlive the claim.
PROFILE_CALL_TIMEOUT_S: Final = 180.0
CALIBRATION_TIMEOUT_S: Final = 120.0
#: How long shutdown waits for a refresh in flight.
STOP_GRACE_S: Final = 20.0
assert PROFILE_CALL_TIMEOUT_S + CALIBRATION_TIMEOUT_S < PROFILE_LEASE.total_seconds()  # noqa: S101

_refreshes = expert_work_counter(
    "expert_work_knowledge_profile_refresh_total",
    "Knowledge-base profile refreshes by outcome "
    "(generated / unchanged / calibrated / empty / calibration_failed / failed).",
    label_names=("outcome",),
)


class ProfileAuxModel(Protocol):
    """One plain call of the auxiliary model, with an output cap."""

    async def __call__(
        self,
        *,
        prompt: str,
        model: str | None,
        tenant_id: UUID,
        max_tokens: int | None = None,
        thinking_off: bool = False,
    ) -> ConsolidatorLLMReply: ...


class EmbeddingConfig(Protocol):
    async def __call__(self) -> tuple[str, str] | None:
        """The live platform embedding model ``(provider, model)``."""


class UnusableProfileError(ValueError):
    """The model's (billed) answer is not a usable profile."""


@dataclass
class ProfileRefresher:
    """Generates profiles and calibrations for claimed refreshes."""

    store: KnowledgeStore
    aux: ProfileAuxModel
    aux_model: AuxModelChoice
    embedder: Embedder
    embedding_config: EmbeddingConfig
    usage: IngestUsageRecorder | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    async def refresh(self, claim: ProfileClaim) -> None:
        """Run one claimed refresh; a failure keeps the stored profile."""
        base = await self._base(claim)
        if base is None:
            return
        try:
            profile = None if claim.calibrate_only else await self.generate(base)
        except Exception:
            await self._failed(claim, "failed")
            return
        try:
            calibration = await asyncio.wait_for(self.calibrate(base), CALIBRATION_TIMEOUT_S)
        except Exception:
            if claim.calibrate_only:
                await self._failed(claim, "calibration_failed")
                return
            logger.warning("knowledge.profile_calibration_failed kb=%s", claim.kb_id, exc_info=True)
            _refreshes.labels(outcome="calibration_failed").inc()
            await self._finish(claim, profile, None, retry_calibration=True)
            return
        await self._finish(claim, profile, calibration)

    async def _finish(
        self,
        claim: ProfileClaim,
        profile: KnowledgeProfile | None,
        calibration: ProbeCalibration | None,
        *,
        retry_calibration: bool = False,
    ) -> None:
        await self.store.finish_profile_refresh(
            tenant_id=claim.tenant_id,
            kb_id=claim.kb_id,
            holder=claim.holder,
            marks=claim.marks,
            profile=profile,
            calibration=calibration,
            now=self.now(),
            retry_calibration=retry_calibration,
        )

    async def _failed(self, claim: ProfileClaim, outcome: str) -> None:
        _refreshes.labels(outcome=outcome).inc()
        # Never the documents' text or the answer: ids only.
        logger.warning("knowledge.profile_refresh_failed kb=%s", claim.kb_id, exc_info=True)
        await self.store.fail_profile_refresh(
            tenant_id=claim.tenant_id, kb_id=claim.kb_id, holder=claim.holder, now=self.now()
        )

    async def _base(self, claim: ProfileClaim) -> KnowledgeBase | None:
        bases = await self.store.list_bases(tenant_id=claim.tenant_id)
        return next((base for base in bases if base.id == claim.kb_id), None)

    async def generate(self, base: KnowledgeBase) -> KnowledgeProfile | None:
        """The base's profile — ``None`` when nothing is to be stored (no ready
        document); the stored one, uncalled, when the digest is unchanged.
        :raises UnusableProfileError: the (billed) answer is not usable."""
        digest = await self.store.profile_digest(
            tenant_id=base.tenant_id,
            kb_id=base.id,
            max_titles=PROFILE_MAX_TITLES,
            detailed=PROFILE_DETAILED_DOCUMENTS,
            max_headings=PROFILE_MAX_HEADINGS,
        )
        rendered = render_digest(digest)
        if not rendered.represented:
            _refreshes.labels(outcome="empty").inc()
            return None
        stored = base.profile.profile if base.profile is not None else None
        if stored is not None and stored.digest_sha256 == rendered.sha256:
            _refreshes.labels(outcome="unchanged").inc()
            return stored
        # A call cut off by the timeout may still be billed by the vendor; the
        # adapter returns no usage for it, so nothing can be recorded then.
        reply = await asyncio.wait_for(
            self.aux(
                prompt=profile_prompt(rendered),
                model=self.aux_model.model,
                tenant_id=base.tenant_id,
                max_tokens=PROFILE_OUTPUT_TOKENS_MAX,
                thinking_off=True,
            ),
            PROFILE_CALL_TIMEOUT_S,
        )
        await self._bill(base.tenant_id, reply)
        generated = parse_profile_reply(reply.text)
        if generated is None:
            raise UnusableProfileError("the model's profile answer is not usable")
        _refreshes.labels(outcome="generated").inc()
        return KnowledgeProfile(
            **generated.model_dump(),
            generated_at=self.now(),
            source_doc_count=rendered.represented,
            model=self.aux_model.key,
            digest_sha256=rendered.sha256,
        )

    async def _bill(self, tenant_id: UUID, reply: ConsolidatorLLMReply) -> None:
        if self.usage is not None:
            await self.usage.record(
                tenant_id=tenant_id,
                provider=self.aux_model.provider,
                model=reply.model or self.aux_model.model,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
            )

    async def calibrate(self, base: KnowledgeBase) -> ProbeCalibration | None:
        """The base's probe threshold — ``None`` when its vectors are not the
        live platform model's (or it has no vector), so a score now would not
        be comparable."""
        live = await self.embedding_config()
        stamped = (base.embedding_provider, base.embedding_model)
        if live is None or stamped != live:
            return None
        key = f"{live[0]}/{live[1]}"
        tops: list[float] = []
        async with billed_embeddings(self.usage, tenant_id=base.tenant_id, model_key=key):
            for query in CALIBRATION_QUERIES:
                vector = await _embed_query(self.embedder, query, base.tenant_id)
                hits = await self.store.search_scored(
                    tenant_id=base.tenant_id, kb_ids=[base.id], query_embedding=vector, limit=1
                )
                if hits:
                    tops.append(hits[0].score)
        if not tops:
            return None
        _refreshes.labels(outcome="calibrated").inc()
        return ProbeCalibration(
            embedding_model=key,
            vector_threshold=percentile(tops),
            queries=len(tops),
            computed_at=self.now(),
        )


async def _embed_query(embedder: Embedder, query: str, tenant_id: UUID) -> tuple[float, ...]:
    """Embedded the way a knowledge search embeds its query (with the model's
    query-side marker when the embedder has one), so the scores compare."""
    embed_query: Callable[..., Awaitable[tuple[float, ...]]] | None = getattr(
        embedder, "embed_query", None
    )
    if embed_query is not None:
        return await embed_query(query, tenant_id=tenant_id)
    return (await embedder.embed([query], tenant_id=tenant_id))[0]


class ProfileRefreshWorker:
    """Background loop running due profile refreshes, ``per_pass`` a pass."""

    def __init__(
        self, *, refresher: ProfileRefresher, interval_s: float, per_pass: int = 2
    ) -> None:
        if interval_s <= 0:
            msg = "interval_s must be positive"
            raise ValueError(msg)
        self._refresher = refresher
        self._interval_s = interval_s
        self._per_pass = per_pass
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="knowledge-profile-refresh")

    async def stop(self, *, grace_s: float = STOP_GRACE_S) -> None:
        """Stop the loop; a refresh in flight gets ``grace_s`` to finish (a
        claim cancelled at shutdown counts as an attempt — one is left)."""
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=grace_s)
        except (TimeoutError, asyncio.CancelledError, Exception):
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: S110 - shutting down
                pass
        finally:
            self._task = None

    async def run_once(self) -> int:
        """Claim (cross-tenant: row security bypassed for the claim only) and
        run due refreshes, each under its base's tenant; return the claims."""
        bypass = bypass_rls_var.set(True)
        try:
            claims = await self._refresher.store.claim_profile_refreshes(
                now=self._refresher.now(), limit=self._per_pass
            )
        finally:
            bypass_rls_var.reset(bypass)
        for claim in claims:
            tenant = current_tenant_id_var.set(claim.tenant_id)
            try:
                await self._refresher.refresh(claim)
            except Exception:
                logger.exception("knowledge.profile_refresh_crashed kb=%s", claim.kb_id)
            finally:
                current_tenant_id_var.reset(tenant)
        return len(claims)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("knowledge.profile_refresh_pass_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                pass  # interval elapsed without a stop request: run the next pass
