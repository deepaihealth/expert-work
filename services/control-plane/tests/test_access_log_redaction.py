"""B-326 round-3 M5 —— the share-link id never reaches uvicorn's access log."""

from __future__ import annotations

import logging

import pytest

from control_plane.access_log import (
    ACCESS_LOGGER,
    RedactShareLinkFilter,
    install_access_log_redaction,
    redact_share_path,
)
from control_plane.app import create_app
from control_plane.settings import Settings


def _access_record(path: str) -> logging.LogRecord:
    """The exact shape uvicorn's ``httptools`` / ``h11`` protocols log."""
    return logging.LogRecord(
        ACCESS_LOGGER,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.5:4321", "GET", path, "1.1", 200),
        None,
    )


@pytest.mark.parametrize(
    "path",
    [
        "/v1/public/files/AbCdEfGhIjKlMnOp",
        "/v1/public/files/AbCdEfGhIjKlMnOp/7%E5%A4%A9%E8%A1%80%E7%B3%96.pdf",
        "/v1/public/files/AbCdEfGhIjKlMnOp/report.pdf?x=1",
    ],
)
def test_filter_redacts_the_id_in_both_path_forms(path: str) -> None:
    record = _access_record(path)
    assert RedactShareLinkFilter().filter(record) is True
    line = record.getMessage()
    assert "AbCdEfGhIjKlMnOp" not in line
    assert "/v1/public/files/{link_id}" in line


def test_other_paths_are_untouched() -> None:
    record = _access_record("/v1/agents/x/sessions?user_id=u")
    RedactShareLinkFilter().filter(record)
    assert (
        record.getMessage() == '10.0.0.5:4321 - "GET /v1/agents/x/sessions?user_id=u HTTP/1.1" 200'
    )
    assert redact_share_path("/v1/public/other/abc") == "/v1/public/other/abc"


def test_create_app_installs_the_filter_once() -> None:
    logger = logging.getLogger(ACCESS_LOGGER)
    logger.filters = [f for f in logger.filters if not isinstance(f, RedactShareLinkFilter)]
    create_app(settings=Settings(env="dev", auth_mode="dev"))
    assert sum(isinstance(f, RedactShareLinkFilter) for f in logger.filters) == 1
    create_app(settings=Settings(env="dev", auth_mode="dev"))
    install_access_log_redaction()
    assert sum(isinstance(f, RedactShareLinkFilter) for f in logger.filters) == 1


def test_an_emitted_access_line_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    install_access_log_redaction()
    logger = logging.getLogger(ACCESS_LOGGER)
    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER):
        logger.info(
            '%s - "%s %s HTTP/%s" %d',
            "10.0.0.5:4321",
            "GET",
            "/v1/public/files/AbCdEfGhIjKlMnOp/report.pdf",
            "1.1",
            200,
        )
    assert "AbCdEfGhIjKlMnOp" not in caplog.text
    assert "/v1/public/files/{link_id}" in caplog.text
