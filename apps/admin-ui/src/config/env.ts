/**
 * Build-time env config — Stream H.3 PR 6.
 *
 * Centralises ``import.meta.env`` reads outside auth/oidc.ts. The
 * indirection exists so:
 *
 *   - Vite's constant-folding still applies (bracket access on the
 *     ``Record<string, string|undefined>`` view of ``import.meta.env``
 *     keeps the substitution working).
 *   - Tests can stub values via ``vi.stubEnv`` and re-read fresh.
 *   - Trailing-slash normalisation lives in one place so every UI
 *     surface that links into Langfuse builds the same URL shape.
 */
function readEnv(key: string): string | undefined {
  const env = import.meta.env as Record<string, string | undefined>;
  const value = env[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

/** Langfuse base URL — e.g. ``https://langfuse.internal``. When unset,
 *  ``buildLangfuseTraceUrl`` returns ``null`` and the debug console's
 *  ``RecordDetails`` hides the "Open in Langfuse" link entirely
 *  (system_admin only). */
export function readLangfuseBaseUrl(): string | undefined {
  const raw = readEnv("VITE_LANGFUSE_BASE_URL");
  if (raw === undefined) return undefined;
  return raw.replace(/\/+$/, "");
}

/** Build a Langfuse trace URL. Returns ``null`` when the base URL
 *  isn't configured or the trace_id is missing — callers should
 *  hide the link in that case rather than rendering a dead anchor. */
export function buildLangfuseTraceUrl(traceId: string | null | undefined): string | null {
  if (traceId === null || traceId === undefined || traceId.length === 0) {
    return null;
  }
  const base = readLangfuseBaseUrl();
  if (base === undefined) return null;
  return `${base}/trace/${encodeURIComponent(traceId)}`;
}

/** Grafana base URL — the self-hosted metrics/log dashboard. Unset → the
 *  Observability hub shows the card disabled with a "configure" hint. */
export function readGrafanaBaseUrl(): string | undefined {
  const raw = readEnv("VITE_GRAFANA_BASE_URL");
  return raw === undefined ? undefined : raw.replace(/\/+$/, "");
}

/** Tempo base URL — the self-hosted distributed-trace store (infra spans).
 *  Unset → the Observability hub shows the card disabled. */
export function readTempoBaseUrl(): string | undefined {
  const raw = readEnv("VITE_TEMPO_BASE_URL");
  return raw === undefined ? undefined : raw.replace(/\/+$/, "");
}

/** Keycloak base URL — the self-hosted IAM admin-console origin (e.g.
 *  ``http://localhost:8080``). The platform Keycloak page external-links to
 *  ``${base}/admin/`` so operators can manage realm users / set member +
 *  first-admin passwords. Unset → the page shows a "configure" hint naming the
 *  env var rather than a dead link. */
export function readKeycloakBaseUrl(): string | undefined {
  const raw = readEnv("VITE_KEYCLOAK_BASE_URL");
  return raw === undefined ? undefined : raw.replace(/\/+$/, "");
}

export type BuildEnv = "test" | "prod";

export interface BuildInfo {
  /** git 短 sha —— 与发版记录 PR、rollback.sh 的 tag 一一对应。 */
  version?: string;
  /** 发版脚本自己的 env_name;不是猜域名猜出来的。 */
  env?: BuildEnv;
  /** 构建时刻,ISO 8601 UTC。 */
  builtAt?: string;
}

/** 「关于」弹窗的数据源。三个值由 tools/deploy/build-push.sh 在发版时烤进
 *  admin-ui 镜像(``VITE_APP_VERSION`` / ``VITE_APP_ENV`` / ``VITE_BUILD_TIME``);
 *  三个镜像同 sha 由 smoke 校验,所以这里的 sha 就是后端在跑的 sha。本地 dev
 *  三个都空 → 弹窗显示「本地开发」,不装作是线上。 */
export function readBuildInfo(): BuildInfo {
  const env = readEnv("VITE_APP_ENV");
  return {
    version: readEnv("VITE_APP_VERSION"),
    env: env === "test" || env === "prod" ? env : undefined,
    builtAt: readEnv("VITE_BUILD_TIME"),
  };
}
