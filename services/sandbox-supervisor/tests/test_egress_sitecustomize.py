"""Tests for the egress urllib shim baked into the sandbox images
(``infra/sandbox-image/sitecustomize.py``, sandbox-egress §3.5).

stdlib ``urllib`` does not send the proxy token on an HTTPS ``CONNECT``, nor
(by default) on a plain-``http://`` proxied request. The shim patches TWO
globals to fix both: ``http.client.HTTPConnection.set_tunnel`` (CONNECT) and
``urllib.request.ProxyHandler.proxy_open`` (plain-HTTP) — both gated on the
supervisor-injected ``EXPERT_WORK_EGRESS_PROXY_AUTH`` env. These tests load the
shim file directly (it lives in the image build context, not an importable
package) and verify: add when env is set, no-op without it, never override a
client's own auth (for both patch points), and that a NO_PROXY-bypassed host
never gets the token. An autouse fixture snapshots and restores BOTH globals
around every test — loading the shim always patches both when the env is set,
so a test exercising only one patch point would otherwise leak the other into
the rest of the (single-process) pytest session.
"""

from __future__ import annotations

import http.client
import importlib.util
import urllib.request
from pathlib import Path

import pytest

_SHIM = Path(__file__).resolve().parents[3] / "infra" / "sandbox-image" / "sitecustomize.py"


@pytest.fixture(autouse=True)
def _restore_shim_globals() -> None:
    """Snapshot + restore both globals the shim patches, regardless of which
    one a given test exercises — loading the shim always patches both when
    ``EXPERT_WORK_EGRESS_PROXY_AUTH`` is set, so leaving either one unrestored
    leaks the patch into every other test in this pytest session."""
    orig_set_tunnel = http.client.HTTPConnection.set_tunnel
    orig_proxy_open = urllib.request.ProxyHandler.proxy_open
    yield
    http.client.HTTPConnection.set_tunnel = orig_set_tunnel  # type: ignore[method-assign]
    urllib.request.ProxyHandler.proxy_open = orig_proxy_open  # type: ignore[method-assign]


def _load_shim(monkeypatch: pytest.MonkeyPatch, auth: str | None) -> None:
    """Exec the shim file with the env set as given. It patches the global
    ``http.client.HTTPConnection.set_tunnel`` (and, with auth set,
    ``urllib.request.ProxyHandler.proxy_open``) at import time iff ``auth`` set."""
    if auth is None:
        monkeypatch.delenv("EXPERT_WORK_EGRESS_PROXY_AUTH", raising=False)
    else:
        monkeypatch.setenv("EXPERT_WORK_EGRESS_PROXY_AUTH", auth)
    spec = importlib.util.spec_from_file_location("_egress_shim_under_test", _SHIM)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_shim_adds_proxy_auth_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_shim(monkeypatch, "QUJDOg==")
    conn = http.client.HTTPConnection("proxy.local", 8081)
    conn.set_tunnel("example.com", 443)
    assert conn._tunnel_headers.get("Proxy-Authorization") == "Basic QUJDOg=="


def test_shim_noop_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    orig = http.client.HTTPConnection.set_tunnel
    _load_shim(monkeypatch, None)
    # No env → the shim must not patch set_tunnel at all.
    assert http.client.HTTPConnection.set_tunnel is orig
    conn = http.client.HTTPConnection("proxy.local", 8081)
    conn.set_tunnel("example.com", 443)
    assert "Proxy-Authorization" not in conn._tunnel_headers


def test_shim_preserves_client_supplied_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_shim(monkeypatch, "QUJDOg==")
    conn = http.client.HTTPConnection("proxy.local", 8081)
    conn.set_tunnel("example.com", 443, headers={"Proxy-Authorization": "Basic CLIENTOWN"})
    # A client that already set proxy auth keeps it; the shim only fills gaps.
    assert conn._tunnel_headers["Proxy-Authorization"] == "Basic CLIENTOWN"


def test_shim_adds_proxy_auth_to_plain_http_proxy_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # PR-C — plain-HTTP requests never reach set_tunnel; stdlib's own
    # proxy_open only sends the header when the proxy URL carries BOTH a
    # user and a password (`if user and password`), and ours is
    # `http://<token>:@host` (empty password) → 407 without this patch.
    calls: list[object] = []

    def _stub(self, req, proxy, type):
        calls.append(req)
        return None

    urllib.request.ProxyHandler.proxy_open = _stub
    _load_shim(monkeypatch, "QUJDOg==")
    req = urllib.request.Request("http://example.com/path")
    handler = urllib.request.ProxyHandler({"http": "http://proxy:8081"})
    handler.proxy_open(req, "http://proxy:8081", "http")
    assert calls, "patched proxy_open must delegate to the original"
    assert req.get_header("Proxy-authorization", "").startswith("Basic ")


def test_shim_preserves_client_auth_on_plain_http(monkeypatch: pytest.MonkeyPatch) -> None:
    urllib.request.ProxyHandler.proxy_open = lambda self, req, proxy, type: None
    _load_shim(monkeypatch, "QUJDOg==")
    req = urllib.request.Request("http://example.com/path")
    req.add_header("Proxy-authorization", "Basic client-own")
    handler = urllib.request.ProxyHandler({"http": "http://proxy:8081"})
    handler.proxy_open(req, "http://proxy:8081", "http")
    assert req.get_header("Proxy-authorization") == "Basic client-own"


def test_shim_skips_proxy_auth_for_no_proxy_bypassed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    # Follow-up to the plain-HTTP patch — stdlib's own proxy_open checks
    # proxy_bypass(req.host) BEFORE deciding whether to proxy at all, and a
    # bypassed host (e.g. NO_PROXY covering the credential-proxy itself, or
    # localhost) gets a direct connection. The header must never be stamped
    # onto that direct connection — that would leak the egress token to a
    # host that was never meant to see it.
    calls: list[object] = []

    def _stub(self, req, proxy, type):
        calls.append(req)
        return None

    urllib.request.ProxyHandler.proxy_open = _stub
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: True)
    _load_shim(monkeypatch, "QUJDOg==")
    req = urllib.request.Request("http://internal.local/path")
    handler = urllib.request.ProxyHandler({"http": "http://proxy:8081"})
    handler.proxy_open(req, "http://proxy:8081", "http")
    assert calls, "patched proxy_open must still delegate to the original"
    assert req.get_header("Proxy-authorization") is None


def test_shim_adds_proxy_auth_for_non_bypassed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def _stub(self, req, proxy, type):
        calls.append(req)
        return None

    urllib.request.ProxyHandler.proxy_open = _stub
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    _load_shim(monkeypatch, "QUJDOg==")
    req = urllib.request.Request("http://example.com/path")
    handler = urllib.request.ProxyHandler({"http": "http://proxy:8081"})
    handler.proxy_open(req, "http://proxy:8081", "http")
    assert calls, "patched proxy_open must delegate to the original"
    assert req.get_header("Proxy-authorization", "").startswith("Basic ")
