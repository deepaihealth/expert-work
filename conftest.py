"""Root pytest configuration — fixtures usable by every test in the repo.

Helper classes live in :mod:`expert_work.testing` for direct importability;
this module only registers the pytest fixtures.

Per-Stream additions:
- Stream A.1 introduced the ``postgres_container`` fixture consumer
  (``packages/expert-work-persistence/tests/test_initial_schema.py``)
- Stream E.1 will introduce VCR-recorded Anthropic cassettes
- ADR-0007 SecretStore Protocol will be added in Stream A.x;
  ``mock_secret_store`` will then implement it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from langgraph.checkpoint.serde.event_hooks import SerdeEvent, register_serde_event_listener

from expert_work.runtime.checkpointer.serde import CHECKPOINT_MSGPACK_ALLOWLIST
from expert_work.testing import InMemorySecretStore, MockLLM

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


#: Module prefixes of our own code — a checkpoint type from anywhere else
#: (langgraph's safe set, a test-local model) is not ours to register.
_OWN_MODULE_PREFIXES = ("expert_work.", "orchestrator.", "control_plane.")


@pytest.fixture(autouse=True)
def _checkpoint_types_are_registered() -> Iterator[None]:
    """B-161 — any test that loads one of OUR types from a checkpoint must find
    it in ``CHECKPOINT_MSGPACK_ALLOWLIST``. Strict msgpack (the announced
    langgraph default) hands an unlisted type back as a bare ``dict``.

    The schema walk in ``test_checkpoint_serde_allowlist.py`` only sees typed
    ``AgentState`` fields; this catches the rest — a model parked in a
    ``dict[str, Any]`` channel or a message's ``artifact`` — whenever any test
    round-trips it. Fires for savers built with or without the allowlist.
    """
    registered = set(CHECKPOINT_MSGPACK_ALLOWLIST)
    unregistered: set[str] = set()

    def _listener(event: SerdeEvent) -> None:
        key = (event["module"], event["name"])
        if key[0].startswith(_OWN_MODULE_PREFIXES) and key not in registered:
            unregistered.add(".".join(key))

    unregister = register_serde_event_listener(_listener)
    yield
    unregister()
    assert not unregistered, (
        f"checkpoint loaded unregistered types {sorted(unregistered)} — add them to "
        "CHECKPOINT_MSGPACK_ALLOWLIST (expert_work/runtime/checkpointer/serde.py)"
    )


@pytest.fixture
def mock_llm() -> MockLLM:
    """Fresh MockLLM per test."""
    return MockLLM()


@pytest.fixture
def mock_secret_store() -> InMemorySecretStore:
    """Fresh InMemorySecretStore per test."""
    return InMemorySecretStore()


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Session-scoped Postgres 16 container via testcontainers.

    Heavy — adds ~10s session startup. First consumer is Stream A.1
    (``packages/expert-work-persistence/tests/test_initial_schema.py``).

    Uses the ``pgvector/pgvector`` image (Postgres 16 + the ``vector``
    extension, Stream J.3) — a superset of stock Postgres, so every
    pre-J.3 migration / test is unaffected.

    **X-8 收官**:pgvector 不是官方镜像,ECR Public / GHCR / quay 三处都没有
    第二个公共源,所以从 ``ghcr.io/deepaihealth/mirror/`` 拉 —— 那是
    ``.github/workflows/mirror-images.yml`` 每周从 Docker Hub 原样复制过来的
    (digest 相同)。裸名 ``pgvector/pgvector:<tag>`` 会被
    ``tools/ci/check_image_registry.py`` 卫兵拦下。

    Requires Docker daemon available; tests using this fixture should
    be marked ``@pytest.mark.integration``.
    """
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("ghcr.io/deepaihealth/mirror/pgvector:pg16")
    with container:
        yield container


@pytest.fixture
def tmp_postgres_dsn(postgres_container: PostgresContainer) -> str:
    """SQLAlchemy-compatible DSN for the session Postgres container."""
    return str(postgres_container.get_connection_url())
