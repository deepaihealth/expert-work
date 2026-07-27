"""Shared per-call vs. shared-client resolution — 一期 Task 5 (connection reuse).

Every HTTP client class in this codebase (LLM providers, embedder,
reranker, web-search, sandbox supervisor) used to open a fresh
``httpx.AsyncClient`` — and pay a full TLS handshake — on every single
call (``async with httpx.AsyncClient(...) as client: ...``). Task 5
threads a process-level ``httpx.AsyncClient`` (owned by the control-plane
lifespan) into each of those classes via a new ``http`` field so the
handshake is paid once per process, not once per call.

:func:`client_for` is the ONE place that decides which of the two
branches applies. It must not be duplicated — a copy-pasted branch that
forgets to leave the shared client open is a bug that only surfaces at
runtime, in production, as every subsequent LLM call failing once the
shared client is closed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx


@asynccontextmanager
async def client_for(
    shared: httpx.AsyncClient | None,
    *,
    timeout: float | httpx.Timeout,
    transport: httpx.AsyncBaseTransport | None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield the shared client (never closing it) or a per-call one (closed
    on exit).

    ``shared`` is the injected process-level client (``None`` when the
    caller — production code not yet wired, tests, the eval CLI — left it
    unset). The shared branch must not close: the client outlives every
    call, it belongs to the control-plane lifespan, not to this one
    request. The per-call branch is byte-identical to the pre-Task-5
    behaviour: a fresh client, closed when the ``with`` block exits.

    The shared branch silently drops ``timeout``/``transport`` — the
    injected client already carries its own transport, and per-request
    ``timeout=`` is passed by the caller directly on the actual
    ``.post()``/``.get()``/... call, not here. The assert below turns "a
    test injects a ``MockTransport`` alongside a shared client and the
    transport gets silently ignored" (the client would then hit whatever
    transport ``shared`` was actually built with — real network in the
    worst case) into an immediate failure instead of a quietly-wrong test.
    """
    assert transport is None or shared is None, (  # noqa: S101 — precondition, not test code
        "client_for(): a transport was passed alongside a shared client; "
        "it would be silently ignored (the shared client's own transport "
        "wins) — pass transport via the shared client's own construction"
    )
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        yield client
