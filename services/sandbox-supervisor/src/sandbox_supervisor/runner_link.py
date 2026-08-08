"""Supervisor↔runner transport — the held-pipe link (STREAM-F-DESIGN, option C).

:class:`PipeRunnerLink` wraps one sandbox's live ``docker run -i``
subprocess: the supervisor writes a runner request line to its stdin and
reads one response line from its stdout (the runner protocol, § 4.2).

This module is the **transport seam**. M1 warm pool swaps
:class:`PipeRunnerLink` for a socket-RPC link; the supervisor's ``exec``
method and HTTP layer above it do not change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

#: How long ``close`` waits for the container to exit on stdin EOF.
_CLOSE_GRACE_S = 5.0


class RunnerLinkError(RuntimeError):
    """The runner broke the protocol — closed, timed out, or sent garbage."""


@dataclass(frozen=True)
class ExecResult:
    """One code execution's outcome, as the runner reports it."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


class RunnerLink(Protocol):
    """A live channel to one sandbox's runner — the transport seam."""

    async def wait_ready(self, timeout_s: float) -> None:
        """Block until the runner emits its readiness line."""

    async def exec(
        self, code: str, timeout_s: int, *, envs: dict[str, str] | None = None
    ) -> ExecResult:
        """Run ``code`` in the sandbox; return its captured outcome.

        ``envs`` (sandbox migration wave 2, spec 决策 10) — per-call env
        overrides merged onto the runner subprocess's environment; ``None``
        (default) → no override, pre-feature behaviour.
        """

    async def close(self) -> None:
        """Close the channel — the container exits on its stdin EOF."""


class PipeRunnerLink:
    """:class:`RunnerLink` over a held ``docker run -i`` subprocess.

    An :class:`asyncio.Lock` serialises access so concurrent ``exec``
    calls cannot interleave writes on the shared stdin pipe.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        read_grace_s: float = 10.0,
    ) -> None:
        self._process = process
        self._read_grace_s = read_grace_s
        self._lock = asyncio.Lock()

    @property
    def is_alive(self) -> bool:
        """Whether the runner subprocess is still running."""
        return self._process.returncode is None

    async def wait_ready(self, timeout_s: float) -> None:
        payload = await self._read_line(timeout_s)
        if payload.get("ready") is not True:
            msg = f"runner sent {payload!r} instead of a readiness line"
            raise RunnerLinkError(msg)

    async def exec(
        self, code: str, timeout_s: int, *, envs: dict[str, str] | None = None
    ) -> ExecResult:
        async with self._lock:
            request: dict[str, object] = {"code": code, "timeout_s": timeout_s}
            if envs:
                request["envs"] = envs
            await self._write(request)
            # The supervisor's read deadline is the runner's own timeout
            # plus a grace window — a runner past it is itself hung.
            payload = await self._read_line(timeout_s + self._read_grace_s)
        return ExecResult(
            stdout=_as_str(payload.get("stdout")),
            stderr=_as_str(payload.get("stderr")),
            exit_code=_as_int(payload.get("exit_code")),
            timed_out=payload.get("timed_out") is True,
        )

    async def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.is_closing():
            stdin.close()
        # The container exits on stdin EOF; do not block teardown on it —
        # ``docker rm --force`` is the backstop.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._process.wait(), timeout=_CLOSE_GRACE_S)

    async def _write(self, request: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            msg = "runner stdin is not available"
            raise RunnerLinkError(msg)
        stdin.write((json.dumps(request) + "\n").encode())
        await stdin.drain()

    async def _read_line(self, timeout_s: float) -> dict[str, object]:
        stdout = self._process.stdout
        if stdout is None:
            msg = "runner stdout is not available"
            raise RunnerLinkError(msg)
        try:
            raw = await asyncio.wait_for(stdout.readline(), timeout=timeout_s)
        except TimeoutError as exc:
            msg = "runner did not respond before the deadline"
            raise RunnerLinkError(msg) from exc
        if not raw:
            msg = "runner closed the connection"
            raise RunnerLinkError(msg)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"runner sent a non-JSON line: {exc}"
            raise RunnerLinkError(msg) from exc
        if not isinstance(payload, dict):
            msg = "runner sent a non-object response"
            raise RunnerLinkError(msg)
        return payload


def _as_str(value: object) -> str:
    """Read a runner-reported string field, defaulting to empty."""
    return value if isinstance(value, str) else ""


def _as_int(value: object) -> int:
    """Read a runner-reported int field, defaulting to -1. ``bool`` is an
    ``int`` subclass but is never a valid exit code, so it is rejected."""
    return value if isinstance(value, int) and not isinstance(value, bool) else -1
