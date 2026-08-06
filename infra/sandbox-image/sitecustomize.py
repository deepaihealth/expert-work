"""Sandbox-image site hook — make stdlib ``urllib`` authenticate to the
transparent egress proxy on both HTTPS ``CONNECT`` and plain-HTTP proxying
(sandbox-egress §3.5).

Why this exists
---------------
The supervisor enables per-agent egress by injecting ``HTTPS_PROXY`` plus a
per-sandbox token (sandbox-egress §3.2). ``requests``/``httpx``/``urllib3`` send
that token as ``Proxy-Authorization`` on the ``CONNECT`` automatically — but
stdlib ``urllib.request`` does **not**: its HTTPS-over-proxy ``CONNECT`` carries
only ``Host:``, so the token is dropped and the proxy answers ``407``. A live
e2e (``tools/eval/verify_live_egress.py``) caught this; CI cannot, since it never
makes a real ``CONNECT``.

What it does
------------
When the supervisor-injected ``EXPERT_WORK_EGRESS_PROXY_AUTH`` env is present (the
base64 of ``"<token>:"`` — exactly what a ``Basic`` proxy-auth header carries),
patch ``http.client.HTTPConnection.set_tunnel`` to add ``Proxy-Authorization``
to every ``CONNECT`` that does not already set it. urllib routes proxied HTTPS
through ``set_tunnel``, so this transparently fixes it. Clients that already send
the header keep theirs (``setdefault``), so ``requests``/``httpx`` are untouched.
A second patch on ``urllib.request.ProxyHandler.proxy_open`` does the same for
plain-``http://`` requests, which never reach ``set_tunnel`` — stdlib only
sends the header itself when the proxy URL has both a user and a non-empty
password.

Loading
-------
Python auto-imports ``sitecustomize`` from the global site-packages at startup.
Both sandbox runners execute submitted code via ``python -E -P`` (the docker
runner's ``-c`` child and the cloud backend's script file — PR-C; formerly
``-I``, whose implied ``-s`` also broke ``pip install --user``): ``-E`` only
suppresses ``PYTHON*`` config env, so ``EXPERT_WORK_EGRESS_PROXY_AUTH`` is
still readable, and neither flag is ``-S``/``-s``, so the ``site`` module
still imports this module from the global site-packages and the *user* site
stays on ``sys.path``.
"""

from __future__ import annotations

import http.client
import os

_AUTH = os.environ.get("EXPERT_WORK_EGRESS_PROXY_AUTH")

if _AUTH:
    _PROXY_AUTH_HEADER = f"Basic {_AUTH}"
    _orig_set_tunnel = http.client.HTTPConnection.set_tunnel

    def _set_tunnel(self, host, port=None, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        merged = dict(headers) if headers else {}
        # Only fill what the client did not already provide — never override a
        # client's own proxy auth.
        if not any(k.lower() == "proxy-authorization" for k in merged):
            merged["Proxy-Authorization"] = _PROXY_AUTH_HEADER
        return _orig_set_tunnel(self, host, port=port, headers=merged, **kwargs)

    http.client.HTTPConnection.set_tunnel = _set_tunnel  # type: ignore[method-assign]

    import urllib.request

    _orig_proxy_open = urllib.request.ProxyHandler.proxy_open

    def _proxy_open(self, req, proxy, type):  # type: ignore[no-untyped-def]
        # Plain-HTTP requests through the proxy never reach set_tunnel, and
        # stdlib's own proxy_open only sends Proxy-Authorization when the
        # proxy URL carries BOTH a user and a password (`if user and
        # password:`) — ours is `http://<token>:@host` (empty password), so
        # stdlib drops the credential and the proxy answers 407. Mirror
        # stdlib's own header spelling; never override a client's own value.
        # For https:// URLs do_open later migrates this header into the
        # CONNECT tunnel headers — harmless overlap with the set_tunnel
        # patch above (both are setdefault-shaped).
        if not req.has_header("Proxy-authorization"):
            req.add_header("Proxy-authorization", _PROXY_AUTH_HEADER)
        return _orig_proxy_open(self, req, proxy, type)

    urllib.request.ProxyHandler.proxy_open = _proxy_open
