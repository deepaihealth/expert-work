"""Manifest-loader exceptions, all rooted at :class:`ManifestError`."""

from __future__ import annotations


class ManifestError(Exception):
    """Base class for any manifest-loading failure."""


class ManifestSyntaxError(ManifestError):
    """YAML failed to parse, or the parsed root is not a mapping."""


class ManifestValidationError(ManifestError):
    """The YAML parsed cleanly but Pydantic / lint rules rejected it."""

    def __init__(self, message: str, *, errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []
