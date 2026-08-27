"""动态子智能体委派率增强(层 2)— subtask-delegation scale rubric wiring.

``_assemble_system_prompt`` appends the ``# Subtask delegation`` rubric when
the build registered the ``spawn_worker`` tool. The rubric is domain-free by
design (work *shapes* only) and appends last, so an agent prompt carrying its
own delegation policy coexists with it. An agent without the tool must not
carry a single character of it — the strict-absence tests pin that down.
"""

from __future__ import annotations

from orchestrator.agent_factory import _WORKER_DELEGATION_BLOCK, _assemble_system_prompt


def test_worker_delegation_appends_rubric_to_base() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], worker_delegation=True)
    assert prompt.startswith("BASE")
    assert "# Subtask delegation" in prompt
    assert _WORKER_DELEGATION_BLOCK in prompt


def test_worker_delegation_block_carries_scale_rubric() -> None:
    # The rubric's load-bearing phrases — pinned so a future edit cannot
    # silently drop a tier or the red lines.
    block = _WORKER_DELEGATION_BLOCK
    # Tier 1 — single-point work stays in the main line.
    assert "do not delegate" in block
    # Tier 2 — multi-track shape criteria + the tool by name.
    assert "three or more similar, mutually independent sub-items" in block
    assert "spawn_worker" in block
    # Tier 3 — large work is batched and reviewed.
    assert "delegate in batches" in block
    # Red lines — writes / judgment / final call never leave the main line.
    assert "Never delegate writes" in block
    # Self-contained task contract.
    assert "self-contained" in block
    assert "sees none of this conversation" in block


def test_no_worker_delegation_leaves_base_unchanged() -> None:
    prompt = _assemble_system_prompt(base="BASE", skill_fragments=[], worker_delegation=False)
    assert prompt == "BASE"
    assert "Subtask delegation" not in prompt
    assert "spawn_worker" not in prompt


def test_worker_delegation_appends_after_other_blocks() -> None:
    # 注入位置 — the rubric is the prompt's final section, after the advisory
    # blocks (memory is the last of those).
    prompt = _assemble_system_prompt(
        base="BASE",
        skill_fragments=[],
        memory_blocks=["<long-term-memory>M</long-term-memory>"],
        current_date="DATELINE",
        worker_delegation=True,
    )
    assert prompt.index("# Subtask delegation") > prompt.index("# Long-term memory")
    assert prompt.index("# Subtask delegation") > prompt.index("# Current date")
