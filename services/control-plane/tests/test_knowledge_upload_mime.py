"""Every extension the knowledge upload accepts must map to a mime.

Drift guard between ``SUPPORTED_EXTENSIONS`` (control plane) and the store's
fixed extension table (persistence): adding an upload format without a mime
fails here instead of silently storing ``None`` (B-175 acceptance, 10-14:
``.docx`` / ``.pptx`` / ``.xlsx`` all had ``mime = null`` on the test stack).
"""

from __future__ import annotations

import pytest

from control_plane.knowledge.parsing import SUPPORTED_EXTENSIONS
from expert_work.persistence.knowledge.base import DocumentFileFacts


@pytest.mark.parametrize("ext", sorted(SUPPORTED_EXTENSIONS))
def test_supported_extension_has_a_mime(ext: str) -> None:
    assert DocumentFileFacts.of(f"upload{ext}", b"x").mime is not None
