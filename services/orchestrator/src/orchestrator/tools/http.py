"""Generic HTTP tool — Stream E.8.

LLM-callable HTTP client gated by a **per-tenant egress policy** (a
static SSRF guard, plus ``tenant_config.http_tool_denylist`` /
``http_tool_allowlist``). M0 ships without Credential Proxy (F.5) — the
tool just dispatches via httpx directly; F.5 swaps the dispatch layer
underneath without touching this surface.

Output truncation per Mini-ADR E-10 in
[STREAM-E-DESIGN](../../../../../docs/streams/STREAM-E-DESIGN.md):

- Body tail-trimmed at 20 000 chars + ``meta.truncated=true``.
- Headers serialised + capped at 4 000 chars total (drop trailing
  pairs over the budget; ``meta.headers_truncated`` flagged).
- Status code is always preserved — even a truncated body without it
  leaves the LLM with no reasoning anchor.

Per-tenant policy (denylist model — mirrors the sandbox ``NetworkSpec``):

- ``ctx.tenant_id`` is **required**; missing → :class:`ToolBlockedError`.
- **SSRF guard first**: every URL passes :func:`validate_remote_url` before the
  allow/deny lists, so a private / loopback / link-local / cloud-metadata
  target is refused even under allow-all. The check is static (no DNS);
  DNS-rebind defense is the infra egress layer's job (ADR-0009). This tool
  dispatches httpx directly (no Credential Proxy backstop), so this static
  guard is its first-line SSRF defense.
- ``http_tool_denylist`` (via ``denylist_provider``): hosts blocked even under
  allow-all, matched exact-or-subdomain. Takes precedence over the allowlist.
- ``http_tool_allowlist`` (via ``allowlist_provider``, wired to
  ``TenantConfigService.get(...).http_tool_allowlist``): empty ↔ allow all
  public hosts; non-empty ↔ strict allow-only-these, matched with
  :func:`fnmatch.fnmatch` (``"https://api.github.com/*"``).

Per Mini-ADR E-7, M0 skips Credential Proxy. Auth headers come from
the manifest ``secret_ref`` → SecretStore at agent compile time (the
caller pre-fills ``args.headers``); this tool doesn't touch
credentials itself.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import httpx

from expert_work.common.egress_token import host_in_denylist
from expert_work.common.url_validation import (
    RemoteURLError,
    validate_remote_host,
    validate_remote_url,
)
from orchestrator.tools.registry import (
    ToolBlockedError,
    ToolContext,
    ToolResult,
    ToolSpec,
)

logger = logging.getLogger(__name__)

DEFAULT_BODY_CHAR_CAP = 20_000
DEFAULT_HEADER_CHAR_CAP = 4_000
DEFAULT_TIMEOUT_S = 15.0
_ALLOWED_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_BODY_TRUNCATION_MARKER = "...[truncated]"
_HEADERS_TRUNCATION_MARKER = "...[truncated]"


#: Callable the orchestrator wires to ``TenantConfigService.get(...).http_tool_allowlist``.
#:
#: Empty result ↔ allow all public hosts (the SSRF guard + denylist are the
#: safety net); a non-empty list is strict allow-only-these.
AllowlistProvider = Callable[[UUID | None], Awaitable[Sequence[str]]]

#: Callable the orchestrator wires to ``TenantConfigService.get(...).http_tool_denylist``.
#:
#: Host entries (exact or subdomain) refused even under allow-all — takes
#: precedence over the allowlist. Empty / unset ↔ nothing denied.
DenylistProvider = Callable[[UUID | None], Awaitable[Sequence[str]]]

#: Factory for the underlying httpx ``AsyncClient``. Production wires
#: a singleton client; tests inject one preloaded with a
#: :class:`httpx.MockTransport`.
HTTPXClientFactory = Callable[[], httpx.AsyncClient]


def _default_client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)


@dataclass
class HTTPTool:
    """Tenant-scoped HTTP caller exposed to the LLM as ``http``."""

    allowlist_provider: AllowlistProvider
    #: Optional per-tenant host denylist (E.8). ``None`` ↔ nothing denied.
    denylist_provider: DenylistProvider | None = None
    client_factory: HTTPXClientFactory = field(default=_default_client_factory)
    body_char_cap: int = DEFAULT_BODY_CHAR_CAP
    header_char_cap: int = DEFAULT_HEADER_CHAR_CAP
    #: B-122 — methods this instance accepts. Defaults to all; worker builds
    #: pass ``worker_policy.READ_ONLY_HTTP_METHODS`` and the schema enum narrows with it.
    allowed_methods: frozenset[str] = _ALLOWED_METHODS

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="http",
            description=(
                "Issue an HTTP request to a public URL. Private, loopback and "
                "cloud-metadata addresses are always blocked; the tenant may "
                "additionally deny specific hosts or restrict to an allowlist. "
                "Returns status, headers, body."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": sorted(self.allowed_methods),
                    },
                    "url": {"type": "string", "format": "uri"},
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "body": {
                        "description": (
                            "Either a JSON-serialisable object (sent as JSON body) "
                            "or a string (sent verbatim)."
                        ),
                    },
                },
                "required": ["method", "url"],
            },
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        method = self._require_method(args)
        url = self._require_url(args)
        headers = self._coerce_headers(args.get("headers"))
        body_kwargs = self._coerce_body(args.get("body"))

        await self._check_policy(url, ctx.tenant_id)

        async with self.client_factory() as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                **body_kwargs,
            )

        return self._format(response)

    # ------------------------------------------------------------------
    # Input parsing / validation
    # ------------------------------------------------------------------

    def _require_method(self, args: Mapping[str, Any]) -> str:
        raw = args.get("method", "GET")
        if not isinstance(raw, str):
            msg = "'method' must be a string"
            raise ValueError(msg)
        upper = raw.strip().upper()
        if upper in _ALLOWED_METHODS and upper not in self.allowed_methods:
            # B-122 — a valid method this instance withholds (worker): say why and who does it.
            msg = (
                f"HTTP {upper} is not available to worker sub-agents: writes to "
                "outside systems stay with the orchestrator. Report what should "
                "be sent in your final message instead."
            )
            raise ValueError(msg)
        if upper not in _ALLOWED_METHODS:
            msg = f"unsupported HTTP method {raw!r}; allowed: {sorted(self.allowed_methods)}"
            raise ValueError(msg)
        return upper

    def _require_url(self, args: Mapping[str, Any]) -> str:
        raw = args.get("url")
        if not isinstance(raw, str) or not raw.strip():
            msg = "http requires a non-empty 'url' string"
            raise ValueError(msg)
        return raw.strip()

    def _coerce_headers(self, raw: object) -> dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            msg = "'headers' must be an object"
            raise ValueError(msg)
        return {str(k): str(v) for k, v in raw.items()}

    def _coerce_body(self, raw: object) -> dict[str, Any]:
        """Translate the LLM-shaped ``body`` into httpx ``request`` kwargs.

        - ``None`` → no body
        - ``str`` → ``content=raw`` (verbatim bytes)
        - any other JSON-serialisable value → ``json=raw``
        """
        if raw is None:
            return {}
        if isinstance(raw, str):
            return {"content": raw}
        if isinstance(raw, dict | list | int | float | bool):
            return {"json": raw}
        msg = "'body' must be a string or JSON-serialisable value"
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Policy — SSRF guard, then denylist, then allowlist
    # ------------------------------------------------------------------

    async def _check_policy(self, url: str, tenant_id: UUID | None) -> None:
        if tenant_id is None:
            logger.warning("http_tool.no_tenant_id url=%s", url)
            msg = "http tool requires a tenant-bound context"
            raise ToolBlockedError(msg)

        # Validate the host httpx will ACTUALLY dial, not the raw urlparse host.
        # httpx applies IDNA normalization (Unicode dot-equivalents
        # U+3002/U+FF0E/U+FF61 → '.') before it resolves, so a raw-string guard
        # would pass 'http://169。254。169。254/' while httpx dials
        # 169.254.169.254 (cloud metadata). Deriving the host from httpx.URL
        # closes that parser differential by construction.
        try:
            dialed_host = httpx.URL(url).host.rstrip(".")
        except httpx.InvalidURL as exc:
            logger.warning("http_tool.deny_invalid_url tenant_id=%s url=%s", tenant_id, url)
            msg = f"invalid URL: {exc}"
            raise ToolBlockedError(msg) from exc

        # SSRF guard first — refuse private / loopback / link-local / metadata
        # targets (and non-canonical IP literals) regardless of the lists, so
        # allow-all-public can never reach an internal address. Static, no DNS:
        # ``validate_remote_url`` checks the scheme + raw form,
        # ``validate_remote_host`` checks the exact host httpx dials.
        try:
            validate_remote_url(url)
            validate_remote_host(dialed_host)
        except RemoteURLError as exc:
            logger.warning("http_tool.deny_ssrf tenant_id=%s url=%s reason=%s", tenant_id, url, exc)
            msg = f"URL blocked by SSRF guard: {exc}"
            raise ToolBlockedError(msg) from exc

        # Denylist precedence — blocked even under allow-all or an allowlist.
        # Matched against the dialed host so a Unicode-dot spelling can't evade.
        denylist = tuple(await self.denylist_provider(tenant_id)) if self.denylist_provider else ()
        if host_in_denylist(dialed_host, denylist):
            logger.warning("http_tool.deny_denylist tenant_id=%s url=%s", tenant_id, url)
            msg = f"host {dialed_host!r} is in the tenant http_tool_denylist; blocked {url!r}"
            raise ToolBlockedError(msg)

        # Allowlist — empty ↔ allow all public hosts (SSRF guard + denylist are
        # the safety net); non-empty ↔ strict allow-only-these (URL glob).
        patterns = await self.allowlist_provider(tenant_id)
        if patterns and not any(fnmatch.fnmatch(url, pattern) for pattern in patterns):
            logger.warning(
                "http_tool.deny_not_in_allowlist tenant_id=%s url=%s",
                tenant_id,
                url,
            )
            msg = f"URL {url!r} not in http_tool_allowlist; configured patterns: {list(patterns)}"
            raise ToolBlockedError(msg)

    # ------------------------------------------------------------------
    # Response formatting + truncation
    # ------------------------------------------------------------------

    def _format(self, response: httpx.Response) -> ToolResult:
        body_text = response.text
        body_truncated = len(body_text) > self.body_char_cap
        if body_truncated:
            body_text = body_text[: self.body_char_cap] + _BODY_TRUNCATION_MARKER

        headers_text, headers_truncated = self._format_headers(response.headers)

        def _render(headers: str, body: str) -> str:
            return (
                f"HTTP {response.status_code} {response.reason_phrase}\n"
                f"--- headers ---\n{headers}\n"
                f"--- body ---\n{body}"
            )

        # Stream CM-5: a truncated response is otherwise unrecoverable
        # (the request may not be replayable) — carry the full rendering
        # so the tools node can externalize it to the workspace.
        full_content: str | None = None
        if body_truncated or headers_truncated:
            full_headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
            full_content = _render(full_headers, response.text)

        return ToolResult(
            content=_render(headers_text, body_text),
            meta={
                "status_code": response.status_code,
                "truncated": body_truncated,
                "headers_truncated": headers_truncated,
            },
            full_content=full_content,
        )

    def _format_headers(self, headers: httpx.Headers) -> tuple[str, bool]:
        rendered: list[str] = []
        running = 0
        truncated = False
        for key, value in headers.items():
            line = f"{key}: {value}"
            cost = len(line) + 1  # +1 for the joining newline
            if running + cost > self.header_char_cap:
                rendered.append(_HEADERS_TRUNCATION_MARKER)
                truncated = True
                break
            rendered.append(line)
            running += cost
        return "\n".join(rendered), truncated
