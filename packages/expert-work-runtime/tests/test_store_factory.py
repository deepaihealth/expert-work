"""Tests for the store factory (memory backend + error paths)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from unittest.mock import patch

import psycopg.errors
import pytest
from langgraph.store.memory import InMemoryStore

from expert_work.runtime.store import make_store


@pytest.mark.asyncio
async def test_memory_backend_yields_in_memory_store() -> None:
    async with make_store("memory") as store:
        assert isinstance(store, InMemoryStore)


@pytest.mark.asyncio
async def test_unknown_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unknown store backend"):
        async with make_store("redis"):  # type: ignore[arg-type]
            pass


@pytest.mark.asyncio
async def test_postgres_backend_requires_dsn() -> None:
    with pytest.raises(ValueError, match="requires a non-empty dsn"):
        async with make_store("postgres"):
            pass


class _FakeAsyncPostgresStore:
    """Stand-in for ``AsyncPostgresStore`` whose ``setup()`` can fail N times
    before succeeding, to exercise the concurrent-DDL-race retry path."""

    def __init__(
        self,
        fail_times: int,
        error: type[Exception] = psycopg.errors.UniqueViolation,
    ) -> None:
        self.setup_calls = 0
        self._fail_times = fail_times
        self._error = error

    async def setup(self) -> None:
        self.setup_calls += 1
        if self.setup_calls <= self._fail_times:
            raise self._error("concurrent setup() collision")


@contextlib.contextmanager
def _patched_from_conn_string(fake_store: _FakeAsyncPostgresStore) -> Iterator[None]:
    @contextlib.asynccontextmanager
    async def _fake_from_conn_string(
        conn_string: str, **kwargs: object
    ) -> AsyncIterator[_FakeAsyncPostgresStore]:
        yield fake_store

    with patch(
        "langgraph.store.postgres.aio.AsyncPostgresStore.from_conn_string",
        new=_fake_from_conn_string,
    ):
        yield


@pytest.mark.asyncio
async def test_postgres_backend_retries_transient_setup_race() -> None:
    """Two replicas racing first-run ``CREATE TYPE`` — the loser's ``setup()``
    raises a transient duplicate/unique error and must retry, not propagate."""
    fake_store = _FakeAsyncPostgresStore(fail_times=2)

    with _patched_from_conn_string(fake_store):
        async with make_store("postgres", dsn="postgresql://fake") as store:
            # via ``object`` — the fake is not a BaseStore subclass, and a direct
            # identity check trips mypy's comparison-overlap.
            yielded: object = store
            assert yielded is fake_store

    assert fake_store.setup_calls == 3


@pytest.mark.asyncio
async def test_postgres_backend_setup_non_transient_error_propagates() -> None:
    """A non-race ``setup()`` failure must not be swallowed/retried."""
    fake_store = _FakeAsyncPostgresStore(fail_times=1, error=ValueError)

    with _patched_from_conn_string(fake_store):
        with pytest.raises(ValueError, match="concurrent setup"):
            async with make_store("postgres", dsn="postgresql://fake"):
                pass

    assert fake_store.setup_calls == 1
