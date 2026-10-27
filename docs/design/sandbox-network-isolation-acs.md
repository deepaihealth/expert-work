# Sandbox network isolation on ACS (B-217)

> Status: **test cluster done (2026-10-25); production groups created, switch pending the next production release.** Supersedes the
> isolation assumption in [sandbox-egress-per-agent.md](./sandbox-egress-per-agent.md),
> which relied on the docker supervisor's `internal: true` network.

## 1. Problem

The per-agent egress design (audited proxy, optional allowlist, `egress: none`)
assumed the sandbox had **no route** except to the credential-proxy. That was
true on the docker supervisor (`internal: true` network). After the move to ACS
microVMs nothing isolated the pod network:

- Every sandbox pod joined the cluster security group, the same group as the
  platform. Probed from a sandbox on 2026-10-24 (test and production identical):
  the public internet, RDS 5432, Redis 6379, the OSS internal endpoint, the k8s
  API, every platform Service (control-plane, keycloak, grafana, prometheus,
  alertmanager, tempo, langfuse-clickhouse, searxng, sandbox-manager/gateway) and
  other users' sandboxes were all reachable.
  - Held by authentication: Redis, ClickHouse, k8s Secrets, the metadata service.
  - Readable **without** authentication: Prometheus metrics, Tempo trace search,
    the Alertmanager API (can silence alerts) and Grafana search.
- `HTTPS_PROXY` was only advisory. `egress: none` meant "no proxy env". The
  allowlist could be bypassed by connecting directly, and `sandbox_egress_audit`
  saw nothing (last row 10-22).
- **A second bug:** the proxy env and token were baked in at `create()`, but one
  user's warm sandbox is shared by all their agents. A later agent inherited the
  first agent's egress settings. Runbook 53.2: an egress-on agent ran with no
  proxy env at all.

## 2. Decision

**One network layer: a dedicated ACS security group for sandboxes, deny by
default.** Plus per-exec egress env in code (§ 4).

Considered and not used:

- **Kubernetes NetworkPolicy:** ACS enforces it only for general-purpose and
  performance pods, not the `agent-sandbox` compute class.
- **ACS TrafficPolicy (Poseidon):** built for agent sandboxes, but:
  - its ACLs live in the pod's network namespace, so root inside the sandbox can
    bypass them (Alibaba's own doc says a separate security group is mandatory
    underneath);
  - it needs an extra cluster component.

  It remains the option for closing the residual OSS channel (§ 6).
- **Dual security groups** (cluster group + a "proxy" group on credential-proxy):
  ACS rejects a pod that joins both. The cluster group is an *enterprise*
  group, and enterprise and normal groups cannot be mixed (`InvalidParameter`,
  measured). Enterprise groups also cannot be referenced as a rule
  source or destination. So credential-proxy moves **entirely** to its own
  normal group.

## 3. Security groups (test cluster)

### `ew-test-sandbox` (`sg-bp1cx4xj915hke4wgyf3`)

Normal group, **intra-group isolation** (sandboxes cannot reach each other).

| Dir | Action | Proto / port | Peer | Why |
|---|---|---|---|---|
| in | allow | all | `172.16.0.0/16` | gateway / manager / kubelet reach envd. The cluster group can't be referenced (enterprise). |
| out | allow | TCP 8081 | group `ew-test-credproxy` | the audited way out (CONNECT). **Not 8080**: `/forward` trusts caller-supplied tenant/agent headers and injects that tenant's secret toward a caller-named upstream; no platform code calls it (0 allowlist rows, 0 audit rows on test). |
| out | allow | UDP+TCP 53 | `172.16.41.73/32`, `172.16.41.74/32` | cluster DNS (managed CoreDNS; pods invisible, endpoints visible) |
| out | allow | UDP+TCP 53 | `100.100.2.136/29` | Aliyun VPC DNS: the image puller resolves through `100.100.2.138` |
| out | allow | all | `172.16.41.84/32` | NAS mount target (NFSv3), the user workspace |
| out | allow | TCP 80 | `100.100.2.148/32` | in-VPC pip mirror (`mirrors.cloud.aliyuncs.com`) |
| out | allow | TCP 6443 | `172.16.41.70/32`, `172.16.41.72/32`, `172.16.88.94/32` | k8s API. The ACS in-pod agent dials `.70` at boot (`FailedToInitKubeClient` otherwise); `.72` / `88.94` are the `kubernetes` endpoints. |
| out | allow | TCP 443 | `100.103.0.0/16` | VPC image registries + `dockerauth-vpc` |
| out | allow | TCP 443 | `100.118.0.0/16` | OSS internal: registry blobs redirect here (§ 6) |
| out | **deny** | all, priority 100 | `0.0.0.0/0` | everything else |

**Why DNS and 6443 are /32s, not the VPC range:** an explicit CIDR rule
overrides intra-group isolation. With `172.16.0.0/16` on 53 and 6443, one
sandbox listening on 6443 / 53 was reachable from another (measured
2026-10-25), a cross-tenant channel. The /32s close it, at a price: if Aliyun
moves CoreDNS or the API address, new sandboxes lose DNS or fail to boot. The
pool records the allowed addresses (`expert-work.io/sg-dns-peers`,
`sg-apiserver-peers` on the SandboxSet); `smoke.sh` compares them with the
`kube-dns` / `kubernetes` endpoints and warns on drift. `.70` isn't a
`kubernetes` endpoint, so drift there only shows as sandboxes not booting.

Why the registry rules exist: ACS pulls any uncached image **through the pod's
own ENI**. Most images hit the image cache, but the platform-injected
`acs/agent-runtime` init container does not. Without these rules every new
sandbox sticks in `ImagePullBackOff`.

### `ew-test-credproxy` (`sg-bp1hjzkfjlao7lcv45py`)

The credential-proxy's only group.

| Dir | Action | Proto / port | Peer | Why |
|---|---|---|---|---|
| in | allow | TCP 8080-8081 | group `ew-test-sandbox` | sandbox CONNECT (the sandbox group's own egress only opens 8081) |
| in | allow | TCP 8080-8081 | `172.16.0.0/16` | control-plane's `/forward` calls (the cluster group can't be referenced) |
| in | allow | TCP 10250 | `172.16.0.0/16` | `kubectl logs/exec` |
| out | — | (no rules = allow all) | | RDS / Redis allowlists are IP-based; public upstreams |

### Where it lives

| What | File | How it is applied |
|---|---|---|
| Sandbox pool annotation | `infra/k8s/sandbox-test/` (kustomize over `infra/k8s/sandbox/`) | `kubectl apply -k infra/k8s/sandbox-test`. `sandboxset.yaml` stays group-free because ids differ per cluster. |
| credential-proxy annotation | `infra/k8s/overlays/test/credential-proxy-sg-patch.yaml` | carried by `release.sh test` |

**Drift guard:** `smoke.sh` → `sandbox network isolation` warns when:
- the pool template has no group;
- any running pool sandbox (claimed ones included) is not on exactly the
  template's group;
- the template's group is also on control-plane;
- cluster DNS or the API endpoints moved outside the recorded /32s.

It is warn-only (like B-57) because a control-plane rollback cannot fix it.

**Switching a pool:** changing the template does not move sandboxes users have
already claimed. They keep their old group until rebuilt (12 h warm-age cap, or
idle reap). So after `apply -k`, delete the claimed sandboxes. The workspace is on
NAS, so a rebuild loses nothing; a run executing at that moment fails its
current tool call.

## 4. Per-exec egress env (code)

- `SandboxRuntime.exec` takes `egress`; `_EgressBindingClient` fills it on
  every call, as it already did for `acquire`.
- `AgentSandboxClient` no longer bakes proxy env at `create()`. On each exec it:
  - **egress on:** mints a fresh token bound to (tenant, sandbox, agent,
    allowlist/denylist) and sets the proxy env;
  - **egress off:** sets every proxy key to `""`. envd can override env but not
    unset it; pip, urllib and requests treat empty as unset (measured in the
    sandbox). It also blanks `PIP_EXTRA_INDEX_URL`, because the public extra
    index is unreachable once isolated and pip would retry it on every install;
  - **owner unknown in this process:** treats egress as off (fail closed)
    rather than minting a token for a guessed tenant.
- The docker supervisor backend ignores the exec-time `egress`. It bakes env per
  container on an `internal: true` network.
- `_session_identity` is now recorded on every acquire (exec needs the tenant).

## 5. Measured on test (2026-10-25)

A sandbox was claimed from a temporary pool on `ew-test-sandbox` through the
platform's own `AgentSandboxClient` (egress on).

| | before | after |
|---|---|---|
| RDS, Redis | open | **blocked** |
| control-plane, keycloak, prometheus, grafana, alertmanager, tempo, clickhouse, searxng, sandbox-manager/gateway | open | **blocked** |
| public internet direct | open | **blocked** |
| another sandbox (envd, 22; a listener on 6443 / 53) | open | **blocked** (6443 / 53 were open while those rules allowed the VPC range) |
| credential-proxy 8080 (`/forward`) | open | **blocked** (8081 open) |
| a fresh sandbox boots, DNS resolves | — | ok |
| via credential-proxy: public hosts | 200 | 200 |
| via credential-proxy: internal host | 403 (`blocked_ssrf`) | 403 |
| pip (in-VPC mirror), NAS workspace write | ok | ok |
| `sandbox_egress_audit` rows for the run | — | present (allowed + blocked_ssrf) |

## 6. Residual channels (accepted 2026-10-25, user decision)

| Channel | Why open | Risk | Closing it later |
|---|---|---|---|
| OSS internal 443 (`100.118.0.0/16`) | image blobs for uncached pulls come from the shared OSS internal endpoint | code carrying an attacker's **own** Aliyun key can upload to the attacker's bucket in this region, unaudited | TrafficPolicy inside the pod (root-bypassable, but sandbox code runs as `agent`), or cache every image and drop the rule (outage risk when Aliyun bumps `agent-runtime`) |
| DNS (CoreDNS /32s + Aliyun resolvers) | name resolution | data encoded in queries | none planned |
| k8s API (3 × /32 on 6443) | ACS in-pod agent | anonymous only. No token in sandboxes (`automountServiceAccountToken: false`); Secrets → 403 | — |
| metadata `100.100.100.200:80` | not filterable by security groups | every path returns 403 (measured, incl. RAM credentials) | — |

## 7. Production

Status: **groups created 2026-10-25 (user, console); not attached to anything
yet.** The sandbox pool switches only after the release carrying #1938 is live in
production.

### Addresses (read-only probes, run by the user 2026-10-25)

| | production | test |
|---|---|---|
| VPC / range | `vpc-bp1b6yy71h4ww98cc05n1`, `172.16.0.0/12` | `172.16.0.0/16` |
| cluster group | `sg-bp17uoxec8vtpgxr23ql` | `sg-bp160370jxjrwefwkdxo` |
| cluster DNS (`kube-dns`) | `172.26.133.241`, `172.29.41.61` | `172.16.41.73`, `.74` |
| k8s API (`kubernetes`) | `172.26.133.62`, `172.29.41.40` | `172.16.41.72`, `172.16.88.94` |
| API host dialed at boot (cluster-info) | `apiserver.c153….cn-hangzhou.cs.aliyuncs.com`, which resolves in the VPC to the two `kubernetes` endpoints | `172.16.41.70` |
| NAS mount target | `172.29.41.55` | `172.16.41.84` |
| pip mirror, registry, OSS internal | same as test | |

RDS and Redis allow `172.16.0.0/12` by IP, so moving credential-proxy to its own
group keeps its database access. The user also listed `ew-prod-credproxy` on
both; that is redundant but harmless.

### Groups

- **`ew-prod-sandbox` (`sg-bp1a9oa7oi3v4s3ccquu`).** Same rules as
  `ew-test-sandbox` (§ 3), with production's DNS, API and NAS addresses above.
  Ingress is from `172.16.0.0/12`.
- **`ew-prod-credproxy` (`sg-bp14yleiog3dkd0io2ox`).** Same rules as
  `ew-test-credproxy`, with `172.16.0.0/12`.

Config:
- `infra/k8s/sandbox-prod/`, the pool annotation;
- `infra/k8s/overlays/prod/credential-proxy-sg-patch.yaml`.

### Order on release day

1. **`release.sh prod`** ships the B-217 code and moves credential-proxy to
   `ew-prod-credproxy`. That is safe before the pool moves: sandboxes still on
   the cluster group reach the proxy through its VPC rule.
   - Check that the proxy pod is Ready on `sg-bp14yleiog3dkd0io2ox`.
   - Check that the canary passes.
2. **`kubectl apply -k infra/k8s/sandbox-prod`.** The pool stays at 1 replica.
3. **Recycle the pool:** `kubectl -n default delete sandbox -l agents.kruise.io/sandbox-pool=expert-work-sandbox`.
   - A tool call running at that moment fails once.
   - Check the claimed count first and pick a quiet moment.
4. **`smoke.sh prod`** should print both `sandbox network isolation` OK lines.
5. **Probes from a warm sandbox:**
   - direct DB, Redis, monitoring and public endpoints all time out;
   - DNS, NAS, pip and the proxy work.

   Then run the canary again.

**Never apply `sandbox-prod` on the old code.** It bakes egress env at sandbox
creation. On the isolated group that means two failures:
- an egress-on agent that reuses an egress-off agent's sandbox has no network at all;
- every pip install first times out on the public extra index.

### Before release day (recommended)

Boot a temporary 1-replica pool on `ew-prod-sandbox`, as was done on test
(§ 5):
- it should boot with no `FailedToInitKubeClient` and no `ImagePullBackOff`;
- run the same reachability probe through `kubectl exec`;
- then delete the pool.

The platform never claims from that pool, so the old code is not involved and
users are unaffected. The user runs it.

**Done 2026-10-25** (two temporary sandboxes, pool `ew-sbx-sgcheck`, image
from the live pool, deleted afterwards). Results:
- booted on `sg-bp1a9oa7oi3v4s3ccquu` with no init or pull errors;
- timed out as expected: DB, Redis, control plane, Prometheus, Alertmanager,
  Grafana, Keycloak, public internet, proxy 8080, the user pool's sandbox and
  the same-group twin;
- open: NAS and the pip mirror;
- proxy 8081 timed out, as expected until release day moves credential-proxy
  onto `ew-prod-credproxy`.

The console shows `ew-prod-sandbox` set to 组内隔离.

## 8. Follow-ups (not in this change)

- **Token theft between one user's agents.** Execs of the same user's agents
  share a uid and `/proc` (`exec_view` unshares `-Urm`, no PID namespace). An
  egress-off agent can therefore read a concurrently running egress-on exec's
  `/proc/<pid>/environ` and reuse its token, which stays valid for
  `egress_token_ttl_s` (24 h). Before this change every agent on the sandbox
  simply had the first agent's token in its env, so this is narrower, not new.
  Fixes:
  - a PID namespace with its own `/proc` per exec (`unshare -p -f --mount-proc`;
    `exec_view` is shared with `runner.py`);
  - a TTL near the exec timeout. That means decoupling it from the
    supervisor drift-lock and from the warm-age cap derived from it.
- **`/forward` has no authentication.** Unreachable from sandboxes now (§ 3);
  either remove it (unused) or put a service token on it.
- **Docker supervisor (dev only)** still reuses one container per user with
  the creating agent's egress env.
- **Monitoring stack authentication** (plan item ④). Sandboxes can no longer
  reach it.
