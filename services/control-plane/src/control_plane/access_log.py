"""Keep the share-link credential out of the access log — B-326.

``GET /v1/public/files/<id>[/<name>]``: the id is the download credential and uvicorn's
access log (on, unfiltered — ``services/control-plane/Dockerfile``) would write it in
full. :class:`RedactShareLinkFilter` rewrites such paths to
``/v1/public/files/{link_id}`` on ``uvicorn.access`` records. ``create_app`` installs it
(idempotently), so it covers every way the app is served — uvicorn configures its
loggers before it imports the app, and a filter added afterwards stays attached.
"""

from __future__ import annotations

import logging
import re

ACCESS_LOGGER = "uvicorn.access"
_SHARE_PATH = re.compile(r"/v1/public/files/[^\s?\"']+")
_REDACTED = "/v1/public/files/{link_id}"


def redact_share_path(text: str) -> str:
    """``text`` with every share-link path (id and optional name segment) redacted."""
    return _SHARE_PATH.sub(_REDACTED, text)


class RedactShareLinkFilter(logging.Filter):
    """Rewrites share-link paths in a record's message and arguments; never drops it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_share_path(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_share_path(a) if isinstance(a, str) else a for a in record.args
            )
        return True


def install_access_log_redaction() -> None:
    """Attach :class:`RedactShareLinkFilter` to ``uvicorn.access`` once."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, RedactShareLinkFilter) for f in logger.filters):
        logger.addFilter(RedactShareLinkFilter())
