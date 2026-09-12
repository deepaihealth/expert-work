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

#: Container for the per-agent subtrees — ``agents/<agent_key>/…`` (B-50).
#: A user's volume is mounted per ``(tenant, user)`` and cannot be split by
#: agent at the mount point (hot sandboxes are reused across agents, the CSI
#: ``subPath`` is fixed at create time — spec § 三), so the split is done in
#: the path and enforced at the tool boundary.
WORKSPACE_AGENTS_DIR = "agents"

#: Legacy whose owning agent could not be inferred at migration time (B-50
#: § 7.1). Readable from any agent via the explicit ``shared:`` prefix,
#: never writable — it is frozen on migration day and decays with time.
WORKSPACE_SHARED_DIR = "shared"

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

#: Tool-result overflow cache — ``.tool_results/<run_id>/…``. Pure machinery:
#: a run externalises an over-long tool result here and references it back by
#: path. Nothing a person browsing the workspace wants to see.
#:
#: Same value as ``orchestrator.tools.overflow.OVERFLOW_DIR``; that module
#: imports this one rather than keeping a second literal.
WORKSPACE_OVERFLOW_DIR = ".tool_results"

#: Workspace prefixes that hold machinery / inputs rather than agent output —
#: hidden from the "agent products" browse view. Add a new reserved namespace
#: here (and use the matching constant where it is written) and every browse
#: surface picks it up automatically.
WORKSPACE_RESERVED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR, WORKSPACE_OVERFLOW_DIR}
)

#: Prefixes a *client* may never delete — the browse-hidden set minus the
#: overflow cache.
#:
#: Hiding and delete-protection used to be the same set, which was right while
#: it held only ``skills`` (seeded machinery) and ``uploads`` (user input):
#: both are written by the platform and must survive whatever a client asks
#: for. ``.tool_results`` breaks that tie — it is equally uninteresting to a
#: browsing human, but it is *garbage* the platform itself has to be able to
#: collect: the session-purge hook rm -rf's ``.tool_results/<run_id>/`` through
#: ``delete_tree``, and a single shared set would make that call raise
#: "path is reserved and cannot be deleted".
#:
#: So the two concerns are now two constants. Reusing one for both is the
#: kind of coupling that only shows up when a new member disagrees with the
#: others — and then it shows up as a feature that silently cannot work.
WORKSPACE_DELETE_PROTECTED_PREFIXES: frozenset[str] = frozenset(
    {WORKSPACE_SKILLS_DIR, WORKSPACE_UPLOADS_DIR}
)

#: Container prefixes that are *positional*, not reserved: they say **whose**
#: subtree follows, not what kind of content it is. They are stripped before
#: the reserved check so the check keeps meaning the same thing at every depth.
#: ``agents`` takes a key segment after it, ``shared`` takes none.
_CONTAINERS: dict[str, int] = {WORKSPACE_AGENTS_DIR: 2, WORKSPACE_SHARED_DIR: 1}


def _head_after_container(relpath: str) -> str:
    """The first path segment once a known container prefix is stripped.

    ``""`` for an empty path. See :func:`is_reserved_workspace_path` for why
    only known containers are stripped and only one segment is examined.
    """
    parts = tuple(p for p in relpath.strip().split("/") if p)
    if not parts:
        return ""
    skip = _CONTAINERS.get(parts[0])
    if skip is not None and len(parts) > skip:
        parts = parts[skip:]
    return parts[0]


def is_reserved_workspace_path(relpath: str) -> bool:
    """Return whether ``relpath`` lives under a reserved (non-output) namespace.

    Historically this compared only the **top** segment, which was the whole
    truth while the workspace was one flat tree. B-50 moved everything under
    ``agents/<agent_key>/`` (and the un-attributable legacy under ``shared/``),
    so a top-segment rule stops matching ``agents/<key>/uploads/x.docx`` —
    and uploaded documents would suddenly appear in the browse view's
    "products" list. That is a *silent* behaviour change introduced by the
    migration: nothing errors, the list just grows files that were never
    agent output.

    So: strip a known container prefix, then look at the first segment of
    what remains. Only **known** containers are stripped and only the first
    segment is checked — deliberately not "any segment named uploads",
    which would swallow an agent's own ``客户案例/uploads/`` directory.

    A bare top-level file (no ``/``) is never reserved, and neither is a
    bare container directory with nothing under it.
    """
    return _head_after_container(relpath) in WORKSPACE_RESERVED_PREFIXES


def is_delete_protected_workspace_path(relpath: str) -> bool:
    """Whether a client is forbidden from deleting ``relpath``.

    A strict subset of :func:`is_reserved_workspace_path` — see
    :data:`WORKSPACE_DELETE_PROTECTED_PREFIXES` for why the two are not the
    same set. Callers guarding a delete endpoint want **this** one; callers
    building a browse listing want the other.
    """
    return _head_after_container(relpath) in WORKSPACE_DELETE_PROTECTED_PREFIXES
