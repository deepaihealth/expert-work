"""In-memory ``TriggerStore`` for unit tests."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from expert_work.persistence.trigger.base import TriggerRunStore, TriggerStore
from expert_work.protocol import TriggerRecord, TriggerRunRecord, TriggerRunStatus


class InMemoryTriggerStore(TriggerStore):
    """In-memory ``TriggerStore`` — keyed by trigger id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, TriggerRecord] = {}

    async def create(self, record: TriggerRecord) -> TriggerRecord:
        for existing in self._rows.values():
            same_scope = (
                existing.tenant_id == record.tenant_id
                and existing.agent_name == record.agent_name
                and existing.name == record.name
                and existing.user_id == record.user_id
            )
            if same_scope:
                msg = (
                    f"trigger {record.name!r} already exists for agent "
                    f"{record.agent_name!r} (user {record.user_id})"
                )
                raise ValueError(msg)
        self._rows[record.id] = record
        return record

    async def get(self, *, trigger_id: UUID, tenant_id: UUID) -> TriggerRecord | None:
        row = self._rows.get(trigger_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_by_agent(self, *, tenant_id: UUID, agent_name: str) -> list[TriggerRecord]:
        return [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and r.agent_name == agent_name
        ]

    async def list_by_tenant(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> list[TriggerRecord]:
        return [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and (agent_name is None or r.agent_name == agent_name)
            and (agent_version is None or r.agent_version == agent_version)
        ]

    async def list_by_user(
        self, *, tenant_id: UUID, user_id: UUID, agent_name: str | None = None
    ) -> list[TriggerRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id
            and r.user_id == user_id
            and (agent_name is None or r.agent_name == agent_name)
        ]
        return sorted(rows, key=lambda r: r.created_at)

    async def list_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        agent_version: str | None = None,
    ) -> list[TriggerRecord]:
        return [
            r
            for r in self._rows.values()
            if (agent_name is None or r.agent_name == agent_name)
            and (agent_version is None or r.agent_version == agent_version)
        ]

    async def list_enabled_cron(self) -> list[TriggerRecord]:
        return [r for r in self._rows.values() if r.kind == "cron" and r.enabled]

    async def update(self, record: TriggerRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def disable_for_agent(
        self, *, agent_name: str, agent_version: str, tenant_id: UUID
    ) -> int:
        """Deletion hygiene PR4 §B — agent soft-delete cascade.

        Predicates: ``enabled == true AND agent_name == AND agent_version ==
        AND tenant_id ==``; sets ``enabled=false``; returns rows changed.
        Must stay semantically identical to ``SqlTriggerStore.disable_for_agent``.
        """
        victims = [
            tid
            for tid, r in self._rows.items()
            if r.tenant_id == tenant_id
            and r.agent_name == agent_name
            and r.agent_version == agent_version
            and r.enabled
        ]
        for tid in victims:
            self._rows[tid] = self._rows[tid].model_copy(update={"enabled": False})
        return len(victims)

    async def claim_cron_fire(
        self,
        *,
        trigger_id: UUID,
        tenant_id: UUID,
        expected_last_fired_at: datetime | None,
        new_last_fired_at: datetime,
    ) -> bool:
        row = self._rows.get(trigger_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        if row.last_fired_at != expected_last_fired_at:
            # A peer already claimed this slot — loser, no fire.
            return False
        self._rows[trigger_id] = row.model_copy(
            update={"last_fired_at": new_last_fired_at, "updated_at": new_last_fired_at}
        )
        return True

    async def delete(self, *, trigger_id: UUID, tenant_id: UUID) -> bool:
        row = self._rows.get(trigger_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        del self._rows[trigger_id]
        return True

    async def delete_all_for_user(self, *, tenant_id: UUID, user_id: UUID) -> list[UUID]:
        victims = [
            tid
            for tid, r in self._rows.items()
            if r.tenant_id == tenant_id and r.user_id == user_id
        ]
        for tid in victims:
            del self._rows[tid]
        return victims

    async def get_for_webhook(self, *, trigger_id: UUID) -> TriggerRecord | None:
        return self._rows.get(trigger_id)

    async def count_cron_by_tenant(self, *, tenant_id: UUID) -> int:
        return sum(1 for r in self._rows.values() if r.tenant_id == tenant_id and r.kind == "cron")


class InMemoryTriggerRunStore(TriggerRunStore):
    """In-memory ``TriggerRunStore`` — keyed by firing id."""

    def __init__(self) -> None:
        self._rows: dict[UUID, TriggerRunRecord] = {}

    async def create(self, record: TriggerRunRecord) -> TriggerRunRecord:
        if record.id in self._rows:
            msg = f"trigger_run row already exists for id {record.id}"
            raise ValueError(msg)
        self._rows[record.id] = record
        return record

    async def get(self, *, trigger_run_id: UUID, tenant_id: UUID) -> TriggerRunRecord | None:
        row = self._rows.get(trigger_run_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def update(self, record: TriggerRunRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        self._rows[record.id] = record
        return True

    async def claim_retry(self, *, trigger_run_id: UUID, tenant_id: UUID) -> bool:
        row = self._rows.get(trigger_run_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        if row.status is not TriggerRunStatus.RETRYING:
            # A peer already claimed this retry — loser, no re-fire.
            return False
        self._rows[trigger_run_id] = row.model_copy(
            update={"status": TriggerRunStatus.FIRED, "next_retry_at": None}
        )
        return True

    async def claim_reconcile(self, record: TriggerRunRecord) -> bool:
        existing = self._rows.get(record.id)
        if existing is None or existing.tenant_id != record.tenant_id:
            return False
        if existing.status is not TriggerRunStatus.FIRED:
            # A peer already finalized this firing — loser, no-op (no overwrite).
            return False
        self._rows[record.id] = record
        return True

    async def list_by_trigger(self, *, trigger_id: UUID, tenant_id: UUID) -> list[TriggerRunRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.trigger_id == trigger_id and r.tenant_id == tenant_id
        ]
        rows.sort(key=lambda r: r.triggered_at, reverse=True)
        return rows

    async def delete_for_triggers(self, *, trigger_ids: Sequence[UUID], tenant_id: UUID) -> int:
        wanted = set(trigger_ids)
        if not wanted:
            return 0
        victims = [
            rid
            for rid, r in self._rows.items()
            if r.trigger_id in wanted and r.tenant_id == tenant_id
        ]
        for rid in victims:
            del self._rows[rid]
        return len(victims)

    async def list_fired(self, *, limit: int = 1000) -> list[TriggerRunRecord]:
        rows = [r for r in self._rows.values() if r.status is TriggerRunStatus.FIRED]
        rows.sort(key=lambda r: r.triggered_at)
        return rows[:limit]

    async def list_due_retries(
        self, *, before: datetime, limit: int = 1000
    ) -> list[TriggerRunRecord]:
        rows = [
            r
            for r in self._rows.values()
            if r.status is TriggerRunStatus.RETRYING
            and r.next_retry_at is not None
            and r.next_retry_at <= before
        ]
        rows.sort(key=lambda r: r.next_retry_at or before)
        return rows[:limit]
