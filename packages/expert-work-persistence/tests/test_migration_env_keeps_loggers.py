"""``migrations/env.py`` must not switch off loggers that already exist.

Bug (#1440 ``Test (integration)`` red, 2026-09-08): ``env.py`` called
``logging.config.fileConfig(ini)`` with the default
``disable_existing_loggers=True``. The CI integration job runs the whole repo
in ONE process with ``--import-mode=importlib``, so every test module — and
every logger those modules create at import — exists before the first
migration test runs. That first ``alembic upgrade`` then set
``disabled=True`` on all of them: later ``logger.warning`` calls emitted
nothing and ``caplog`` came back empty, while the code under test had run
exactly as intended. A file run on its own never sees it, because no
migration precedes it. Reproduce on ``main`` with the CI command shape::

    uv run pytest -v -m integration --timeout=1800 --timeout-method=thread \\
        packages/expert-work-persistence/tests/test_initial_schema.py \\
        services/orchestrator/tests/test_rate_limit_redis_integration.py

This test creates a logger at import time (collection), runs one upgrade in
a **fixture** (setup phase — ``fileConfig`` also replaces the root logger's
handlers, so an upgrade inside the test body would discard caplog's own
capture handler regardless of the fix), then checks the logger still works
and caplog still sees it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

#: Created at import — i.e. at collection, before any migration test runs —
#: which is exactly the situation of every logger in the repo under the
#: single-process integration job.
_PROBE = logging.getLogger("expert_work.tests.alembic_env_probe")


def _sync_dsn(container: PostgresContainer) -> str:
    url: str = str(container.get_connection_url())
    return url.replace("+psycopg2", "+psycopg").replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
def upgraded(postgres_container: PostgresContainer) -> None:
    """Run ``alembic upgrade head`` (executes ``env.py`` → ``fileConfig``)."""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_dsn(postgres_container))
    command.upgrade(cfg, "head")


def test_alembic_upgrade_keeps_pre_existing_loggers_enabled(
    upgraded: None, caplog: pytest.LogCaptureFixture
) -> None:
    assert _PROBE.disabled is False, (
        "env.py's fileConfig switched off a logger that existed before the upgrade"
    )

    caplog.set_level(logging.WARNING, logger=_PROBE.name)
    _PROBE.warning("alembic_env_probe.after_upgrade")

    assert [r.getMessage() for r in caplog.records] == ["alembic_env_probe.after_upgrade"]
