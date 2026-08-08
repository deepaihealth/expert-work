"""``exec_python`` tool — Stream F.4 / F.7.

Runs LLM-generated Python in a gVisor sandbox via the Sandbox Supervisor
(F.1): ``acquire`` a sandbox, ``exec`` the code, ``release`` it. The
supervisor mediates the held-pipe runner protocol (F.4a); this tool
speaks only the supervisor's HTTP API behind a small
:class:`SandboxRuntime` Protocol — tests inject
:class:`RecordingSandboxRuntime`.

Output is truncated to :data:`DEFAULT_OUTPUT_CHAR_CAP` for the LLM
(Mini-ADR F-9 / E-10) — the runner already capped it at ~1 MiB for
transport. The gVisor sandbox (read-only rootfs / cap-drop / no-new-privileges
/ pids-mem-cpu caps / proxy-only egress) is the security boundary; submitted
code is recorded into the tool audit (docs/design/sandbox-audit-evaluation.md).

Cancellation (F.7): E.15 races the whole tool dispatch in
``run_cancellable``, so a cancelled run surfaces here as
:class:`asyncio.CancelledError` on the ``exec`` ``await``. The tool then
``destroy``s the sandbox with ``reason="cancelled"`` — a forced SIGKILL
teardown — instead of a routine ``release`` (Mini-ADR F-8).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from expert_work.common.observability import inject_context
from expert_work.persistence import SANDBOX_AGENTS_ROOT
from orchestrator.llm.providers._http import client_for
from orchestrator.tools.registry import ToolBlockedError, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

#: Per-stream cap on stdout / stderr handed back to the LLM (Mini-ADR F-9).
DEFAULT_OUTPUT_CHAR_CAP = 20_000
_TRUNCATION_MARKER = "...[truncated]"
_DEFAULT_TIMEOUT_S = 60.0
#: Upper bound a tool may set for a single sandbox exec (mirrors
#: :func:`coerce_timeout`'s cap). The exec HTTP read timeout is derived from
#: the per-call exec timeout so the orchestrator always outlasts the sandbox-
#: side enforcement; when a caller passes no ``timeout_s`` we assume this cap so
#: a long-but-legit command (e.g. ``pip install``) is never cut short with a
#: misleading "supervisor unreachable" while the sandbox is still running.
_MAX_EXEC_TIMEOUT_S = 300
#: Slack added on top of the exec deadline for acquire / runner / network
#: overhead before the HTTP read timeout fires.
_EXEC_HTTP_BUFFER_S = 15.0
#: Thread label used for ``acquire`` when the run has no id (ad-hoc call).
_FALLBACK_THREAD_ID = "exec-python"
#: ``destroy`` reason for a cancelled run — drives the supervisor's
#: forced-teardown audit (Mini-ADR F-8).
_CANCELLED_REASON = "cancelled"


@dataclass(frozen=True)
class SandboxOutcome:
    """One code execution's outcome, as the supervisor reports it."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool


@dataclass(frozen=True)
class EgressContext:
    """Per-agent sandbox egress binding (sandbox-egress §3.3).

    Carries the manifest's ``sandbox.network.egress`` policy plus the agent
    identity the supervisor needs to mint the per-sandbox egress token. Bound
    once per build and injected into every ``acquire`` by
    :class:`_EgressBindingClient` — so no per-tool threading is needed.
    """

    policy: str  # manifest egress: "none" | "direct" | "proxy"
    agent_name: str
    agent_version: str
    #: Optional per-agent host allowlist (sandbox-egress Phase 2). Empty → any
    #: public host (audited); non-empty → only these hosts pass at the proxy.
    allowlist: tuple[str, ...] = ()
    #: Optional per-agent host denylist. Blocks these hosts even under the
    #: default allow-all (takes precedence over the allowlist).
    denylist: tuple[str, ...] = ()


def agent_key_envs(agent_key: str) -> dict[str, str]:
    """Per-agent env overrides for an ``exec`` call (spec 决策 10).

    Currently just ``PYTHONUSERBASE`` isolation: two agents sharing one warm
    sandbox otherwise share ``$HOME/.local``, so a ``pip install --user`` from
    one can clobber or race the other's packages (spec § 五之二 concurrency
    review). Shared by both sandbox backends (:class:`HTTPSupervisorRuntime`
    and ``AgentSandboxClient``) so the injected env is byte-identical —
    contract-tested in ``test_sandbox_runtime_contract.py``. Empty
    ``agent_key`` (a caller that never bound one) → no override.
    """
    if not agent_key:
        return {}
    return {"PYTHONUSERBASE": f"{SANDBOX_AGENTS_ROOT}/{agent_key}"}


def _traced_headers() -> dict[str, str]:
    """Outbound headers carrying the active W3C trace context (A.8).

    ``inject_context`` writes ``traceparent`` from the current OTel span so
    the sandbox-supervisor continues the same trace. The supervisor is the
    one trusted internal HTTP hop (see 20-observability § 5.8); external
    egress (``http``/``mcp``/LLM tools) deliberately does *not* propagate.
    No active span (OTel uninitialized, e.g. tests) → nothing is written.
    """
    headers: dict[str, str] = {}
    inject_context(headers)
    return headers


class SandboxSupervisorError(RuntimeError):
    """A Sandbox Supervisor HTTP call failed.

    Raised by :class:`HTTPSupervisorRuntime`; :class:`ExecPythonTool`
    lets it propagate so the ReAct ``tools`` node wraps it into a
    ``ToolMessage(status="error")`` (Mini-ADR E-12).
    """


class WorkspacePermissionError(SandboxSupervisorError):
    """工作区文件存在,但本进程的 uid/gid 读写不动它。

    与"不存在"必须分开:前者是**服务端配置问题**(共享 gid 没配上、存量文件
    没迁移、目录 mode 不对),后者才是用户输入问题。合并成一个之后,用户看到
    的是"文件不存在"而文件明明列在浏览列表里 —— W2-BUG-1 的诊断成本几乎全在
    这上面。

    仍是 :class:`SandboxSupervisorError` 的子类,既有的宽 ``except`` 一律不受
    影响;只有想区分的调用方(工作区下载端点)才需要单独接它。
    """


@runtime_checkable
class SandboxRuntime(Protocol):
    """The sandbox runtime operations the tool needs."""

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        """Launch a sandbox for the tenant; return its id.

        ``user_id`` set → the sandbox mounts that user's persistent
        workspace volume (Stream J.15); ``None`` → an ephemeral tmpfs.
        ``seed_files`` (skill-runtime §5.1) are ``(relpath, bytes)`` pairs the
        supervisor materializes under ``SANDBOX_SKILLS_ROOT`` (sandbox-local,
        not ``/workspace`` — sandbox migration wave 2 spec § 四) before first
        exec — the agent's activated skill files, already namespaced under
        ``<agent_key>/`` by the caller (``build_skill_seed_files``).
        ``egress`` (sandbox-egress §3.3) carries the agent's egress policy +
        identity; normally injected by :class:`_EgressBindingClient`, so
        callers (``run_in_sandbox``) leave it ``None``.
        """

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        """Run ``code`` in the sandbox; return its captured outcome.

        ``agent_key`` (spec 决策 10) injects
        ``PYTHONUSERBASE=SANDBOX_AGENTS_ROOT/<agent_key>`` so a ``pip install
        --user`` stays isolated per agent even when two agents share one warm
        sandbox; see :func:`agent_key_envs`. Normally injected by
        :class:`_AgentKeyBindingClient`, so callers (``run_in_sandbox``) leave
        it unset (``""`` → no override).
        """

    async def release(self, *, sandbox_id: UUID) -> None:
        """Routine sandbox teardown (graceful)."""

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        """Forced sandbox teardown (SIGKILL); ``reason`` is audited."""

    async def reap(self, *, force: bool) -> int:
        """Run the idle-session sweep now; return how many were reaped.

        ``force=True`` reaps every active session regardless of idle age
        (deterministic teardown for ops + the M0→M1 Gate E2E); ``force=False``
        runs the normal idle-TTL sweep. Persistent workspace volumes are
        preserved — only sessions are destroyed (Stream P, Mini-ADR P-14)."""


@dataclass
class HTTPSupervisorRuntime:
    """Production :class:`SandboxRuntime` — calls the supervisor's HTTP API.

    A non-2xx response raises :class:`SandboxSupervisorError`.
    """

    base_url: str
    timeout_s: float = _DEFAULT_TIMEOUT_S
    #: Test seam — inject ``httpx.MockTransport`` / ``ASGITransport`` to
    #: exercise the wire layer (e.g. the A.8 traceparent round-trip).
    #: Production leaves it ``None`` (real network transport).
    transport: httpx.AsyncBaseTransport | None = None
    #: 一期 Task 5 — process-level shared client. ``None`` falls back to the
    #: original per-call ``httpx.AsyncClient(...)`` behaviour (tests / eval
    #: CLI / not-yet-wired production paths). When injected it must NOT be
    #: closed here: it is owned by the control-plane lifespan and outlives
    #: every individual call.
    http: httpx.AsyncClient | None = None

    @asynccontextmanager
    async def _make_client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with client_for(
            self.http, timeout=self.timeout_s, transport=self.transport
        ) as client:
            yield client

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        payload: dict[str, Any] = {"tenant_id": str(tenant_id), "thread_id": thread_id}
        if user_id is not None:
            payload["user_id"] = str(user_id)
        if seed_files:
            payload["seed_files"] = [
                {"path": path, "content_b64": base64.b64encode(data).decode("ascii")}
                for path, data in seed_files
            ]
        if egress is not None:
            payload["egress"] = egress.policy
            payload["agent_name"] = egress.agent_name
            payload["agent_version"] = egress.agent_version
            if egress.allowlist:
                payload["egress_allowlist"] = list(egress.allowlist)
            if egress.denylist:
                payload["egress_denylist"] = list(egress.denylist)
        body = await self._post("/v1/sandboxes:acquire", json=payload)
        return UUID(str(body["sandbox_id"]))

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        payload: dict[str, Any] = {"code": code}
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
        # spec 决策 10 — PYTHONUSERBASE per agent. Same env dict the cloud
        # backend builds for commands.run(envs=...) (agent_key_envs is the
        # single source both backends call), sent over the supervisor's own
        # exec envs channel (ExecRequest.envs, sandbox_supervisor/schemas.py).
        envs = agent_key_envs(agent_key)
        if envs:
            payload["envs"] = envs
        # The sandbox enforces the exec wall-clock (it SIGKILLs + returns
        # ``timed_out``); the HTTP read timeout must OUTLAST that enforcement so
        # the orchestrator receives the real outcome instead of giving up early
        # with a misleading "supervisor unreachable" (and leaking a still-running
        # exec). With the fixed 60s client a ``pip install`` granted up to 300s
        # was cut at 60s. Derive the read timeout from the per-call deadline;
        # assume the cap when the caller left it unset.
        read_timeout = (
            float(timeout_s) if timeout_s is not None else float(_MAX_EXEC_TIMEOUT_S)
        ) + _EXEC_HTTP_BUFFER_S
        url = f"{self.base_url}/v1/sandboxes/{sandbox_id}:exec"
        async with client_for(self.http, timeout=read_timeout, transport=self.transport) as client:
            try:
                response = await client.post(
                    url,
                    json=payload,
                    headers=_traced_headers(),
                    timeout=read_timeout,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({url}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = f"sandbox supervisor exec failed: {response.status_code} {response.text}"
            raise SandboxSupervisorError(msg)
        body = response.json()
        if not isinstance(body, Mapping):
            raise SandboxSupervisorError("sandbox supervisor exec returned a non-object body")
        return SandboxOutcome(
            stdout=str(body.get("stdout", "")),
            stderr=str(body.get("stderr", "")),
            exit_code=int(body.get("exit_code", -1)),
            timed_out=bool(body.get("timed_out", False)),
        )

    async def release(self, *, sandbox_id: UUID) -> None:
        await self._post(f"/v1/sandboxes/{sandbox_id}:release", json=None, expect_body=False)

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        await self._post(
            f"/v1/sandboxes/{sandbox_id}:destroy",
            json={"reason": reason},
            expect_body=False,
        )

    async def reap(self, *, force: bool) -> int:
        body = await self._post("/v1/sandboxes:reap", json={"force": force})
        return int(body.get("reaped_count", 0))

    async def _post(
        self,
        path: str,
        *,
        json: Mapping[str, Any] | None,
        expect_body: bool = True,
    ) -> Mapping[str, Any]:
        async with self._make_client() as client:
            try:
                response = await client.post(
                    f"{self.base_url}{path}",
                    json=json,
                    headers=_traced_headers(),
                    timeout=self.timeout_s,  # per-request — governs even when sharing a client
                )
            except httpx.HTTPError as exc:
                msg = f"sandbox supervisor unreachable ({path}): {exc}"
                raise SandboxSupervisorError(msg) from exc
        if response.is_error:
            msg = f"sandbox supervisor {path} failed: {response.status_code} {response.text}"
            raise SandboxSupervisorError(msg)
        if not expect_body:
            return {}
        data = response.json()
        if not isinstance(data, Mapping):
            msg = f"sandbox supervisor {path} returned a non-object body"
            raise SandboxSupervisorError(msg)
        return data


@dataclass
class RecordingSandboxRuntime:
    """In-memory :class:`SandboxRuntime` for dev / tests.

    Records the acquire / exec / release / destroy calls and returns the
    pre-set :attr:`outcome`. Set ``exec_error`` to drive the error path
    (an :class:`asyncio.CancelledError` drives the cancellation path),
    and ``destroy_error`` to drive a failed teardown.
    """

    outcome: SandboxOutcome = field(
        default_factory=lambda: SandboxOutcome(stdout="", stderr="", exit_code=0, timed_out=False)
    )
    exec_error: BaseException | None = None
    destroy_error: Exception | None = None
    acquired: list[tuple[UUID, str, UUID | None, tuple[tuple[str, bytes], ...]]] = field(
        default_factory=list
    )
    #: sandbox-egress §3.3 — the ``EgressContext`` passed to each acquire (kept
    #: out of the ``acquired`` tuple so existing tests stay unchanged).
    egress_calls: list[EgressContext | None] = field(default_factory=list)
    execs: list[tuple[UUID, str]] = field(default_factory=list)
    #: spec 决策 10 — the ``agent_key`` passed to each exec, kept out of
    #: ``execs`` for the same reason ``egress_calls`` is kept out of
    #: ``acquired``: existing tests asserting that tuple's shape stay
    #: unchanged.
    exec_agent_keys: list[str] = field(default_factory=list)
    released: list[UUID] = field(default_factory=list)
    destroyed: list[tuple[UUID, str]] = field(default_factory=list)
    reaped: list[bool] = field(default_factory=list)
    reap_count: int = 0
    _next_id: int = 0

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        self.acquired.append((tenant_id, thread_id, user_id, seed_files))
        self.egress_calls.append(egress)
        self._next_id += 1
        return UUID(int=self._next_id)

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        del timeout_s
        self.execs.append((sandbox_id, code))
        self.exec_agent_keys.append(agent_key)
        if self.exec_error is not None:
            raise self.exec_error
        return self.outcome

    async def release(self, *, sandbox_id: UUID) -> None:
        self.released.append(sandbox_id)

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append((sandbox_id, reason))

    async def reap(self, *, force: bool) -> int:
        self.reaped.append(force)
        return self.reap_count


@dataclass
class _EgressBindingClient:
    """Wraps a :class:`SandboxRuntime`, injecting a fixed :class:`EgressContext`
    into every ``acquire`` (sandbox-egress §3.3).

    Bound once per agent build (``agent_factory``) around the shared supervisor
    client, so all sandbox tools get the agent's egress policy + identity on the
    wire without threading it through each tool. Every other call delegates
    unchanged.
    """

    inner: SandboxRuntime
    egress: EgressContext

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        # The bound context wins — callers don't supply egress themselves.
        return await self.inner.acquire(
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=user_id,
            seed_files=seed_files,
            egress=self.egress,
        )

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        # This wrapper doesn't own agent_key — it's a pure passthrough here so
        # a chain like _AgentKeyBindingClient(inner=_EgressBindingClient(...))
        # still reaches the real backend (spec 决策 10; see bind_agent_key).
        return await self.inner.exec(
            sandbox_id=sandbox_id, code=code, timeout_s=timeout_s, agent_key=agent_key
        )

    async def release(self, *, sandbox_id: UUID) -> None:
        await self.inner.release(sandbox_id=sandbox_id)

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        await self.inner.destroy(sandbox_id=sandbox_id, reason=reason)

    async def reap(self, *, force: bool) -> int:
        return await self.inner.reap(force=force)


def bind_egress(client: SandboxRuntime, egress: EgressContext | None) -> SandboxRuntime:
    """Wrap ``client`` so every acquire carries ``egress`` (no-op if ``None``)."""
    if egress is None:
        return client
    return _EgressBindingClient(inner=client, egress=egress)


@dataclass
class _AgentKeyBindingClient:
    """Wraps a :class:`SandboxRuntime`, injecting a fixed ``agent_key`` into
    every ``exec`` (spec 决策 10 — ``PYTHONUSERBASE`` per-agent isolation).

    Bound once per agent build (``agent_factory``) around the shared sandbox
    client — mirrors :class:`_EgressBindingClient`, but binds into ``exec``
    rather than ``acquire``: unlike the egress token (minted once at
    container-create time, no re-mint on reconnect — see
    ``AgentSandboxClient`` module docstring "Important-3"), ``PYTHONUSERBASE``
    is a plain per-call env override both backends' exec channels support, so
    it can — and must — be re-asserted on every call rather than baked in at
    acquire time (two agents can share one *already-acquired* warm sandbox).
    Every other call delegates unchanged.
    """

    inner: SandboxRuntime
    agent_key: str

    async def acquire(
        self,
        *,
        tenant_id: UUID,
        thread_id: str,
        user_id: UUID | None = None,
        seed_files: tuple[tuple[str, bytes], ...] = (),
        egress: EgressContext | None = None,
    ) -> UUID:
        return await self.inner.acquire(
            tenant_id=tenant_id,
            thread_id=thread_id,
            user_id=user_id,
            seed_files=seed_files,
            egress=egress,
        )

    async def exec(
        self, *, sandbox_id: UUID, code: str, timeout_s: int | None, agent_key: str = ""
    ) -> SandboxOutcome:
        # The bound key wins — callers (run_in_sandbox) don't supply their own.
        return await self.inner.exec(
            sandbox_id=sandbox_id, code=code, timeout_s=timeout_s, agent_key=self.agent_key
        )

    async def release(self, *, sandbox_id: UUID) -> None:
        await self.inner.release(sandbox_id=sandbox_id)

    async def destroy(self, *, sandbox_id: UUID, reason: str) -> None:
        await self.inner.destroy(sandbox_id=sandbox_id, reason=reason)

    async def reap(self, *, force: bool) -> int:
        return await self.inner.reap(force=force)


def bind_agent_key(client: SandboxRuntime, agent_key: str) -> SandboxRuntime:
    """Wrap ``client`` so every exec carries ``agent_key`` (no-op if empty)."""
    if not agent_key:
        return client
    return _AgentKeyBindingClient(inner=client, agent_key=agent_key)


async def run_in_sandbox(
    client: SandboxRuntime,
    *,
    code: str,
    timeout_s: int | None,
    ctx: ToolContext,
    tool_label: str,
    fallback_thread_id: str,
    seed_files: tuple[tuple[str, bytes], ...] = (),
) -> SandboxOutcome:
    """Acquire a sandbox, run ``code``, and tear it down — shared by the
    ``exec_python`` (F.4) and ``bash`` (TE-5) tools.

    Both tools execute arbitrary code in the same gVisor sandbox (bash
    rides the exec channel as a ``subprocess`` wrapper), so the
    acquire / exec / release / cancel-then-destroy dance lives here once.
    Tenant-scoped (the sandbox quota is per-tenant); a cancellation
    mid-exec SIGKILLs the sandbox rather than releasing it gracefully
    (Mini-ADR F-8 — a graceful release would burn the gate-#8 ≤1s budget).

    Workspace durability is **automatic**: any user-scoped run (``ctx.user_id``
    set) acquires against that user's persistent workspace volume, so files
    survive idle-reclaim and restore on the next acquire — no manifest opt-in.
    A run with no ``user_id`` falls back to an ephemeral tmpfs.
    """
    if ctx.tenant_id is None:
        msg = f"{tool_label} requires a tenant binding (ctx.tenant_id)"
        raise ToolBlockedError(msg)
    thread_id = str(ctx.run_id) if ctx.run_id is not None else fallback_thread_id
    # Durability is automatic for user-scoped runs: pass through the run's
    # user so the supervisor mounts that user's persistent workspace volume;
    # no ``user_id`` (e.g. a system run) → an ephemeral tmpfs.
    sandbox_id = await client.acquire(
        tenant_id=ctx.tenant_id,
        thread_id=thread_id,
        user_id=ctx.user_id,
        seed_files=seed_files,
    )
    cancelled = False
    try:
        return await client.exec(sandbox_id=sandbox_id, code=code, timeout_s=timeout_s)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        if cancelled:
            await _destroy_quietly(
                client, sandbox_id, reason=_CANCELLED_REASON, tool_label=tool_label
            )
        else:
            await _release_quietly(client, sandbox_id, tool_label=tool_label)


def format_sandbox_outcome(outcome: SandboxOutcome, output_char_cap: int) -> ToolResult:
    """Render a :class:`SandboxOutcome` into the ``ToolResult`` the LLM sees.

    Head-truncates stdout / stderr to ``output_char_cap`` (Mini-ADR F-9)
    and surfaces ``exit_code`` / ``timed_out`` — plus the truncated ``stdout`` / ``stderr``
    themselves — in the structured ``meta`` (the debug console's data path; the rendered
    text is datamarked on the wire). Shared by ``exec_python`` and ``bash``.

    When either stream was cut, ``full_content`` carries the complete
    rendering so the tools node can externalize it to the workspace
    (Stream CM-5 recoverable compression).
    """
    stdout, cut_out = _truncate(outcome.stdout, output_char_cap)
    stderr, cut_err = _truncate(outcome.stderr, output_char_cap)

    def _render(out: str, err: str) -> str:
        parts: list[str] = []
        if out:
            parts.append(f"stdout:\n{out}")
        if err:
            parts.append(f"stderr:\n{err}")
        if not parts:
            parts.append("(no output)")
        if outcome.timed_out:
            # Actionable so the model self-corrects instead of stalling — a
            # missing dependency install or a slow command just needs more time.
            parts.append(
                "[execution timed out — if the command legitimately needs longer "
                "(e.g. installing a package), re-run it with a larger timeout_s "
                "(max 300)]"
            )
        parts.append(f"exit_code: {outcome.exit_code}")
        return "\n\n".join(parts)

    truncated = cut_out or cut_err
    return ToolResult(
        content=_render(stdout, stderr),
        meta={
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "truncated": truncated,
            # PR-D — the debug console renders stdout/stderr from here
            # (→ ToolMessage.artifact): the ``content`` string is spotlight-
            # datamarked on the wire, so its newlines don't survive. These are
            # the same head-truncated streams ``content`` renders, pre-mark.
            "stdout": stdout,
            "stderr": stderr,
        },
        full_content=_render(outcome.stdout, outcome.stderr) if truncated else None,
    )


async def _release_quietly(client: SandboxRuntime, sandbox_id: UUID, *, tool_label: str) -> None:
    # A release failure must never mask the exec result / error.
    try:
        await client.release(sandbox_id=sandbox_id)
    except Exception:
        logger.exception("%s.release_failed sandbox=%s", tool_label, sandbox_id)


async def _destroy_quietly(
    client: SandboxRuntime, sandbox_id: UUID, *, reason: str, tool_label: str
) -> None:
    # A destroy failure must not mask the cancellation — the supervisor's
    # TTL reaper is the backstop for a leaked container.
    try:
        await client.destroy(sandbox_id=sandbox_id, reason=reason)
    except Exception:
        logger.exception("%s.destroy_failed sandbox=%s", tool_label, sandbox_id)


@dataclass
class ExecPythonTool:
    """Sandbox Python execution exposed to the LLM as ``exec_python``."""

    client: SandboxRuntime
    output_char_cap: int = DEFAULT_OUTPUT_CHAR_CAP
    #: skill-runtime §5.1 — the agent's activated skill files, materialized
    #: under ``/opt/skills/<agent_key>/<name>/`` on each acquire (sandbox
    #: migration wave 2). Set at build.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="exec_python",
            description=(
                "Execute a Python 3 snippet in an isolated sandbox and return "
                "its stdout / stderr / exit code. Use for calculations, data "
                "transforms, or anything better done by running code."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to execute.",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 300,
                        "description": "Execution timeout in seconds (optional).",
                    },
                },
                "required": ["code"],
            },
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        code = self._require_code(args)
        timeout_s = coerce_timeout(args.get("timeout_s"))
        outcome = await run_in_sandbox(
            self.client,
            code=code,
            timeout_s=timeout_s,
            ctx=ctx,
            tool_label="exec_python",
            fallback_thread_id=_FALLBACK_THREAD_ID,
            seed_files=self.skill_seed_files,
        )
        return format_sandbox_outcome(outcome, self.output_char_cap)

    def _require_code(self, args: Mapping[str, Any]) -> str:
        raw = args.get("code")
        if not isinstance(raw, str) or not raw.strip():
            msg = "exec_python requires a non-empty 'code' string"
            raise ValueError(msg)
        return raw


def coerce_timeout(raw: object) -> int | None:
    """Read an optional ``timeout_s`` arg; reject ``bool`` and out-of-range."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return max(1, min(300, raw))


def _truncate(text: str, cap: int) -> tuple[str, bool]:
    """Head-truncate ``text`` to ``cap`` chars; return ``(text, was_truncated)``."""
    if len(text) <= cap:
        return text, False
    return text[:cap] + _TRUNCATION_MARKER, True
