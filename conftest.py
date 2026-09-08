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
