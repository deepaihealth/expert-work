"""B-47 self-audit — advisory-lock ``classid`` values must stay unique.

Two locks shared ``8619`` for weeks (``trigger_delivery`` and
``workspace_janitor``) while both of their comments claimed the value was
freshly taken. Nothing caught it because nothing checked: the "registry" was
prose spread over seven modules. These tests make it checkable — a duplicate
value fails, a constant missing from the mapping fails (it would otherwise
dodge the duplicate check), and a ``classid`` written as a literal anywhere
outside the registry module fails (that is how the collision happened).
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from control_plane import advisory_locks

#: Keys the advisory-lock SQL binds its classid to.
_CLASSID_BIND_KEYS = frozenset({"cid", "classid"})


def _control_plane_sources() -> list[Path]:
    registry = Path(advisory_locks.__file__).resolve()
    root = registry.parent
    # The scan's scope is "every module in the control plane", derived from
    # where the registry sits. If the registry ever moves into a subpackage
    # that derivation would silently shrink the scan instead of failing, so
    # pin the expectation rather than trusting the path.
    assert root.name == "control_plane", (
        f"expected the registry at the package root, found it in {root} — "
        "the literal scan below would only cover that subpackage"
    )
    sources = sorted(p for p in root.rglob("*.py") if p != registry)
    # An empty (or badly shrunk) enumeration makes the literal scan below pass
    # vacuously — the failure mode where the audit is blind but green. The
    # package holds ~225 modules; anything near zero is a broken scan, not a
    # clean codebase.
    assert len(sources) > 100, (
        f"only {len(sources)} modules found under {root} — the enumeration is "
        "broken, not the codebase"
    )
    return sources


def test_no_two_locks_share_a_classid() -> None:
    owners_by_value: dict[int, list[str]] = {}
    for owner, value in advisory_locks.CLASSID_BY_OWNER.items():
        owners_by_value.setdefault(value, []).append(owner)
    counts = Counter(advisory_locks.CLASSID_BY_OWNER.values())
    shared = {value: owners_by_value[value] for value, n in counts.items() if n > 1}
    assert not shared, f"advisory-lock classids must be unique — shared values: {shared}"


def test_every_classid_constant_is_registered() -> None:
    """A constant outside ``CLASSID_BY_OWNER`` would dodge the duplicate check."""
    registered = set(advisory_locks.CLASSID_BY_OWNER.values())
    constants = {
        name: value
        for name, value in vars(advisory_locks).items()
        if name.endswith("_CLASSID") and isinstance(value, int)
    }
    unregistered = sorted(name for name, value in constants.items() if value not in registered)
    assert not unregistered, (
        f"every *_CLASSID constant must appear in CLASSID_BY_OWNER; missing: {unregistered}"
    )


def test_no_module_defines_its_own_classid_literal() -> None:
    """``classid`` numbers live in the registry, never as a literal in a caller.

    Both halves matter: a module-level ``_X_LOCK_CLASSID = 8619`` (how the
    collision was written the first time) and a number bound straight into the
    lock SQL's parameters.
    """
    offenders: list[str] = []
    for path in _control_plane_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            named_classid = any(
                isinstance(t, ast.Name) and t.id.upper().endswith("_CLASSID") for t in targets
            )
            value = getattr(node, "value", None)
            if named_classid and isinstance(value, ast.Constant) and isinstance(value.value, int):
                offenders.append(f"{path.name}:{node.lineno} literal classid constant")
            if isinstance(node, ast.Dict):
                for key, item in zip(node.keys, node.values, strict=True):
                    binds_classid = (
                        isinstance(key, ast.Constant) and key.value in _CLASSID_BIND_KEYS
                    )
                    if (
                        binds_classid
                        and isinstance(item, ast.Constant)
                        and isinstance(item.value, int)
                    ):
                        offenders.append(f"{path.name}:{node.lineno} literal classid bind")
    assert not offenders, (
        "advisory-lock classids must come from control_plane.advisory_locks; "
        f"found literals at: {offenders}"
    )
