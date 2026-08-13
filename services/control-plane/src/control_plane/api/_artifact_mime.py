"""Artifact download MIME inference + XSS defence — Stream J.9-step3.

The download endpoint (``GET /v1/artifacts/download``) used to return
``application/octet-stream`` for every artifact regardless of kind /
extension. That was safe by accident — clients always saved to disk —
but failed three real requirements the 2026-05-21 expert-work-vs-deer-flow
review surfaced (STREAM-J-DESIGN § 10.5, Mini-ADR J-25 第 (4) 项):

1. **XSS — (c) red line.** A user can save an HTML / SVG / XHTML
   artifact, then a UI client that trusts ``Content-Type`` could
   inline-render it; ``<script>`` inside the bytes executes in the app's
   origin (stored XSS). The fix is non-negotiable: any "active content"
   MIME *must* go out with
   ``Content-Disposition: attachment; filename=…`` so browsers never
   inline-render it.
2. **Type-aware preview.** Text / code / image artifacts should send
   the real ``Content-Type`` so admin UI (Stream H.4) + API clients can
   render inline without sniffing.
3. **Sniff lockout.** ``X-Content-Type-Options: nosniff`` keeps older
   browsers from second-guessing the server-set content type.

The implementation is deliberately **whitelist-driven**: anything not
explicitly mapped falls through to ``application/octet-stream`` +
``attachment``. ``mimetypes.guess_type`` is **avoided** — it maps SVG
to ``image/svg+xml`` (an XSS vector through inline rendering) and would
silently expand the attack surface every time stdlib updates its DB.

The module is a pure function — no FastAPI / state. Tested directly.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal

from expert_work.protocol import ArtifactKind

__all__ = [
    "ContentDisposition",
    "InferredContentType",
    "content_disposition_header",
    "infer_content_type",
]


def content_disposition_header(filename: str, *, disposition: ContentDisposition) -> str:
    """RFC 6266 ``Content-Disposition`` with both ASCII fallback + utf-8.

    ``filename=`` carries an ASCII-safe approximation (replacing anything
    outside printable ASCII with ``_``) for legacy clients; ``filename*=UTF-8''…``
    carries the percent-encoded original. Quoting the ASCII fallback escapes
    embedded quotes and backslashes — defence against a CR/LF / quote / backslash
    in the name leaking into the header. A trailing backslash left unescaped
    would otherwise escape the closing quote of the ``filename=`` quoted-string
    (RFC 6266), letting lenient parsers swallow the following
    ``filename*=UTF-8''…`` segment into the ``filename`` value and degrade the
    display name.
    """
    ascii_safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    encoded = urllib.parse.quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_safe}\"; filename*=UTF-8''{encoded}"


ContentDisposition = Literal["inline", "attachment"]


# Active-content extensions — anything that a browser could parse + execute
# inline. ``Content-Disposition: attachment`` is mandatory for these.
_ACTIVE_CONTENT_EXTS: frozenset[str] = frozenset(
    {
        ".html",
        ".htm",
        ".xhtml",
        ".xht",
        ".svg",
        ".svgz",
        ".xml",
        ".xsl",
        ".xslt",
        ".mathml",
    }
)

# Inline-safe text/code extensions. Sent as ``text/plain; charset=utf-8``
# unless a more specific mapping below applies — this keeps the surface
# small (no language-specific MIME) and prevents browsers from running
# the bytes (no ``application/javascript`` etc).
_TEXT_LIKE_EXTS: frozenset[str] = frozenset(
    {
        ".txt",
        ".log",
        ".md",
        ".markdown",
        ".rst",
        ".csv",
        ".tsv",
        ".ini",
        ".conf",
        # Code — served as text/plain so the browser shows source, never
        # executes it (no ``application/javascript`` / ``text/html``).
        ".py",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".scala",
        ".rb",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".sql",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".swift",
        ".dart",
        ".lua",
        ".r",
        ".jl",
        ".pl",
        ".vue",
    }
)

# Structured-text data formats with their canonical MIME — also inline,
# also safe (no script execution).
_STRUCTURED_TEXT_MIME: Mapping[str, str] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".toml": "application/toml",
}

# Raster images — inline-safe (browsers don't execute pixels).
_IMAGE_MIME: Mapping[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/vnd.microsoft.icon",
    # ``.svg`` deliberately NOT here — it lives in ``_ACTIVE_CONTENT_EXTS``.
}


class InferredContentType:
    """The triple a download handler needs to build its response."""

    __slots__ = ("content_type", "disposition", "is_text")

    def __init__(
        self,
        *,
        content_type: str,
        disposition: ContentDisposition,
        is_text: bool,
    ) -> None:
        self.content_type = content_type
        self.disposition = disposition
        self.is_text = is_text

    def __repr__(self) -> str:
        return (
            f"InferredContentType(content_type={self.content_type!r}, "
            f"disposition={self.disposition!r}, is_text={self.is_text!r})"
        )


def _extension(path: str) -> str:
    """Lower-case last suffix of ``path``, including the leading dot."""
    return PurePosixPath(path).suffix.lower()


def infer_content_type(*, kind: ArtifactKind, path: str) -> InferredContentType:
    """Map an artifact's ``kind`` + workspace path to a safe response triple.

    Mapping rules (whitelist-first, unknown fallthrough is always safe):

    * Active-content extensions (``.html`` / ``.svg`` / etc) → real
      MIME + **attachment** disposition. (c) red-line — never inline.
    * Image extensions → ``image/<format>`` + inline.
    * Structured text (``.json`` / ``.yaml`` / ``.toml`` / ``.ndjson``)
      → canonical MIME + inline.
    * Text / code extensions → ``text/plain; charset=utf-8`` + inline.
    * Anything else, including ``kind=data`` + unknown extensions →
      ``application/octet-stream`` + attachment.
    """
    ext = _extension(path)
    if ext in _ACTIVE_CONTENT_EXTS:
        # Set the real MIME so error logs / SOC tooling can see what kind
        # of active content this was — disposition is what keeps the
        # browser from rendering it.
        return InferredContentType(
            content_type=_active_content_mime(ext),
            disposition="attachment",
            is_text=True,
        )
    if ext in _IMAGE_MIME:
        return InferredContentType(
            content_type=_IMAGE_MIME[ext],
            disposition="inline",
            is_text=False,
        )
    if ext in _STRUCTURED_TEXT_MIME:
        return InferredContentType(
            content_type=_STRUCTURED_TEXT_MIME[ext],
            disposition="inline",
            is_text=True,
        )
    if ext in _TEXT_LIKE_EXTS:
        return InferredContentType(
            content_type="text/plain; charset=utf-8",
            disposition="inline",
            is_text=True,
        )
    # ``kind=document`` with no extension is still treated as opaque
    # bytes — better a needless download than a wrong inline.
    del kind
    return InferredContentType(
        content_type="application/octet-stream",
        disposition="attachment",
        is_text=False,
    )


def _active_content_mime(ext: str) -> str:
    """Real MIME for an active-content extension (used as the response
    ``Content-Type`` even though disposition is attachment)."""
    if ext in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    if ext in {".xhtml", ".xht"}:
        return "application/xhtml+xml"
    if ext == ".svgz":
        return "image/svg+xml"
    if ext == ".svg":
        return "image/svg+xml"
    if ext in {".xsl", ".xslt"}:
        return "application/xslt+xml"
    if ext == ".mathml":
        return "application/mathml+xml"
    return "application/xml"
