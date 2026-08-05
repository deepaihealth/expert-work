"""``bash`` tool — Stream TE-5.

Runs an arbitrary shell command in the agent's gVisor sandbox, confined
to the per-user workspace. It is the "ultimate executor" escape hatch —
git / grep / pandoc / zip / format conversion — that a curated tool set
can't anticipate (TE-ADR-1).

**Design (TE-ADR-1 / TE-ADR-2)** — bash does NOT need a new supervisor
endpoint: it rides the existing ``exec`` channel by wrapping the command
in a tiny ``subprocess`` snippet, so it executes in exactly the same
sandbox (same gVisor boundary, same ``/workspace`` mount, same per-tenant
quota / timeout) as ``exec_python``. The sandbox — not the absence of a
bash tool — is the security boundary; ``exec_python`` already permits
arbitrary execution, so adding bash is an ergonomics win at ~zero
marginal risk.

The command string is embedded with ``repr()`` so it is a safe Python
literal regardless of quotes / newlines / backslashes — there is no
Python-level injection. ``shell=True`` then runs it via ``/bin/sh -c``;
the command's stdout / stderr flow through the snippet's, and the
command's exit code propagates via ``sys.exit`` so the LLM sees the real
shell exit code.

**Governance** — ``bash`` declares ``side_effect="irreversible"``: a
shell command can do anything (``rm -rf`` as easily as ``ls``). That
makes every bash call forced-serial (never shares a tool stage) and
fully audited. Approval is **config-driven**: bash pauses for human
review only when an operator lists it in the manifest's
``approval_required_tools`` — sandbox isolation + serialisation + audit
are the always-on safety floor, the gate is the explicit opt-in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from orchestrator.tools.locks import NullWorkspaceLock, WorkspaceLock
from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec
from orchestrator.tools.sandbox import (
    DEFAULT_OUTPUT_CHAR_CAP,
    SandboxRuntime,
    coerce_timeout,
    format_sandbox_outcome,
    run_in_sandbox,
)

#: ``acquire`` thread label when the run has no id (ad-hoc call).
_FALLBACK_THREAD_ID = "bash"


def _build_wrapper(command: str) -> str:
    """Wrap a shell command as a Python snippet for the exec channel.

    ``{command!r}`` embeds the command as a safe Python string literal
    (no injection); ``shell=True`` runs it via ``/bin/sh -c`` in the
    sandbox's ``/workspace`` cwd; ``sys.exit`` propagates the command's
    real exit code so the LLM sees it.

    A command killed by signal N has ``returncode == -N``; we map it to
    the shell convention ``128 + N`` so the LLM sees e.g. ``137`` for a
    SIGKILL (OOM / timeout-kill) rather than Python's raw ``sys.exit(-9)``
    which would otherwise surface as OS status ``247``.
    """
    return (
        "import subprocess, sys\n"
        f"rc = subprocess.run({command!r}, shell=True).returncode\n"
        "sys.exit(128 - rc if rc < 0 else rc)\n"
    )


@dataclass
class BashTool:
    """Sandboxed shell execution exposed to the LLM as ``bash``."""

    client: SandboxRuntime
    output_char_cap: int = DEFAULT_OUTPUT_CHAR_CAP
    #: Stream TE-8 — cross-replica per-workspace write lock. bash can write
    #: anything, so it takes the same exclusive workspace lock as write_file.
    workspace_lock: WorkspaceLock = field(default_factory=NullWorkspaceLock)
    #: skill-runtime §5.1 — activated skill files seeded under /workspace/skills/.
    skill_seed_files: tuple[tuple[str, bytes], ...] = ()

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="bash",
            description=(
                "Run a shell command in the agent's sandboxed workspace and "
                "return its stdout / stderr / exit code. Use for git, grep, "
                "file manipulation, format conversion, or anything not covered "
                "by a dedicated tool. Runs in /workspace inside an isolated "
                "sandbox. NOTE: this is an irreversible action — it runs "
                "serially and may require human approval before executing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run (via /bin/sh -c).",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 300,
                        "description": "Execution timeout in seconds (optional).",
                    },
                },
                "required": ["command"],
            },
            # Stream TE-5 / TE-ADR-1 — a shell command can do anything, so
            # bash is irreversible: that forces it serial (never shares a tool
            # stage) and audited. Approval is config-driven (``approval_required_tools``),
            # not auto-applied to irreversible tools.
            side_effect="irreversible",
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        command = self._require_command(args)
        timeout_s = coerce_timeout(args.get("timeout_s"))
        # Stream TE-8 — hold the per-workspace write lock around the command so
        # bash serialises against write_file (and other bash) across replicas.
        # The lock scopes to the run's user — the durable workspace that may be
        # shared across replicas; a user-less (ephemeral) run needs no lock.
        async with self.workspace_lock.acquire(tenant_id=ctx.tenant_id, user_id=ctx.user_id):
            outcome = await run_in_sandbox(
                self.client,
                code=_build_wrapper(command),
                timeout_s=timeout_s,
                ctx=ctx,
                tool_label="bash",
                fallback_thread_id=_FALLBACK_THREAD_ID,
                seed_files=self.skill_seed_files,
            )
        return format_sandbox_outcome(outcome, self.output_char_cap)

    def _require_command(self, args: Mapping[str, Any]) -> str:
        raw = args.get("command")
        if not isinstance(raw, str) or not raw.strip():
            msg = "bash requires a non-empty 'command' string"
            raise ValueError(msg)
        return raw
