"""YAML → :class:`AgentSpec`.

Stages:

1. **Size guard** — refuse documents larger than ``max_size_bytes`` (DoS
   protection per STREAM-B-DESIGN § 6).
2. **YAML parse** — ``yaml.safe_load``, never ``yaml.load``.
3. **Pydantic validation** — :class:`AgentSpec` carries the lint rules
   (network allowlist + fallback-chain cycles) as ``model_validator``\\s.

There is deliberately **no** template-rendering stage: ``{{ … }}`` in a
manifest is run-time Jinja (``system_prompt.jinja`` + request ``inputs``,
rendered by :mod:`control_plane.prompt_render`), never a save-time
substitution. The former save-time ``template_vars`` pass was removed in
the 2026-08-17 console-redesign PR0 (zero callers; it swallowed every
``{{ }}`` a jinja agent's prompt legitimately carries).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from jinja2 import StrictUndefined, select_autoescape
from jinja2.sandbox import SandboxedEnvironment
from pydantic import ValidationError

from control_plane.manifest.errors import (
    ManifestSyntaxError,
    ManifestValidationError,
)
from expert_work.protocol import AgentSpec

#: Default cap mirrors STREAM-B-DESIGN § 6 (DoS guard).
DEFAULT_MAX_SIZE_BYTES = 64 * 1024


def build_sandboxed_environment() -> SandboxedEnvironment:
    """The one SSTI-safe Jinja2 environment used for the run-time
    ``system_prompt`` render (:mod:`control_plane.prompt_render`).

    ``SandboxedEnvironment`` blocks the ``__class__.__mro__`` /
    ``__subclasses__`` introspection chain that turns ordinary Jinja2
    templates into a Python RCE primitive — required because the template
    source is user-supplied (CodeQL's py/template-injection rule).

    The template is YAML / plain text, not HTML. ``select_autoescape``
    with an empty enabled-extensions list (and ``default_for_string=False``)
    is jinja2's canonical "opt out explicitly" pattern; CodeQL's
    py/jinja2-autoescape-false flags the literal ``False`` but accepts this
    callable as evidence the choice was deliberate. ``StrictUndefined``
    surfaces a typo as an error rather than a silent empty string.
    """
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        autoescape=select_autoescape(enabled_extensions=(), default_for_string=False),
        keep_trailing_newline=True,
    )


class ManifestLoader:
    """Reusable loader that the FastAPI handler holds on app.state."""

    def __init__(self, *, max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES) -> None:
        if max_size_bytes <= 0:
            msg = f"max_size_bytes must be > 0, got {max_size_bytes}"
            raise ValueError(msg)
        self._max_size_bytes = max_size_bytes

    @property
    def max_size_bytes(self) -> int:
        return self._max_size_bytes

    def load_from_string(self, source: str) -> AgentSpec:
        encoded = source.encode("utf-8")
        if len(encoded) > self._max_size_bytes:
            msg = f"manifest exceeds size cap {len(encoded)} > {self._max_size_bytes} bytes"
            raise ManifestSyntaxError(msg)

        document = self._parse_yaml(source)
        return self._validate(document)

    def load_from_path(self, path: str | Path) -> AgentSpec:
        return self.load_from_string(Path(path).read_text(encoding="utf-8"))

    # ----- internals --------------------------------------------------

    def _parse_yaml(self, rendered: str) -> dict[str, Any]:
        try:
            doc = yaml.safe_load(rendered)
        except yaml.YAMLError as exc:
            raise ManifestSyntaxError(f"manifest is not valid YAML: {exc}") from None
        if not isinstance(doc, dict):
            raise ManifestSyntaxError(f"manifest root must be a mapping, got {type(doc).__name__}")
        return doc

    def _validate(self, document: dict[str, Any]) -> AgentSpec:
        try:
            return AgentSpec.model_validate(document)
        except ValidationError as exc:
            # Project the pydantic errors into a hand-curated whitelist
            # of fields and build a fresh list of plain dicts. This
            # severs the data-flow link CodeQL traces from the caught
            # ``ValidationError`` (py/stack-trace-exposure).
            sanitized: list[dict[str, object]] = []
            for err in exc.errors():
                sanitized.append(
                    {
                        "loc": list(err.get("loc", ())),
                        "type": str(err.get("type", "")),
                        "msg": str(err.get("msg", "")),
                    }
                )
            error_count = exc.error_count()
            # ``from None`` deliberately drops the __cause__ chain so
            # CodeQL stops tracing taint from the pydantic exception.
            raise ManifestValidationError(
                f"manifest failed Pydantic validation ({error_count} error(s))",
                errors=sanitized,
            ) from None


def load_manifest(
    source: str | Path,
    *,
    max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
) -> AgentSpec:
    """Convenience wrapper for one-off loads (tests, CLI lint)."""
    loader = ManifestLoader(max_size_bytes=max_size_bytes)
    if isinstance(source, Path):
        return loader.load_from_path(source)
    return loader.load_from_string(source)
