"""Unit tests for workspace layout conventions (reserved-prefix filter)."""

from __future__ import annotations

from expert_work.persistence import (
    SANDBOX_AGENTS_ROOT,
    SANDBOX_SKILLS_ROOT,
    WORKSPACE_RESERVED_PREFIXES,
    WORKSPACE_SKILLS_DIR,
    WORKSPACE_UPLOADS_DIR,
    is_reserved_workspace_path,
)


def test_reserved_prefixes_cover_skills_and_uploads() -> None:
    assert WORKSPACE_SKILLS_DIR in WORKSPACE_RESERVED_PREFIXES
    assert WORKSPACE_UPLOADS_DIR in WORKSPACE_RESERVED_PREFIXES


def test_seeded_skill_and_upload_paths_are_reserved() -> None:
    assert is_reserved_workspace_path("skills/pptx/SKILL.md")
    assert is_reserved_workspace_path("uploads/ticket.pdf")


def test_agent_output_paths_are_not_reserved() -> None:
    # Bare top-level files and the agent's own output dirs stay visible.
    assert not is_reserved_workspace_path("report.pdf")
    assert not is_reserved_workspace_path("out/notes.txt")
    # A file literally named like a prefix (not a dir) is output, not reserved.
    assert not is_reserved_workspace_path("skills.md")


def test_sandbox_local_roots_are_absolute_and_distinct() -> None:
    # sandbox migration wave 2 (spec § 四 / 决策 10) — both roots live on
    # sandbox-local disk, outside the (NAS-backed) workspace tree entirely,
    # so they must never collide with WORKSPACE_* or each other.
    assert SANDBOX_SKILLS_ROOT == "/opt/skills"
    assert SANDBOX_AGENTS_ROOT == "/opt/agents"
    assert SANDBOX_SKILLS_ROOT != SANDBOX_AGENTS_ROOT
