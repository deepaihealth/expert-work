# OIDC code-flow against a local Keycloak

This guide walks through standing up a single-node Keycloak in Docker
and pointing the admin-ui at it for OIDC code-flow login. The same
configuration shape works for any OIDC provider (Okta, Auth0, Azure AD,
Google Workspace) — only the issuer URL and client setup differ.

## Prerequisites

- Docker (or Podman with `alias docker=podman`)
- ``apps/admin-ui`` ``pnpm install``'d at least once
- The control-plane running locally with ``oidc_issuer`` configured to
  match Keycloak (see § Backend config below)

## 1. Start Keycloak

```bash
docker run --rm -p 8080:8080 \
  -e KEYCLOAK_ADMIN=admin \
  -e KEYCLOAK_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak:26.0 \
  start-dev
```

Keycloak's admin console is at `http://localhost:8080/admin` — log in
as `admin`/`admin`. (Production deployments use proper secrets +
external DB + TLS; the dev image above is single-node in-memory.)

## 2. Create a realm + client

In the admin console:

1. **Create a realm** — top-left dropdown → "Create Realm" → name it
   `Expert Work`.
2. **Create a client** under that realm → "Clients" → "Create client":
   - Client ID: `expert-work-admin-ui`
   - Client type: **OpenID Connect**
   - Click "Next".
3. **Capability config**:
   - Client authentication: **OFF** (public client — PKCE only).
   - Authentication flow: keep "Standard flow" checked, uncheck the
     others.
   - Click "Next".
4. **Login settings** — fill these exact values for dev:
   - Root URL: `http://localhost:5173`
   - Home URL: `http://localhost:5173`
   - Valid redirect URIs:
     - `http://localhost:5173/auth/callback`
     - `http://localhost:5173/auth/silent`
   - Valid post-logout redirect URIs: `http://localhost:5173/login`
   - Web origins: `http://localhost:5173` (so the silent-renew iframe
     can read its postMessage).
   - Click "Save".

## 3. Create a test user

1. "Users" → "Create new user" → username `alice` → save.
2. "Credentials" tab → "Set password" → `password` (uncheck
   "Temporary"). This user can now log in to the realm.

The user gets default OIDC claims (`sub`, `email`, `preferred_username`).
For Expert Work you'll want a `tenant_id` claim — wire it via Keycloak's
"Client scopes" → "user-attribute mapper" once you're past initial
verification.

## 4. Configure the admin-ui

Copy `.env.example` to `.env.local` in `apps/admin-ui/` and set:

```env
VITE_OIDC_ISSUER=http://localhost:8080/realms/Expert Work
VITE_OIDC_CLIENT_ID=expert-work-admin-ui
# Audience — Keycloak defaults audience to client_id, so we leave this
# unset. If your tenant has a separate API audience claim, set it here.
VITE_OIDC_AUDIENCE=
VITE_OIDC_SCOPES=openid profile email
# Optional — the Platform nav "Keycloak" quick-link. Set to the
# browser-reachable Keycloak origin; the page's Open button targets
# ${VITE_KEYCLOAK_BASE_URL}/admin/. Unset → the page shows a "configure" hint.
VITE_KEYCLOAK_BASE_URL=http://localhost:8080
# Optional — Langfuse UI origin for the debug console's per-turn deep link
# (compose observability profile serves langfuse-web on 3001). Unset →
# the link stays hidden. Deployed images bake it at build time via
# tools/deploy/build-push.sh --langfuse-base-url.
VITE_LANGFUSE_BASE_URL=http://localhost:3001
```

Then:

```bash
cd apps/admin-ui
pnpm dev
```

Open `http://localhost:5173/login` — the "Sign in with SSO" button
should now be visible. Clicking it redirects to Keycloak, login as
`alice`, Keycloak redirects back to `/auth/callback`, the SPA exchanges
the code (PKCE) for an id_token, and lands you on `/agents`.

## 5. Backend config (control-plane)

The control-plane already verifies inbound JWTs via OIDC discovery
(`JWTVerifier`). For the local Keycloak setup, set:

```env
EXPERT_WORK_OIDC_ISSUER=http://localhost:8080/realms/Expert Work
EXPERT_WORK_OIDC_AUDIENCE=expert-work-admin-ui
```

JWKS is fetched automatically from
`http://localhost:8080/realms/Expert Work/protocol/openid-connect/certs`.

If you see `AUTH_INVALID_AUDIENCE` from the backend, double-check that
the JWT's `aud` claim matches `EXPERT_WORK_OIDC_AUDIENCE`. Keycloak v25+
puts `account` in the audience by default — add a "audience" mapper
under the client's "Client scopes" → "dedicated" scope to inject the
expected value.

## Switching to a production IdP

For Okta / Auth0 / Azure AD / Google Workspace:

1. Register an SPA / Single-Page App client with PKCE enabled.
2. Allowed callback URLs: `https://<your-host>/auth/callback`,
   `https://<your-host>/auth/silent`.
3. Allowed logout URLs: `https://<your-host>/login`.
4. Set the `VITE_OIDC_ISSUER` to the IdP's tenant-specific authority
   (eg. `https://acme.okta.com/oauth2/default`, `https://acme.auth0.com`,
   `https://login.microsoftonline.com/<tenant-guid>/v2.0`,
   `https://accounts.google.com`).
5. If the IdP supports a separate API audience (Okta authorization
   server, Auth0 `audience`), set `VITE_OIDC_AUDIENCE`.

The admin-ui code does not change — only env. The same logic that runs
against Keycloak in dev runs against your production IdP.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Redirect loop login → callback → login | Keycloak's "Valid redirect URIs" doesn't include `/auth/callback`, or the SPA is using a different origin (eg. `127.0.0.1` vs `localhost`). |
| `AUTH_INVALID_AUDIENCE` on every `/v1/*` request | The IdP's id_token `aud` doesn't match the backend's `oidc_audience`. Set `VITE_OIDC_AUDIENCE` + an "audience" mapper in the IdP. |
| Silent renew quietly stops working | The iframe's origin is blocked by CSP, or `silent_redirect_uri` isn't registered in the IdP's allowed callbacks. Add `http://localhost:5173/auth/silent` (or your origin) to the IdP's allowed redirect URIs. |
| User lands on `/agents` but `is_system_admin` is wrong | The role binding lookup is server-side (Stream N). Confirm via ``GET /v1/me`` that the response carries the expected `is_system_admin`; if it's wrong, the binding in `role_binding_repo` needs `platform_scope=true`. |
