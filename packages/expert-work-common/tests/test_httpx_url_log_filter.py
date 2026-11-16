"""B-308 §4.7 — ``httpx`` request logs keep the origin and drop the path.

``httpx`` logs every request's full URL at INFO; a custom request address can
carry a token in its path. The filter rewrites the URL argument to
``scheme://host[:port]`` for every httpx request; ``httpcore`` is pinned at
WARNING so a DEBUG root level does not bring connection detail back.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import httpx
import pytest

from expert_work.common.observability.log import (
    HttpxUrlFilter,
    init_logging,
    install_httpx_url_filter,
)

_MARKER = "TOKEN-IN-PATH-7f3a"
_URL = f"https://proxy.example.com:8443/{_MARKER}/v1/chat/completions"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name} {record.getMessage()}")


@pytest.fixture
def clean_httpx_logger() -> Iterator[logging.Logger]:
    """The ``httpx`` logger with no URL filter installed; restored afterwards."""
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    saved_filters = list(httpx_logger.filters)
    saved_levels = (httpx_logger.level, httpcore_logger.level, logging.getLogger().level)
    saved_disabled = httpx_logger.disabled
    for existing in saved_filters:
        httpx_logger.removeFilter(existing)
    # Another test in the same process may have switched existing loggers off
    # (``logging.config.fileConfig`` does) or raised this one's level: these
    # tests are about what httpx logs when it does log.
    httpx_logger.disabled = False
    httpx_logger.setLevel(logging.NOTSET)
    try:
        yield httpx_logger
    finally:
        httpx_logger.disabled = saved_disabled
        for current in list(httpx_logger.filters):
            httpx_logger.removeFilter(current)
        for existing in saved_filters:
            httpx_logger.addFilter(existing)
        httpx_logger.setLevel(saved_levels[0])
        httpcore_logger.setLevel(saved_levels[1])
        logging.getLogger().setLevel(saved_levels[2])


@pytest.fixture
def capture() -> Iterator[_Capture]:
    handler = _Capture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield handler
    finally:
        root.removeHandler(handler)


async def _request_once() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.post(_URL, json={})


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_httpx_logger")
async def test_without_the_filter_httpx_logs_the_path(capture: _Capture) -> None:
    # The premise: this is what the filter exists to stop.
    logging.getLogger().setLevel(logging.INFO)
    await _request_once()
    assert any(_MARKER in line for line in capture.lines)


@pytest.mark.asyncio
@pytest.mark.usefixtures("clean_httpx_logger")
@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
async def test_init_logging_keeps_host_and_drops_path(capture: _Capture, level: int) -> None:
    init_logging(service="control_plane", env="test", level=level, stream=io.StringIO())
    await _request_once()

    assert not [line for line in capture.lines if _MARKER in line]
    request_lines = [line for line in capture.lines if "HTTP Request:" in line]
    assert len(request_lines) == 1
    assert "POST https://proxy.example.com:8443 " in request_lines[0]
    assert "200 OK" in request_lines[0]


@pytest.mark.usefixtures("clean_httpx_logger")
def test_httpcore_is_pinned_at_warning_even_under_a_debug_root() -> None:
    init_logging(service="control_plane", env="test", level=logging.DEBUG, stream=io.StringIO())
    assert logging.getLogger("httpcore").level == logging.WARNING
    assert not logging.getLogger("httpcore.connection").isEnabledFor(logging.DEBUG)
    assert not logging.getLogger("httpcore.http11").isEnabledFor(logging.INFO)


def test_installing_twice_leaves_one_filter(clean_httpx_logger: logging.Logger) -> None:
    install_httpx_url_filter()
    install_httpx_url_filter()
    installed = [f for f in clean_httpx_logger.filters if isinstance(f, HttpxUrlFilter)]
    assert len(installed) == 1


@pytest.mark.parametrize("url_arg", [httpx.URL(_URL), _URL])
def test_url_argument_is_rewritten_as_object_or_string(url_arg: object) -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", url_arg, "HTTP/1.1", 200, "OK"),
        exc_info=None,
    )
    assert HttpxUrlFilter().filter(record) is True
    expected = 'HTTP Request: POST https://proxy.example.com:8443 "HTTP/1.1 200 OK"'
    assert record.getMessage() == expected


def test_other_records_pass_through_untouched() -> None:
    record = logging.LogRecord(
        name="httpx",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="load_ssl_context verify=%r cert=%r",
        args=(True, None),
        exc_info=None,
    )
    assert HttpxUrlFilter().filter(record) is True
    assert record.getMessage() == "load_ssl_context verify=True cert=None"
