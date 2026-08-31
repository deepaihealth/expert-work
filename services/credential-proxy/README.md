# Credential Proxy (Stream F.5)

Outbound secret injection. A sandbox / caller sends an HTTP request to
the proxy with two headers — `X-Expert-Work-Upstream` (the real target) and
`X-Expert-Work-Secret-Ref` (the secret to inject) — and the proxy resolves the
ref, injects the real credential, and forwards upstream. The sandbox
**never sees the secret value**. STREAM-F-DESIGN § 2.5 / subsystems/11.

M0 is an aiohttp reverse proxy (Mini-ADR F-6); Envoy is M1. Secrets are
resolved through the `SecretStore` abstraction (Mini-ADR F-7) — `local_dev`
in dev, Aliyun KMS in production.

## HTTP API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/forward` | Inject the secret + forward to `X-Expert-Work-Upstream` |
| POST | `/admin/allowlist` | Register a `(tenant, agent, version, ref)` four-tuple |
| DELETE | `/admin/allowlist/{tenant}/{agent}/{version}` | Revoke an agent version's refs |
| POST | `/admin/cache/invalidate` | Drop the in-process secret cache |
| GET | `/admin/health` | Liveness |

The three `/admin` write / invalidate routes require
`Authorization: Bearer $EXPERT_WORK_CRED_PROXY_ADMIN_TOKEN`. **An unset token
closes them (503) rather than opening them** — they write the allowlist, and
an allowlist row is what lets `/forward` inject a secret toward a
caller-chosen upstream. `/admin/health` stays open for the kubelet probes.

`/forward` request headers: `X-Expert-Work-Tenant`, `X-Expert-Work-Agent`,
`X-Expert-Work-Agent-Version`, `X-Expert-Work-Session`, `X-Expert-Work-Sandbox`,
`X-Expert-Work-Secret-Ref`, `X-Expert-Work-Upstream`.

## Safety

- A `secret_ref` not on the `secret_allowlist` for the calling
  `(tenant, agent, version)` is refused with `403` — a manifest can
  only reference secrets it declared.
- Every injection attempt writes a `credential_proxy_audit` row
  recording the ref + target host + status — **never** the secret value.
- A short-TTL in-process LRU keeps secret-store read QPS down.

## Run

```bash
uvicorn-style entrypoint:  python -m credential_proxy
```

Settings come from `EXPERT_WORK_CRED_PROXY_*` env vars (see `settings.py`).
