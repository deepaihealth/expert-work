"""Abstract :class:`QualityDriftAlertStore` — Stream RT-5 (RT-ADR-24)."""

from __future__ import annotations

import abc
from datetime import datetime
from uuid import UUID

from expert_work.protocol import QualityDriftAlertRecord


class QualityDriftAlertStore(abc.ABC):
    """Append-only per-tenant history of raised quality-drift alerts."""

    @abc.abstractmethod
    async def insert(self, record: QualityDriftAlertRecord) -> QualityDriftAlertRecord:
        """Persist one alert; returns it with ``id`` / ``detected_at`` set."""

    @abc.abstractmethod
    async def latest_alert_at(self, *, tenant_id: UUID, agent_name: str) -> datetime | None:
        """Most recent ``detected_at`` for ``(tenant_id, agent_name)``, or ``None``.

        Backs the drift cooldown (RT-ADR-24): the worker skips an agent whose
        last alert is still within the cooldown window.
        """

    @abc.abstractmethod
    async def list_alerts(
        self,
        *,
        tenant_id: UUID,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        """Recent alerts, newest ``detected_at`` first (dashboard, RT-ADR-26)."""

    @abc.abstractmethod
    async def list_alerts_all_tenants(
        self,
        *,
        agent_name: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[QualityDriftAlertRecord]:
        """:meth:`list_alerts` across every tenant (``detected_at`` DESC,
        ``id`` tiebreak) — the W4 ``tenant_id=*`` aggregate. Caller MUST wrap
        the call in ``bypass_rls_session()`` / ``applied_scope(CrossTenant)``.
        """
