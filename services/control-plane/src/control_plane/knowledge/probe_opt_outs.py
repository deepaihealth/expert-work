"""B-267 —— which stored manifests turn the per-turn knowledge probe off.

``knowledge.probe`` is stored only when it is ``false`` (the default is left
out, ``KnowledgeSpec._omit_default_probe``). An image from before B-267 reads
such a manifest leniently — stored manifests go through ``load_stored_spec``
(B-61), which drops keys it does not know — so a rollback does not stop those
agents from loading. What a rollback can do is lose the opt-out: an agent saved
while the older image runs is written back without it, and after upgrading
again it is probed. This lists the rows to re-check — ids only, read-only::

    python -m control_plane.knowledge.probe_opt_outs

Run it before rolling back (keep the output) and again after upgrading: a row
listed before and not after lost its opt-out.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from expert_work.persistence.models import AgentSpecRow
from expert_work.persistence.models.platform_agent_template import PlatformAgentTemplateRow


@dataclass(frozen=True)
class OptOut:
    """A stored manifest with ``knowledge.probe: false``."""

    table: str
    row_id: UUID
    tenant_id: UUID | None
    draft: bool

    def line(self) -> str:
        return (
            f"table={self.table} id={self.row_id} tenant={self.tenant_id or '-'} "
            f"draft={str(self.draft).lower()}"
        )


def _probe_off(column: InstrumentedAttribute[Any]) -> ColumnElement[bool]:
    probe: ColumnElement[bool] = column["spec"]["knowledge"]["probe"].astext == "false"
    return probe


async def list_opt_outs(session_factory: async_sessionmaker[AsyncSession]) -> list[OptOut]:
    """Every agent (live or draft manifest) and platform template that turns
    the probe off. Caller bypasses row security (all tenants)."""
    async with session_factory() as session:
        agents = (
            await session.execute(
                select(
                    AgentSpecRow.id,
                    AgentSpecRow.tenant_id,
                    _probe_off(AgentSpecRow.spec_json),
                    _probe_off(AgentSpecRow.draft_spec_json),
                ).where(
                    or_(
                        _probe_off(AgentSpecRow.spec_json),
                        _probe_off(AgentSpecRow.draft_spec_json),
                    )
                )
            )
        ).all()
        templates = (
            await session.execute(
                select(PlatformAgentTemplateRow.id, PlatformAgentTemplateRow.tenant_id).where(
                    _probe_off(PlatformAgentTemplateRow.spec_json)
                )
            )
        ).all()
    found: list[OptOut] = []
    for row_id, tenant_id, live, draft in agents:
        if live:
            found.append(OptOut("agent_spec", row_id, tenant_id, draft=False))
        if draft:
            found.append(OptOut("agent_spec", row_id, tenant_id, draft=True))
    found.extend(
        OptOut("platform_agent_template", row_id, tenant_id, draft=False)
        for row_id, tenant_id in templates
    )
    return found


async def _amain(args: argparse.Namespace) -> int:  # pragma: no cover - needs a database
    from control_plane.settings import Settings
    from control_plane.tenant_scope import bypass_rls_session
    from expert_work.persistence import (
        DatabaseConfig,
        build_rls_sessionmaker,
        create_async_engine_from_config,
        create_async_session_factory,
    )

    settings = Settings()
    engine = create_async_engine_from_config(
        DatabaseConfig(dsn=args.dsn or settings.db_dsn, pgbouncer_mode=settings.db_pgbouncer_mode)
    )
    try:
        session_factory = build_rls_sessionmaker(create_async_session_factory(engine))
        async with bypass_rls_session():
            found = await list_opt_outs(session_factory)
    finally:
        await engine.dispose()
    for entry in found:
        print(entry.line())
    print(f"opt_outs={len(found)}")
    return 0


def main() -> None:  # pragma: no cover - CLI entry
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=None, help="override the DB DSN")
    raise SystemExit(asyncio.run(_amain(parser.parse_args())))


if __name__ == "__main__":
    main()
