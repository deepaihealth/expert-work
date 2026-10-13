"""Expert Work Control Plane service (Stream B).

``create_app`` is exported lazily (PEP 562): the knowledge ingestion pipeline
parses documents in spawned worker processes that unpickle
``control_plane.knowledge.parsing.parse_document`` — importing this package
eagerly would drag the whole FastAPI app (~3.6k modules, ~0.5 GB RSS per
worker) into every parse worker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from control_plane.app import create_app

__version__ = "0.0.0"

__all__ = ["__version__", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from control_plane.app import create_app

        return create_app
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
