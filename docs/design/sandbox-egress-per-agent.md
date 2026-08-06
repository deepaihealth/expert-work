# Per-agent sandbox egress — let skill code reach the internet, audited not walled

> Status: **proposed** (design). Companion to
> [skill-runtime-capability.md](./skill-runtime-capability.md): this closes that
> doc's "F (external network) is a hard ceiling" claim, which was **overstated**
> — see §1.

## 1. Why this doc (and a correction)

`skill-runtime-capability.md` §3 called network-dependent skills (bucket **F**,
~12% of script-bearing skills: `videodb`, `ppt-master`'s image/TTS backends,
`mcp-builder` eval, anything calling an external API) a *hard ceiling* that
`network=none` forbids. That was inaccurate on two counts:

1. The sandbox is **not** `network=none`. It runs on the `internal: true`
   `expert-work-sandbox-egress` network (`infra/docker-compose.yml:757`) with a
   `credential-proxy` at a static IP. Sandbox code reaches the outside **only**
   through that proxy's explicit `POST /forward` API (`credential-proxy/app.py:114`),
   which injects a configured secret and forwards to a single upstream
   (`proxy.py:65,146`). It is a **credential injector**, keyed by `secret_ref`,
   not a general egress path — and skill code would have to be rewritten to its
   header convention (`X-Expert-Work-Upstream` / `X-Expert-Work-Secret-Ref`) to use it.
2. The manifest **already declares** a per-agent network config —
   `SandboxSpec.network` = `NetworkSpec{egress: none|direct|proxy, allowlist:
   [host]}` (`agent_spec.py:213-229,268`) — but it is **dead**: read nowhere
   except a wildcard lint. `acquire()` doesn't carry it; the runtime hard-codes
   one egress network for all sandboxes (`runtime_provider.py:121`).

So F is not a hard ceiling — it's an **unwired config field plus a missing
egress path**. This doc wires it.

### Guiding principle (operator-set, 2026-06-21)

> Enterprise-grade, but don't let excess restriction degrade capability or
> smoothness. Keep the **necessary** controls; for the rest prefer **audit-trail
> traceability over up-front blocking.**

Applied here: the *necessary* controls are infrastructure-protecting (block
private/metadata/link-local targets — SSRF; tenant isolation; never leak
secrets). Everything else leans **allow + audit**: when an agent has egress,
default to reaching **any public host**, log every connection, and make a host
allowlist an **optional** hardening — never the default gate.

## 2. What "F runs" actually requires

For `requests.get("https://api.example.com")` inside `exec_python` to just work:

- **A routable egress path** the sandbox can use transparently (no skill code
  changes). Today there is none — `internal: true` gives no route off-network.
- **TLS preserved** — almost all targets are HTTPS. We must NOT MITM (cert pain,
  and we don't want to read bodies). So the egress path tunnels HTTPS via
  `CONNECT` and sees only `host:port`, never plaintext — which is exactly the
  right audit granularity (host + bytes, not content).
- **SSRF safety** at that path: resolve the target, refuse private/loopback/
  link-local/metadata IPs, and **pin the resolved IP** for the connection (this
  also closes the DNS-rebind gap `url_validation.py` deliberately punts to the
  infra layer, ADR-0009).
- **Identity** the proxy can trust for audit + optional allowlist. The current
  `/forward` trusts sandbox-**self-reported** `X-Expert-Work-*` headers
  (`app.py:142`) — fine behind the internal net, but not trustworthy enough to
  attribute or scope per-agent. We inject identity from **outside** the sandbox.

## 3. Design

### 3.1 A transparent egress proxy (the chokepoint)

Add a forward proxy that speaks standard `HTTP_PROXY`/`HTTPS_PROXY` semantics:

- **`CONNECT host:port`** for HTTPS (tunnel; proxy never decrypts) and plain
  HTTP forwarding for `http://`.
- On each connection: resolve host → check every resolved IP with the existing
  `validate_remote_url` block logic (`expert-work-common/url_validation.py:33-41`:
  private, loopback, link-local incl. `169.254.169.254`, reserved, multicast,
  unspecified, plus non-canonical IP literals) → **connect to the pinned IP** (no
  re-resolution → no rebind).
- **Audit every connection** (the whole point): one row per connect with
  `tenant_id, agent_name, agent_version, sandbox_id, target_host, target_port,
  bytes_up, bytes_down, duration_ms, verdict ∈ {allowed, blocked_ssrf,
  blocked_allowlist}`. Model on `credential_proxy_audit`
  (`migrations/.../0013_credential_proxy.py:66`) + the MCP-traffic-audit
  "record length, never content" rule (`builder.py:1551-1569`). TLS bodies are
  opaque by design, so we log **metadata + volume**, not payloads.
- **Optional per-agent host allowlist** (opt-in hardening): if the agent's
  `NetworkSpec.allowlist` is non-empty, only those hosts pass; empty = any
  public host (audited). Never required.

**Where it lives.** Reuse the **credential-proxy service process** — it is
already dual-homed on the egress network with DB access for audit + allowlist.
Add a second listener (the CONNECT/forward proxy) alongside the existing
`/forward` secret-injector. One service, two roles; no new container, no new
network. (Alternative: a standalone `egress-proxy` service — cleaner separation
but more ops; deferred unless the credential-proxy grows unwieldy.)

**No MITM, no content DLP.** Because HTTPS is tunneled, there is no content-level
DLP on egress — and we deliberately don't add cert-MITM for it (cert pain, and
it fights the principle: don't over-engineer blocking). Egress "DLP" = **volume/host
anomaly detection off the audit trail** (e.g. a sandbox shipping 500 MB to a
never-seen host), not inline payload inspection. Stated plainly so it isn't
mistaken for body inspection.

### 3.2 Trusted identity — per-sandbox proxy token

When egress is enabled for an acquire, the supervisor mints a short per-sandbox
token bound to `(tenant_id, agent_name, agent_version, sandbox_id)` and injects
it **outside** the sandbox as the proxy URL's `Proxy-Authorization`
(`HTTPS_PROXY=http://<token>@egress-proxy.internal:PORT`). Standard HTTP clients
send it automatically. The proxy validates the token → resolves identity →
applies allowlist + attributes audit. The sandbox can read its own env, but the
token only grants what *that* agent already may do, so leakage to skill code is
not a privilege escalation. This closes the "self-reported header" gap (§2) for
the new path without touching the existing `/forward`.

### 3.3 Wire `NetworkSpec` end-to-end (the dead field)

Revised semantics (collapse `direct` away — all egress goes through the audited
chokepoint; no raw un-audited path):

| `egress` | meaning |
| --- | --- |
| `none` | fully isolated — today's behavior (internal net, proxy-only via `/forward`). |
| `proxy` | egress **on** through the transparent proxy: any public host + full audit; `allowlist` (if set) restricts, else all-public. |

Plumbing (the 6 known touchpoints, from the as-built trace):

1. `AcquireRequest` += `egress: Literal["none","proxy"]` + `allowlist: list[str]`
   (`sandbox-supervisor/schemas.py:53`).
2. `SupervisorClient.acquire` Protocol + `HTTPSupervisorClient.acquire` HTTP body
   + `RecordingSupervisorClient` (`orchestrator/tools/sandbox.py:87,145,263`) —
   thread it exactly like `image_variant`/`seed_files`.
3. `run_in_sandbox()` / tool instances read it from `SandboxSpec.network`
   (`sandbox.py:302,412`) — bind at build time, same pattern as `image_variant`.
4. `SandboxRecord` += the egress choice; `_new_record` fills it
   (`supervisor.py:642`).
5. **Parameterize `docker_run_argv(network=…)`** to override the frozen
   provider default per call, and inject `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`
   env + the proxy token when `egress=="proxy"` (`runtime_provider.py:81-128`,
   `supervisor.py:674`). `egress=="none"` keeps the internal-only network.
6. **Pool interaction** (`pool.py:220`): pooled containers bake one network/env;
   either key the pool by egress policy or skip the pool for `egress=="proxy"`
   acquires (simplest: egress sandboxes are non-pooled). Note in the PR.

### 3.4 Default posture — the one real decision

`NetworkSpec.egress` currently defaults to `"proxy"`. Two stances:

- **Default `none` (opt-in egress).** An agent reaches the internet only when its
  manifest says so. The *decision to allow outbound at all* is explicit per
  agent — reasonable for a multi-tenant platform where a compromised/injected
  skill with egress can exfiltrate tenant data. Once on, it's a single smooth
  toggle (no forced host list) + full audit. **Recommended.**
- **Default `proxy` (egress on, audited).** Maximum smoothness; every agent can
  call the internet, all of it logged. Matches the principle most aggressively
  but widens the default exfil surface across all tenants.

**Decided (operator, 2026-06-21): default `proxy` — egress on, audited.** Every
agent can reach the public internet out of the box; every connection is logged
(host/bytes/duration). This is the smoothest stance and the fullest expression
of "audit over blocking": no per-agent toggle stands between a skill and the API
it needs. The trade accepted is a platform-wide default egress (exfil) surface,
governed by the *necessary* controls below — SSRF block + IP pin + tenant-scoped
identity + full audit + volume-anomaly detection — rather than by walling egress
off. An operator who wants a tighter posture sets `egress: none` per agent (or a
host `allowlist`); the platform does not force it.

### 3.5 stdlib `urllib` CONNECT auth — the sitecustomize shim (live finding)

§3.2 assumes "standard HTTP clients send the proxy token automatically." The
live e2e (`verify_live_egress.py`) proved that is **not** universally true:

| client | sends `Proxy-Authorization` on the `CONNECT`? |
| --- | --- |
| `requests` / `httpx` / `urllib3` | yes (from the proxy URL userinfo) |
| stdlib `urllib.request` | **no** — the HTTPS `CONNECT` carries only `Host:` |

So an egress-enabled sandbox whose skill uses bare `urllib` (the zero-dependency
baseline; Anthropic's own catalog code uses it) loses the token → the proxy
returns `407` → egress silently fails. CI could never catch this — only a real
`CONNECT` over the wire shows the missing header.

Fix, transparent to skill code (capability must not weaken):

1. The supervisor also injects `EXPERT_WORK_EGRESS_PROXY_AUTH = base64("<token>:")`
   (the exact bytes a `Basic` proxy-auth header would carry) alongside
   `HTTPS_PROXY`.
2. A `sitecustomize.py` baked into the sandbox images monkeypatches
   `http.client.HTTPConnection.set_tunnel` to add `Proxy-Authorization: Basic
   <EXPERT_WORK_EGRESS_PROXY_AUTH>` to every `CONNECT` when that env is present.
   It also patches `urllib.request.ProxyHandler.proxy_open` for plain-HTTP
   proxied requests (both use setdefault-style injection to avoid overwriting
   existing headers). Python auto-imports `sitecustomize` from the **global**
   site-packages at interpreter startup — and crucially this still fires under
   the runner's `python -I -c` (isolated mode is `-E -s`, *not* `-S`, so `site`
   still runs; `-E` ignores only `PYTHON*` env, so the auth env is still read).
   `requests`/`httpx`/`urllib3` already work and are unaffected (the patch only
   fills a header they already set).

No skill change, no proxy change (the proxy already validates the standard
`Proxy-Authorization`); the shim only makes urllib behave like every other
client.

### 3.6 Plain-HTTP egress (`http://`) — shipped

The proxy now handles plain-HTTP absolute-form requests (`GET http://host/path`)
in addition to `CONNECT`, behind the **same** auth + allowlist + SSRF-pin gate
(`_secure_connect`). Per request it rewrites the request line to origin-form,
drops the proxy/connection hop-by-hop headers, rebuilds `Host` from the URL
authority (so it can't disagree with the pinned target), forces `Connection:
close` — **one request per connection**, so a pipelined second request can't
smuggle past the per-request host check — and relays the body + response, one
audit row per request. For `http://` the proxy does see the bytes (no TLS), but
it neither inspects nor logs the payload (host/port + volumes only, as for
HTTPS). `HTTP_PROXY` was already injected into the sandbox, so no supervisor
change was needed.

## 4. Security accounting (necessary vs. dropped)

| Control | Keep? | Why |
| --- | --- | --- |
| Block private/loopback/link-local/metadata IPs (+ pin resolved IP) | **necessary** | protects infra + cloud metadata creds; reuse `validate_remote_url`. |
| Tenant isolation / per-sandbox identity token | **necessary** | attribute + scope egress; no cross-tenant. |
| Never log secrets / payloads | **necessary** | audit records host + volume only. |
| Full per-connection audit + volume-anomaly detection | **necessary (the trade)** | this is how we trade up-front blocking for traceability. |
| Mandatory per-agent host allowlist | **dropped → optional** | over-blocking; breaks F skills that hit many hosts. Opt-in hardening only. |
| Inline content DLP on egress | **not done** | HTTPS tunneled (no MITM); volume/host anomaly off audit instead. |
| Egress on by default | **yes — decided** (§3.4) | smoothest; governed by audit + SSRF, not a wall. Tighten per agent. |

## 5. Scope, phases, verification

- **Phase 1 — egress path + wiring. ✅ shipped (#745 1a / #746 1b).** Transparent
  CONNECT proxy in the credential-proxy process, SSRF block + resolved-IP pin,
  per-sandbox token, per-connection audit; `NetworkSpec` wired end-to-end (§3.3)
  via a `supervisor_client` wrapper (no per-tool threading). Sandboxes stay on
  the `internal:true` net and reach the proxy (no new network needed); the
  supervisor injects `HTTPS_PROXY` + the token when egress is on. Default
  `proxy` (§3.4).
- **Phase 2 — optional allowlist. ✅ shipped.** The per-agent
  `NetworkSpec.allowlist` is embedded in the signed egress token (tamper-proof,
  no new store) and enforced at the proxy (exact or subdomain match) →
  `blocked_allowlist` 403 + audit. Empty = any public host (audited). **Anomaly
  detection is intentionally not code here** — the `sandbox_egress_audit` table
  already carries host + byte volumes per connection, so volume/novel-host
  anomaly is a Grafana/SQL query over that table (ops surface, per "ops →
  Grafana not admin-ui"), not a new engine.
- **Phase 3 — admin-ui.** Per-agent egress toggle in the manifest editor; egress
  audit view (reuse the existing traffic-audit page + MCP-badge pattern).

**Verification.** Unit: SSRF block table (private/metadata/non-canonical IP →
`blocked_ssrf`), allowlist gate, token→identity. Integration (real Docker): an
`egress: proxy` acquire → `exec_python` does `requests.get("https://<mock-
upstream>")` and succeeds; a `requests.get` to `169.254.169.254` is blocked +
audited; `egress: none` still has no route. Live E2E (per "CI green ≠ live"):
import a real F-bucket skill, bind it to an egress agent, run it end-to-end, and
confirm the audit trail shows the outbound hosts + volumes.

## 6. Decision

1. Wire the dead `NetworkSpec` and add a **transparent egress proxy** so F-bucket
   skills run without code changes — TLS tunneled, host+volume audited.
2. **Audit over blocking**: when egress is on, allow any **public** host (block
   only the necessary infra/SSRF targets) + log every connection; host allowlist
   is **optional** hardening.
3. **Default `egress: proxy` (on, audited)** — decided 2026-06-21; smoothest
   stance, governed by SSRF block + IP pin + tenant identity + audit + anomaly
   rather than a wall. An operator tightens per agent (`none` / `allowlist`).
   Identity via per-sandbox token, not self-reported headers.
4. No cert-MITM / inline content DLP; egress safety = SSRF block + IP pin + audit
   + volume anomaly.
