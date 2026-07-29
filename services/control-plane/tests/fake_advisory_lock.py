"""Fake Postgres advisory-lock session factory — no real DB required.

Emulates ``pg_try_advisory_xact_lock`` well enough to unit-test callers of
``control_plane._tenant_resource_lock.tenant_resource_lock`` (and the API
routes that wrap it) without a real Postgres: a ``(classid, key)`` lock is
held by whichever session first ``execute()``s it and released on that
session's ``rollback()`` — mirrors the real primitive's "held for the
transaction" semantics closely enough to prove call-order / exclusion in
tests. Genuine cross-connection mutual exclusion is proven separately by
``test_tenant_resource_lock_integration.py`` (real testcontainers Postgres,
independent connections).
"""

from __future__ import annotations

from types import TracebackType
from typing import Any


class _FakeLockResult:
    def __init__(self, value: bool) -> None:
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class FakeAdvisoryLockSession:
    """One fake connection/session — holds whatever locks it acquired
    until ``rollback()`` (or the ``async with`` block exits, which calls
    it)."""

    def __init__(self, registry: dict[tuple[int, str], FakeAdvisoryLockSession]) -> None:
        self._registry = registry
        self._held: set[tuple[int, str]] = set()

    async def execute(self, _stmt: object, params: dict[str, Any] | None = None) -> _FakeLockResult:
        if not params or "cid" not in params or "k" not in params:
            # Anything that isn't the advisory-lock SELECT (e.g. a future
            # ``SET LOCAL ...``) — no-op, result unused by the caller.
            return _FakeLockResult(True)
        lock_key = (int(params["cid"]), str(params["k"]))
        holder = self._registry.get(lock_key)
        if holder is None or holder is self:
            self._registry[lock_key] = self
            self._held.add(lock_key)
            return _FakeLockResult(True)
        return _FakeLockResult(False)

    async def rollback(self) -> None:
        for lock_key in self._held:
            if self._registry.get(lock_key) is self:
                del self._registry[lock_key]
        self._held.clear()

    async def __aenter__(self) -> FakeAdvisoryLockSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.rollback()


class FakeAdvisoryLockSessionFactory:
    """Callable factory — each call returns a new session sharing the same
    lock registry, mirroring one physical connection per
    ``async with session_factory() as session:`` call."""

    def __init__(self) -> None:
        self._registry: dict[tuple[int, str], FakeAdvisoryLockSession] = {}

    def __call__(self) -> FakeAdvisoryLockSession:
        return FakeAdvisoryLockSession(self._registry)
