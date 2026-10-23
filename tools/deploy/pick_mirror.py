"""Pick the package mirrors a release build downloads from (build-push.sh).

control-plane's image build downloads Debian packages (the builder stage's
``apt-get install git``) and every Python package in ``uv.lock``. Both default
to mirrors.aliyun.com. On 2026-10-23 that host ran at ~30 KB/s from the release
machine — Debian and PyPI alike, two machines, while Tsinghua / USTC / Huawei
Cloud served the same files at 4-18 MB/s — and two test releases died at the
apt step after ten minutes each.

The default stays first and wins whenever it is fast enough, so a normal
release builds exactly as before and keeps its layer cache (APT_MIRROR and
PYPI_MIRROR are build args: a different value is a cache miss). Only when the
default is slow are the others probed, one at a time so they do not share the
link, and the fastest wins. A PyPI mirror must also already have the newest
file in ``uv.lock``: a mirror that lags a fresh version bump would 404
mid-build.

Run inside the build's base image (build-push.sh does), so the probe takes the
same network path and CA bundle as the build itself:

    python3 pick_mirror.py apt                  # prints a Debian mirror host
    python3 pick_mirror.py pypi --lock uv.lock  # prints a PyPI prefix ("" = pypi.org)

The probe table goes to stderr; stdout carries only the chosen value.
"""

from __future__ import annotations

import argparse
import http.client
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Debian mirror hosts. The build rewrites ``deb.debian.org`` to the chosen
#: host, so each must serve both ``/debian`` and ``/debian-security`` (all of
#: these were checked on 2026-10-23). The first one is the default.
APT_HOSTS: tuple[str, ...] = (
    "mirrors.aliyun.com",
    "mirrors.ustc.edu.cn",
    "mirrors.huaweicloud.com",
    "mirrors.cloud.tencent.com",
    "mirrors.tuna.tsinghua.edu.cn",
    "deb.debian.org",
)
APT_PROBE_PATH = "/debian/dists/bookworm/main/binary-amd64/Packages.xz"

#: PyPI file prefixes: the build replaces ``https://files.pythonhosted.org/``
#: in its copy of uv.lock with the chosen prefix, so ``<prefix>packages/...``
#: must be the same file byte for byte (checked on 2026-10-23 against a
#: wheel's sha256). ``""`` means pypi.org itself. The first one is the default.
PYPI_PREFIXES: tuple[str, ...] = (
    "https://mirrors.aliyun.com/pypi/",
    "https://pypi.tuna.tsinghua.edu.cn/",
    "https://mirrors.ustc.edu.cn/pypi/",
    "https://repo.huaweicloud.com/repository/pypi/",
    "https://mirrors.cloud.tencent.com/pypi/",
    "",
)
PYPI_ORIGIN = "https://files.pythonhosted.org/"

#: The default is kept at or above this rate (bytes/s). A full uv.lock is a
#: few hundred MB; at 1 MiB/s that is a few minutes, while the cache-miss
#: cost of switching is a full re-download anyway.
GOOD_ENOUGH_BPS = 1024 * 1024
PROBE_SECONDS = 3.0
PROBE_MAX_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024
_USER_AGENT = "expert-work-release-mirror-probe/1"

_LOCK_FILE_RE = re.compile(
    r'url = "https://files\.pythonhosted\.org/(packages/[^"]+)"[^}]*?'
    r"size = (\d+)(?:[^}]*?upload-time = \"([^\"]+)\")?"
)


@dataclass(frozen=True)
class Probe:
    candidate: str
    bytes_per_second: float
    note: str = ""


def _open(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Any:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    request = urllib.request.Request(  # noqa: S310 — scheme checked above
        url, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    )
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 — scheme checked above


def measure(url: str, *, seconds: float = PROBE_SECONDS, max_bytes: int = PROBE_MAX_BYTES) -> Probe:
    """Download ``url`` for up to ``seconds`` / ``max_bytes``; rate in bytes/s.

    Any failure (refused, 404, TLS, timeout before the first byte) is a rate
    of 0 with the reason in ``note``.
    """
    started = time.monotonic()
    received = 0
    try:
        with _open(url, timeout=max(seconds, 1.0)) as response:
            while received < max_bytes and time.monotonic() - started < seconds:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                received += len(chunk)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if received == 0:
            return Probe(url, 0.0, type(exc).__name__)
    elapsed = max(time.monotonic() - started, 1e-3)
    return Probe(url, received / elapsed)


def exists(url: str) -> bool:
    """True when ``url`` answers 2xx (one byte is enough)."""
    try:
        with _open(url, timeout=10, headers={"Range": "bytes=0-0"}) as response:
            return bool(200 <= response.status < 300)
    except (OSError, ValueError, http.client.HTTPException):
        return False


def choose(
    candidates: Sequence[str],
    probe_url: Callable[[str], str],
    *,
    acceptable: Callable[[str], bool] = lambda _candidate: True,
    good_enough: float = GOOD_ENOUGH_BPS,
    measure_fn: Callable[[str], Probe] = measure,
    log: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
) -> str:
    """The first candidate if it is fast enough, else the fastest acceptable one.

    Falls back to the first candidate (today's behaviour) when nothing could
    be measured at all.
    """
    default = candidates[0]
    rates: dict[str, float] = {}
    for candidate in candidates:
        result = measure_fn(probe_url(candidate))
        ok = result.bytes_per_second > 0 and acceptable(candidate)
        rates[candidate] = result.bytes_per_second if ok else 0.0
        note = result.note or ("" if ok else "lags uv.lock")
        log(f"  {candidate or 'pypi.org':45s} {result.bytes_per_second / 1024:9.0f} KiB/s {note}")
        if candidate == default and rates[candidate] >= good_enough:
            return default
    best = max(candidates, key=lambda c: rates[c])
    if rates[best] <= 0:
        log("  no mirror answered; keeping the default")
        return default
    return best


def lock_probe_files(lock_text: str) -> tuple[str, str]:
    """(file to time, newest file) as ``packages/...`` paths from uv.lock.

    The timed file is the largest one up to 20 MB, so the probe runs long
    enough to mean something; the newest one (by upload time) is what a
    lagging mirror would be missing.
    """
    entries = [
        (m.group(1), int(m.group(2)), m.group(3) or "") for m in _LOCK_FILE_RE.finditer(lock_text)
    ]
    if not entries:
        raise ValueError("uv.lock has no files.pythonhosted.org file URLs")
    sized = [e for e in entries if e[1] <= 20 * 1024 * 1024] or entries
    timed = max(sized, key=lambda e: e[1])[0]
    newest = max(entries, key=lambda e: e[2])[0]
    return timed, newest


def _pypi_url(prefix: str, path: str) -> str:
    return (prefix or PYPI_ORIGIN) + path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=("apt", "pypi"))
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    args = parser.parse_args(argv)
    if args.kind == "apt":
        print(f"==> Debian mirror probe ({APT_PROBE_PATH})", file=sys.stderr)
        chosen = choose(APT_HOSTS, lambda host: f"http://{host}{APT_PROBE_PATH}")
    else:
        timed, newest = lock_probe_files(args.lock.read_text(encoding="utf-8"))
        timed_name, newest_name = timed.rsplit("/", 1)[-1], newest.rsplit("/", 1)[-1]
        print(f"==> PyPI mirror probe ({timed_name}; newest {newest_name})", file=sys.stderr)
        chosen = choose(
            PYPI_PREFIXES,
            lambda prefix: _pypi_url(prefix, timed),
            acceptable=lambda prefix: exists(_pypi_url(prefix, newest)),
        )
    print(f"    chosen: {chosen or 'pypi.org'}", file=sys.stderr)
    print(chosen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
