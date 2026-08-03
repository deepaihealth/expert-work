"""Token-usage store — Stream G.9.

Persists one row per LLM call so dashboards + budget queries can roll
up tokens per ``(tenant, agent_name, agent_version, model)`` over any
time window. Two implementations behind one ABC, matching the audit-
log / feedback store pattern:

* :class:`InMemoryTokenUsageStore` — dev / unit tests (no durability).
* :class:`DbTokenUsageStore` — Postgres-backed; RLS-scoped via the
  sessionmaker wrapper (``app.tenant_id`` GUC set after BEGIN, so the
  ``token_usage`` RLS policy applies).

The C.5 ``token_budget_ledger`` table (one row per ``(tenant, month)``
rollup) remains independent — G.9 is for analytics + per-agent
visibility, the ledger is for budget enforcement's fast path. M1-D
will revisit whether to fold the ledger into roll-ups over this table.
"""

from __future__ import annotations

import abc
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from expert_work.persistence.models.token_usage import TokenUsageRow

# Cross-tenant usage read (W4) must ``SET LOCAL ROLE`` to a BYPASSRLS role:
# ``token_usage`` is FORCE ROW LEVEL SECURITY (migration 0036) and the
# application's main connection role is NOT guaranteed BYPASSRLS, so merely
# skipping the ``app.tenant_id`` GUC leaves the policy as ``tenant_id = NULL``
# → zero rows. Assuming ``audit_reader`` (NOLOGIN BYPASSRLS, migration 0005;
# GRANTed SELECT on this table by migration 0140) is what actually lets the
# read cross tenants — same precedent as the ledger / tenant_member readers.
# ``SET LOCAL`` is transaction-scoped, so it lifts on commit/rollback.
_SET_AUDIT_READER_ROLE = text("SET LOCAL ROLE audit_reader")


@dataclass(frozen=True)
class TokenTotals:
    """Aggregated token usage for one trace.

    A ``trace_id`` joins ``agent_run`` ↔ ``token_usage`` (there is no
    ``run_id`` column), so this is the per-run token summary the Runs
    list + detail render. ``llm_calls`` is the number of usage rows
    (≈ LLM calls); ``models`` is the distinct models the run touched.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0
    models: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class TokenUsageRecord:
    """One LLM-call row. ``id`` / ``observed_at`` are ``None`` pre-insert."""

    tenant_id: UUID
    agent_name: str
    agent_version: str
    model: str
    # Stream Y-3 — additive nullable provider so Y4 can price by
    # ``(provider, model)``. ``None`` for legacy rows.
    provider: str | None = None
    # Stream Agent-Templates (M1-5a) — the end-user this LLM call ran for
    # (``tenant_user.id``), for per-user cost attribution. ``None`` for runs with
    # no user context (system / preview builds).
    user_id: UUID | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    trace_id: str | None = None
    # SE-16 (SE-A43) — what spent the tokens ('conversation' | 'skill_evolution').
    usage_kind: str = "conversation"
    id: int | None = None
    observed_at: datetime | None = None


class TokenUsageStore(abc.ABC):
    """Append + read-by-tenant for per-call token usage."""

    @abc.abstractmethod
    async def insert(self, record: TokenUsageRecord) -> TokenUsageRecord:
        """Persist one row; return it with ``id`` + ``observed_at`` filled."""

    @abc.abstractmethod
    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        model: str | None = None,
        limit: int = 100,
    ) -> Sequence[TokenUsageRecord]:
        """Return rows for ``tenant_id``, newest first.

        Isolation is an explicit ``tenant_id`` SQL predicate — the RLS
        backstop is bypassed under the app's superuser connection, so the
        predicate is the actual cross-tenant check at the data layer.
        """

    @abc.abstractmethod
    async def list_for_tenant_window(
        self,
        *,
        tenant_id: UUID,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        """Return **all** rows with ``start <= observed_at < end`` — Stream Y4.

        Half-open window (start inclusive, end exclusive). No row cap: the Y4
        rollup must price every usage row in a month, so this reads the full
        window (the SQL impl pages internally). Filtered by an explicit
        ``tenant_id`` SQL predicate, like :meth:`list_for_tenant` (RLS is
        bypassed at runtime). ``user_id`` narrows to one end-user
        (conversation-centric IA M2 — the user-detail usage tab).
        """

    @abc.abstractmethod
    async def list_window_all_tenants(
        self,
        *,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        """Return **every** tenant's rows with ``start <= observed_at < end`` — W4.

        Cross-tenant sibling of :meth:`list_for_tenant_window` (identical
        half-open window + optional ``user_id`` narrowing, minus the tenant
        predicate), feeding the ``GET /v1/usage/tokens`` ``tenant_id=*``
        aggregate. Caller MUST have resolved a ``CrossTenant`` scope
        (system_admin only) and wrapped the call in ``applied_scope`` /
        ``bypass_rls_session``; the SQL implementation additionally assumes
        the ``audit_reader`` BYPASSRLS role because ``token_usage`` is
        FORCE-RLS (see ``_SET_AUDIT_READER_ROLE``).
        """

    @abc.abstractmethod
    async def totals_by_trace_ids(self, trace_ids: Sequence[str]) -> dict[str, TokenTotals]:
        """Sum token usage grouped by ``trace_id`` for the given ids.

        Feeds the Runs list + detail — ``trace_id`` joins ``agent_run`` ↔
        ``token_usage`` (no ``run_id`` column). Tenant scoping rides on the RLS
        context, like :meth:`list_for_tenant`. Ids with no recorded usage
        (legacy / auto-triggered runs) are simply absent from the result; the
        caller treats a missing id as zero.
        """

    @abc.abstractmethod
    async def totals_by_users(
        self,
        *,
        agent_name: str,
        agent_version: str,
        user_ids: Sequence[UUID],
    ) -> dict[UUID, TokenTotals]:
        """Sum token usage per ``user_id`` for one agent version.

        Feeds the conversation-centric M2 users rollup
        (``docs/design/conversation-centric-ia.md`` §5) — ``token_usage``
        carries ``agent_name`` / ``agent_version`` / ``user_id`` directly
        (migration 0096 + M1-5a), so this is a plain GROUP BY with **no
        trace join**. Tenant scoping rides on the RLS context, like
        :meth:`list_for_tenant`. Users with no recorded usage are absent
        from the result; the caller treats a missing id as zero.
        """

    @abc.abstractmethod
    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        """Phase 3a (purge_user) — null the ``user_id`` on a user's rows, KEEP them.

        Sets ``user_id = NULL`` on every ``token_usage`` row for ``(tenant_id,
        user_id)`` and returns the count updated (0 when none / re-purge). The
        rows are NOT deleted — token usage is billing / analytics data owned by
        the tenant; only the per-user PII link is severed. Tenant- AND
        user-scoped — the predicate never touches another tenant's or user's
        rows, and rows already NULL are left as-is."""


class InMemoryTokenUsageStore(TokenUsageStore):
    """In-memory :class:`TokenUsageStore` — dev / unit tests."""

    def __init__(self) -> None:
        self._rows: list[TokenUsageRecord] = []
        self._ids = itertools.count(1)

    async def insert(self, record: TokenUsageRecord) -> TokenUsageRecord:
        stored = replace(record, id=next(self._ids), observed_at=datetime.now(UTC))
        self._rows.append(stored)
        return stored

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        model: str | None = None,
        limit: int = 100,
    ) -> Sequence[TokenUsageRecord]:
        rows = [r for r in self._rows if r.tenant_id == tenant_id]
        if agent_name is not None:
            rows = [r for r in rows if r.agent_name == agent_name]
        if model is not None:
            rows = [r for r in rows if r.model == model]
        rows.sort(key=lambda r: r.id or 0, reverse=True)
        return rows[:limit]

    async def list_for_tenant_window(
        self,
        *,
        tenant_id: UUID,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        return [
            r
            for r in self._rows
            if r.tenant_id == tenant_id
            and r.observed_at is not None
            and start <= r.observed_at < end
            and (user_id is None or r.user_id == user_id)
        ]

    async def list_window_all_tenants(
        self,
        *,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        # Same predicate as ``list_for_tenant_window`` minus the tenant clause.
        return [
            r
            for r in self._rows
            if r.observed_at is not None
            and start <= r.observed_at < end
            and (user_id is None or r.user_id == user_id)
        ]

    async def totals_by_trace_ids(self, trace_ids: Sequence[str]) -> dict[str, TokenTotals]:
        wanted = {t for t in trace_ids if t}
        inputs: dict[str, int] = {}
        outputs: dict[str, int] = {}
        cache_create: dict[str, int] = {}
        cache_read: dict[str, int] = {}
        calls: dict[str, int] = {}
        models: dict[str, set[str]] = {}
        for r in self._rows:
            tid = r.trace_id
            if tid is None or tid not in wanted:
                continue
            inputs[tid] = inputs.get(tid, 0) + r.input_tokens
            outputs[tid] = outputs.get(tid, 0) + r.output_tokens
            cache_create[tid] = cache_create.get(tid, 0) + r.cache_creation_tokens
            cache_read[tid] = cache_read.get(tid, 0) + r.cache_read_tokens
            calls[tid] = calls.get(tid, 0) + 1
            models.setdefault(tid, set()).add(r.model)
        return {
            tid: TokenTotals(
                input_tokens=inputs[tid],
                output_tokens=outputs[tid],
                cache_creation_tokens=cache_create[tid],
                cache_read_tokens=cache_read[tid],
                llm_calls=calls[tid],
                models=tuple(sorted(m for m in models[tid] if m)),
            )
            for tid in calls
        }

    async def totals_by_users(
        self,
        *,
        agent_name: str,
        agent_version: str,
        user_ids: Sequence[UUID],
    ) -> dict[UUID, TokenTotals]:
        wanted = set(user_ids)
        grouped: dict[UUID, list[TokenUsageRecord]] = {}
        for r in self._rows:
            if (
                r.user_id is None
                or r.user_id not in wanted
                or r.agent_name != agent_name
                or r.agent_version != agent_version
            ):
                continue
            grouped.setdefault(r.user_id, []).append(r)
        return {
            uid: TokenTotals(
                input_tokens=sum(r.input_tokens for r in rows),
                output_tokens=sum(r.output_tokens for r in rows),
                cache_creation_tokens=sum(r.cache_creation_tokens for r in rows),
                cache_read_tokens=sum(r.cache_read_tokens for r in rows),
                llm_calls=len(rows),
                models=tuple(sorted({r.model for r in rows if r.model})),
            )
            for uid, rows in grouped.items()
        }

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        updated = 0
        for idx, r in enumerate(self._rows):
            if r.tenant_id == tenant_id and r.user_id == user_id:
                self._rows[idx] = replace(r, user_id=None)
                updated += 1
        return updated


class DbTokenUsageStore(TokenUsageStore):
    """Postgres-backed :class:`TokenUsageStore`.

    ``session_factory`` must be the RLS-wrapped sessionmaker
    (:func:`expert_work.persistence.rls.build_rls_sessionmaker`): the
    tenant GUC is set after BEGIN, so the ``token_usage`` RLS policy
    scopes every row to the calling tenant. An ``insert`` whose tenant
    context is unset fails the policy ``WITH CHECK`` — RLS, not trust.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def insert(self, record: TokenUsageRecord) -> TokenUsageRecord:
        async with self._sf() as session:
            row = TokenUsageRow(
                tenant_id=record.tenant_id,
                agent_name=record.agent_name,
                agent_version=record.agent_version,
                model=record.model,
                provider=record.provider,
                user_id=record.user_id,
                trace_id=record.trace_id,
                usage_kind=record.usage_kind,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cache_creation_tokens=record.cache_creation_tokens,
                cache_read_tokens=record.cache_read_tokens,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            stored = _row_to_record(row)
            await session.commit()
            return stored

    async def list_for_tenant(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        model: str | None = None,
        limit: int = 100,
    ) -> Sequence[TokenUsageRecord]:
        async with self._sf() as session:
            # Explicit tenant predicate: the RLS backstop is bypassed at runtime
            # (the app connects as a superuser DB role), so app-layer filtering
            # is the actual cross-tenant isolation — not the tenant GUC alone.
            stmt = select(TokenUsageRow).where(TokenUsageRow.tenant_id == tenant_id)
            if agent_name is not None:
                stmt = stmt.where(TokenUsageRow.agent_name == agent_name)
            if model is not None:
                stmt = stmt.where(TokenUsageRow.model == model)
            stmt = stmt.order_by(TokenUsageRow.id.desc()).limit(limit)
            result = await session.execute(stmt)
            return [_row_to_record(r) for r in result.scalars().all()]

    async def list_for_tenant_window(
        self,
        *,
        tenant_id: UUID,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        # Keyset pagination by ascending id: no row cap, bounded memory per
        # page. ``observed_at`` is monotonic with insert but not guaranteed
        # unique, so we page on the surrogate ``id`` instead.
        #
        # NOTE (Y4 operability follow-up): all pages stream inside ONE session,
        # so a very large tenant-month holds a read transaction (+ connection)
        # open for the whole scan. Acceptable for the M1 rollup cadence and a
        # direct-to-Postgres connection (see the rollup job's settings); revisit
        # with per-page sessions if the job runs behind transaction-mode pooling
        # or tenants accumulate millions of rows per month.
        page_size = 5000
        out: list[TokenUsageRecord] = []
        after_id = 0
        async with self._sf() as session:
            while True:
                stmt = (
                    select(TokenUsageRow)
                    .where(
                        # Explicit tenant predicate — RLS is bypassed under the
                        # app's superuser connection, so this is the isolation.
                        TokenUsageRow.tenant_id == tenant_id,
                        TokenUsageRow.observed_at >= start,
                        TokenUsageRow.observed_at < end,
                        TokenUsageRow.id > after_id,
                    )
                    .order_by(TokenUsageRow.id.asc())
                    .limit(page_size)
                )
                if user_id is not None:
                    stmt = stmt.where(TokenUsageRow.user_id == user_id)
                rows = (await session.execute(stmt)).scalars().all()
                if not rows:
                    break
                out.extend(_row_to_record(r) for r in rows)
                after_id = rows[-1].id
                if len(rows) < page_size:
                    break
        return out

    async def list_window_all_tenants(
        self,
        *,
        start: datetime,
        end: datetime,
        user_id: UUID | None = None,
    ) -> Sequence[TokenUsageRecord]:
        # Same keyset pagination as ``list_for_tenant_window``, minus the
        # tenant predicate. ``token_usage`` is FORCE-RLS, so the first
        # statement assumes the BYPASSRLS ``audit_reader`` role (see
        # ``_SET_AUDIT_READER_ROLE``); ``SET LOCAL`` is transaction-scoped
        # and the whole loop runs inside ONE session/transaction, so one
        # SET covers every page.
        #
        # NOTE (Y4 operability follow-up, as on ``list_for_tenant_window``):
        # all pages stream inside ONE session, so a very large window holds a
        # read transaction (+ connection) open for the whole scan — and this
        # variant scans EVERY tenant's rows, so it grows with the whole
        # platform, not one tenant. Acceptable for the W4 usage-aggregate
        # cadence; revisit with per-page sessions (or push the rollup into a
        # SQL GROUP BY — recorded follow-up) if it runs behind
        # transaction-mode pooling or the table accumulates millions of rows
        # per month. The window predicate has no leading ``tenant_id``, so it
        # depends on the ``observed_at``-leading ``token_usage_time_idx``
        # (migration 0140) — the 0036 indexes all lead with ``tenant_id``.
        page_size = 5000
        out: list[TokenUsageRecord] = []
        after_id = 0
        async with self._sf() as session:
            await session.execute(_SET_AUDIT_READER_ROLE)
            while True:
                stmt = (
                    select(TokenUsageRow)
                    .where(
                        TokenUsageRow.observed_at >= start,
                        TokenUsageRow.observed_at < end,
                        TokenUsageRow.id > after_id,
                    )
                    .order_by(TokenUsageRow.id.asc())
                    .limit(page_size)
                )
                if user_id is not None:
                    stmt = stmt.where(TokenUsageRow.user_id == user_id)
                rows = (await session.execute(stmt)).scalars().all()
                if not rows:
                    break
                out.extend(_row_to_record(r) for r in rows)
                after_id = rows[-1].id
                if len(rows) < page_size:
                    break
        return out

    async def totals_by_trace_ids(self, trace_ids: Sequence[str]) -> dict[str, TokenTotals]:
        ids = [t for t in dict.fromkeys(trace_ids) if t]  # dedup, drop empty
        if not ids:
            return {}
        stmt = (
            select(
                TokenUsageRow.trace_id,
                func.coalesce(func.sum(TokenUsageRow.input_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.output_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.cache_creation_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.cache_read_tokens), 0),
                func.count(),
                func.array_agg(func.distinct(TokenUsageRow.model)),
            )
            # Tenant scoping rides on RLS (the sessionmaker sets the tenant GUC),
            # so ``trace_id`` collisions across tenants can't leak — a foreign
            # tenant's rows are invisible to this session.
            .where(TokenUsageRow.trace_id.in_(ids))
            .group_by(TokenUsageRow.trace_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {
            row[0]: TokenTotals(
                input_tokens=int(row[1]),
                output_tokens=int(row[2]),
                cache_creation_tokens=int(row[3]),
                cache_read_tokens=int(row[4]),
                llm_calls=int(row[5]),
                models=tuple(sorted(m for m in (row[6] or []) if m)),
            )
            for row in rows
            if row[0] is not None
        }

    async def totals_by_users(
        self,
        *,
        agent_name: str,
        agent_version: str,
        user_ids: Sequence[UUID],
    ) -> dict[UUID, TokenTotals]:
        ids = list(dict.fromkeys(user_ids))
        if not ids:
            return {}
        stmt = (
            select(
                TokenUsageRow.user_id,
                func.coalesce(func.sum(TokenUsageRow.input_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.output_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.cache_creation_tokens), 0),
                func.coalesce(func.sum(TokenUsageRow.cache_read_tokens), 0),
                func.count(),
                func.array_agg(func.distinct(TokenUsageRow.model)),
            )
            # Tenant scoping rides on RLS (the sessionmaker sets the tenant
            # GUC) — a foreign tenant's rows are invisible to this session.
            .where(
                TokenUsageRow.agent_name == agent_name,
                TokenUsageRow.agent_version == agent_version,
                TokenUsageRow.user_id.in_(ids),
            )
            .group_by(TokenUsageRow.user_id)
        )
        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()
        return {
            row[0]: TokenTotals(
                input_tokens=int(row[1]),
                output_tokens=int(row[2]),
                cache_creation_tokens=int(row[3]),
                cache_read_tokens=int(row[4]),
                llm_calls=int(row[5]),
                models=tuple(sorted(m for m in (row[6] or []) if m)),
            )
            for row in rows
            if row[0] is not None
        }

    async def anonymize_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> int:
        # Explicit tenant_id predicate (never rely on RLS alone for a mutation —
        # the app connects as a BYPASSRLS superuser). user_id is nulled, the
        # billing row is KEPT.
        stmt = (
            update(TokenUsageRow)
            .where(
                TokenUsageRow.tenant_id == tenant_id,
                TokenUsageRow.user_id == user_id,
            )
            .values(user_id=None)
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


def _row_to_record(row: TokenUsageRow) -> TokenUsageRecord:
    return TokenUsageRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_name=row.agent_name,
        agent_version=row.agent_version,
        model=row.model,
        provider=row.provider,
        user_id=row.user_id,
        trace_id=row.trace_id,
        usage_kind=row.usage_kind,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cache_creation_tokens=row.cache_creation_tokens,
        cache_read_tokens=row.cache_read_tokens,
        observed_at=row.observed_at,
    )
