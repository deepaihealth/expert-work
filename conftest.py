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

from expert_work.testing import InMemorySecretStore, MockLLM

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


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

    **X-8 余项**:这是全仓最后一个仍从 Docker Hub 拉的镜像,不是漏改 ——
    官方镜像都搬到了 ``public.ecr.aws/docker/library``(见
    ``tools/ci/check_image_registry.py``),但 pgvector 不是官方镜像,
    ECR Public / GHCR / quay 三处都探过,没有第二个公共源。要摘掉它只能
    自建镜像仓(GHCR + 一条定时 ``imagetools create``)。

    Requires Docker daemon available; tests using this fixture should
    be marked ``@pytest.mark.integration``.
    """
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("pgvector/pgvector:pg16")
    with container:
        yield container


@pytest.fixture
def tmp_postgres_dsn(postgres_container: PostgresContainer) -> str:
    """SQLAlchemy-compatible DSN for the session Postgres container."""
    return str(postgres_container.get_connection_url())
