"""Unit tests for ``_artifact_mime`` — STREAM-J-DESIGN § 10.5."""

from __future__ import annotations

import re
import urllib.parse

import pytest

from control_plane.api._artifact_mime import content_disposition_header, infer_content_type


@pytest.mark.parametrize(
    ("path", "kind", "expected_ct", "expected_disp", "expected_text"),
    [
        # Text-like
        ("report.md", "document", "text/plain; charset=utf-8", "inline", True),
        ("notes.txt", "document", "text/plain; charset=utf-8", "inline", True),
        ("script.py", "code", "text/plain; charset=utf-8", "inline", True),
        ("module.ts", "code", "text/plain; charset=utf-8", "inline", True),
        # Structured text
        ("data.json", "data", "application/json", "inline", True),
        ("config.yaml", "data", "application/x-yaml", "inline", True),
        ("config.yml", "data", "application/x-yaml", "inline", True),
        ("pyproject.toml", "data", "application/toml", "inline", True),
        ("events.ndjson", "data", "application/x-ndjson", "inline", True),
        # Images (inline-safe)
        ("photo.png", "data", "image/png", "inline", False),
        ("photo.jpg", "data", "image/jpeg", "inline", False),
        ("photo.jpeg", "data", "image/jpeg", "inline", False),
        ("anim.gif", "data", "image/gif", "inline", False),
        ("photo.webp", "data", "image/webp", "inline", False),
    ],
)
def test_inline_safe_extensions(
    path: str,
    kind: str,
    expected_ct: str,
    expected_disp: str,
    expected_text: bool,
) -> None:
    inferred = infer_content_type(kind=kind, path=path)  # type: ignore[arg-type]
    assert inferred.content_type == expected_ct
    assert inferred.disposition == expected_disp
    assert inferred.is_text is expected_text


@pytest.mark.parametrize(
    "path",
    [
        "page.html",
        "page.htm",
        "page.xhtml",
        "page.xht",
        "logo.svg",
        "logo.svgz",
        "doc.xml",
        "style.xsl",
        "style.xslt",
        "math.mathml",
    ],
)
def test_active_content_always_attachment(path: str) -> None:
    """STREAM-J-DESIGN § 10.5 (c) red-line — HTML / SVG / etc never inline."""
    inferred = infer_content_type(kind="document", path=path)
    assert inferred.disposition == "attachment"


@pytest.mark.parametrize(
    "path",
    ["dump.bin", "weird.xyz", "no_extension", "data.unknown_ext"],
)
def test_unknown_extension_is_octet_attachment(path: str) -> None:
    inferred = infer_content_type(kind="data", path=path)
    assert inferred.content_type == "application/octet-stream"
    assert inferred.disposition == "attachment"


def test_case_insensitive_extension() -> None:
    inferred = infer_content_type(kind="document", path="REPORT.MD")
    assert inferred.content_type == "text/plain; charset=utf-8"
    assert inferred.disposition == "inline"


def test_active_content_real_mime_in_response() -> None:
    """The real MIME *does* land on Content-Type — only the disposition keeps
    the browser from rendering. SOC tooling can spot the active-content shape."""
    inferred = infer_content_type(kind="document", path="page.html")
    assert "text/html" in inferred.content_type
    assert inferred.disposition == "attachment"


def test_content_disposition_header_strips_backslash_from_ascii_fallback() -> None:
    """A stray backslash must not survive into the quoted ``filename=`` value.

    Left unescaped, a trailing backslash would escape the closing quote of the
    RFC 6266 quoted-string, letting lenient parsers swallow the following
    ``filename*=UTF-8''…`` segment into the ``filename`` value and degrade the
    displayed name.
    """
    filename = "back\\slash"
    header = content_disposition_header(filename, disposition="attachment")

    fallback_match = re.search(r'filename="([^"]*)"', header)
    assert fallback_match is not None
    assert "\\" not in fallback_match.group(1)

    star_match = re.search(r"filename\*=UTF-8''(\S+)$", header)
    assert star_match is not None
    assert star_match.group(1) == urllib.parse.quote(filename, safe="")
