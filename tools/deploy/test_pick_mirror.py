"""pick_mirror — keep the default while it is fast, else take the fastest.

The selection logic runs against fake rates; ``measure`` / ``exists`` run
against a local HTTP server (fast, throttled, missing, refused), so no test
touches the network.
"""

from __future__ import annotations

import functools
import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pick_mirror import Probe, choose, exists, lock_probe_files, measure

_BLOCK = b"x" * 4096


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/missing":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_BLOCK) * 256))
        self.end_headers()
        try:
            for _ in range(256):
                self.wfile.write(_BLOCK)
                if self.path == "/slow":
                    time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the probe stops reading once it has its sample

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


_quiet = functools.partial(choose, good_enough=1e6, log=lambda _line: None)


def _fake(rates: dict[str, float]) -> tuple[list[str], object]:
    probed: list[str] = []

    def measure_fn(url: str) -> Probe:
        probed.append(url)
        return Probe(url, rates[url])

    return probed, measure_fn


def test_a_fast_default_wins_without_probing_the_others() -> None:
    probed, fake = _fake({"a": 5e6, "b": 9e6})
    assert _quiet(["a", "b"], lambda c: c, measure_fn=fake) == "a"
    assert probed == ["a"]


def test_a_slow_default_gives_way_to_the_fastest() -> None:
    probed, fake = _fake({"a": 3e4, "b": 4e6, "c": 1.8e7, "d": 0.0})
    assert _quiet(["a", "b", "c", "d"], lambda c: c, measure_fn=fake) == "c"
    assert probed == ["a", "b", "c", "d"]


def test_nothing_answering_keeps_the_default_and_says_so() -> None:
    _, fake = _fake({"a": 0.0, "b": 0.0})
    lines: list[str] = []
    chosen = choose(["a", "b"], lambda c: c, good_enough=1e6, measure_fn=fake, log=lines.append)
    assert chosen == "a"
    assert lines[-1].strip() == "no mirror answered; keeping the default"


def test_a_mirror_missing_the_newest_file_is_never_chosen() -> None:
    _, fake = _fake({"a": 3e4, "lagging": 2e7, "b": 4e6})
    chosen = _quiet(
        ["a", "lagging", "b"], lambda c: c, acceptable=lambda c: c != "lagging", measure_fn=fake
    )
    assert chosen == "b"


def test_a_fast_default_that_lags_the_lock_is_not_kept() -> None:
    _, fake = _fake({"a": 5e6, "b": 4e6})
    chosen = _quiet(["a", "b"], lambda c: c, acceptable=lambda c: c != "a", measure_fn=fake)
    assert chosen == "b"


def test_measure_tells_a_fast_link_from_a_throttled_one(server: str) -> None:
    fast = measure(f"{server}/fast", seconds=1.0)
    slow = measure(f"{server}/slow", seconds=1.0)
    assert fast.bytes_per_second > 10 * slow.bytes_per_second > 0


def test_measure_reports_zero_for_404_and_refused(server: str) -> None:
    assert measure(f"{server}/missing", seconds=0.5) == Probe(f"{server}/missing", 0.0, "HTTPError")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    refused = measure(f"http://127.0.0.1:{closed_port}/x", seconds=0.5)
    assert refused.bytes_per_second == 0.0
    assert refused.note == "URLError"


def test_exists(server: str) -> None:
    assert exists(f"{server}/fast")
    assert not exists(f"{server}/missing")


def _lock_entry(path: str, size: int, uploaded: str) -> str:
    return (
        f'{{ url = "https://files.pythonhosted.org/packages/{path}", hash = "sha256:0", '
        f'size = {size}, upload-time = "{uploaded}" }}'
    )


def test_lock_probe_files_times_a_big_file_and_checks_the_newest() -> None:
    lock = "\n".join(
        [
            _lock_entry("aa/small-1.0.whl", 1000, "2026-10-20T00:00:00Z"),
            _lock_entry("bb/big-2.0.whl", 9000000, "2025-01-01T00:00:00Z"),
            _lock_entry("cc/huge-3.0.whl", 90000000, "2025-01-01T00:00:00Z"),
        ]
    )
    assert lock_probe_files(lock) == ("packages/bb/big-2.0.whl", "packages/aa/small-1.0.whl")


def test_lock_without_pypi_files_is_an_error() -> None:
    with pytest.raises(ValueError, match=r"no files\.pythonhosted\.org"):
        lock_probe_files('source = { git = "https://github.com/x/y" }')
