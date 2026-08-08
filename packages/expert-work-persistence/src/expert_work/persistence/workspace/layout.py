"""Workspace volume layout conventions — Stream J.15.

A user's ``/workspace`` mixes three kinds of content in one flat volume:

* **agent output** — whatever the agent wrote (a generated PDF, ``out/…``);
  this is what a user actually wants to retrieve.
* **machinery** — activated skill packages the runtime seeds before exec
  (skill-runtime §5.1), materialised under ``skills/<name>/…``.
* **inputs** — documents the user uploaded for a later run's ``read_document``,
  landing under ``uploads/…``.

The machinery + input namespaces are *system-reserved*: the platform controls
exactly where they go, so they can be enumerated. Agent output, by contrast,
can be written anywhere and cannot be allow-listed. The browse / download
surface therefore **hides the reserved prefixes and shows everything else** —
the same model as ``.gitignore`` hiding generated dirs.

This module is the single source of truth for those prefixes: the seeders that
*write* them and the browser that *hides* them both import from here, so a path
change in one place can never silently desync from the filter.
"""

from __future__ import annotations

#: Activated skill packages, seeded at ``skills/<name>/…`` (skill-runtime §5.1).
#: Sandbox migration wave 2 (spec § 四) moved the actual seed *destination*
#: out from under this workspace prefix to :data:`SANDBOX_SKILLS_ROOT` — this
#: constant (and :data:`WORKSPACE_RESERVED_PREFIXES` below) stays only to keep
#: hiding any pre-wave-2 workspace residue from the browse view.
WORKSPACE_SKILLS_DIR = "skills"

#: User-uploaded documents, landing at ``uploads/<name>`` for ``read_document``.
WORKSPACE_UPLOADS_DIR = "uploads"

#: Sandbox-local seed root for an agent's activated skill files (sandbox
#: migration wave 2, spec § 四 — "技能方案"). Materialized at
#: ``{SANDBOX_SKILLS_ROOT}/<agent_key>/<skill-name>/…`` on every sandbox
#: acquire, per-agent namespaced so two agents sharing one warm sandbox never
#: clobber each other's skill files. Both backends seed here (the cloud
#: ``AgentSandboxClient`` and the local ``sandbox-supervisor``, Task 6) —
#: sandbox-local disk, not the user's NAS-backed ``/workspace``, so it never
#: occupies workspace quota or shows up in the workspace browse surface.
SANDBOX_SKILLS_ROOT = "/opt/skills"

#: Sandbox-local ``PYTHONUSERBASE`` root, per agent (spec 决策 10). Same
#: sharing problem as skills: two agents on one warm sandbox share
#: ``$HOME/.local`` by default, so a ``pip install --user`` from one can
#: clobber or race the other's. Injected as
#: ``PYTHONUSERBASE={SANDBOX_AGENTS_ROOT}/<agent_key>`` on every ``exec`` call
#: (not just acquire — see ``orchestrator.tools.sandbox.agent_key_envs``).
#: The directory is pip's to create; nothing pre-creates or chowns it beyond
#: the image-level ``/opt/agents`` (sandbox migration wave 2 Task 9).
SANDBOX_AGENTS_ROOT = "/opt/agents"

#: Top-level workspace prefixes that hold machinery / inputs rather than agent
#: output — hidden from the "agent products" browse view. Add a new reserved
#: namespace here (and use the matching constant where it is written) and every
#: browse surface picks it up automatically.
WORKSPACE_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR}
)


def is_reserved_workspace_path(relpath: str) -> bool:
    """Return whether ``relpath`` lives under a reserved (non-output) namespace.

    Compares the first path segment against :data:`WORKSPACE_RESERVED_PREFIXES`;
    a bare top-level file (no ``/``) is never reserved.
    """
    return relpath.split("/", 1)[0] in WORKSPACE_RESERVED_PREFIXES
