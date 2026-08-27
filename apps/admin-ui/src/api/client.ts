/**
 * Control-plane HTTP client — Stream H.1b.
 *
 * One ``axios`` instance with two interceptors:
 *
 *   1. request: attach the bearer token from local storage so non-React
 *      callers can mint a client without consuming :ref:`AuthContext`.
 *   2. response: normalize the API envelope ``{ success, data, error }``
 *      into either ``data`` (via :func:`unwrap`) or a thrown
 *      :class:`ApiError` carrying ``code`` + ``status``.
 *
 * Stream N: list endpoints accept ``?tenant_id=`` (UUID for a specific
 * tenant or ``"*"`` for the system_admin cross-tenant view). Callers
 * thread the resolved scope through :func:`withTenantScope` so the same
 * SDK call serves both single-tenant and cross-tenant flows.
 */
import axios, { type AxiosInstance, type AxiosRequestConfig } from "axios";

const TOKEN_STORAGE_KEY = "expert_work.admin.token";

/** UUID string for a specific tenant, ``"*"`` for cross-tenant, or
 *  ``undefined`` to fall through to the caller's home tenant.
 */
export type TenantScope = string | "*" | undefined;

export interface ApiEnvelope<T> {
  success: boolean;
  data: T | null;
  error: { code: string; message: string } | null;
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  /** The raw backend ``detail`` object when it's structured (carries extra
   *  fields like ``candidates``); ``undefined`` for plain string details. */
  readonly details?: Record<string, unknown>;
  constructor(
    message: string,
    code: string,
    status: number,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

/** 错误 → 一句可展示的话:``ApiError`` 带上错误码(它的 ``message`` 就是
 *  后端 ``detail``),普通 ``Error`` 取 message,其余 String()。给 toast 用
 *  —— 静默吞错让「下载 404」表现成「点了没反应」(2026-08-26 用户反馈)。 */
export function errMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}: ${err.message}`;
  if (err instanceof Error) return err.message;
  return String(err);
}

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setStoredToken(token: string | null): void {
  if (typeof window === "undefined") return;
  if (token) {
    window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  } else {
    window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  }
}

/** 会话过期 handler — 任意端点 401 时由响应拦截器调用(AuthContext 挂载时
 *  注册它的清会话流程,ProtectedRoute 随后带当前路径跳登录)。模块级回调而
 *  非直接 import context:client 是纯模块,不能吃 React。
 *
 *  防重/防循环不在这里做花活:
 *  - handler 第一件事是清 token,后续并发 401 撞上 ``getStoredToken()===null``
 *    直接跳过 —— 天然去抖;
 *  - 登录/刷新不走 apiClient(OIDC 直连 IdP),未登录请求本来就没有 token,
 *    同样命中上面的空 token 跳过,不可能成环。 */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function readDetail(detail: unknown, key: "code" | "message"): string | undefined {
  if (typeof detail === "object" && detail !== null && key in detail) {
    const value = (detail as Record<string, unknown>)[key];
    return typeof value === "string" ? value : undefined;
  }
  return undefined;
}

export function createClient(baseURL = ""): AxiosInstance {
  const client = axios.create({ baseURL });
  client.interceptors.request.use((config) => {
    const token = getStoredToken();
    if (token) {
      config.headers = config.headers ?? {};
      (config.headers as Record<string, string>)["Authorization"] = `Bearer ${token}`;
    }
    return config;
  });
  client.interceptors.response.use(
    (response) => response,
    (error: unknown) => {
      if (axios.isAxiosError(error)) {
        const status = error.response?.status ?? 0;
        // 401 = 会话过期(后端 401 只表示认证失败,授权失败是 403)——
        // 通知注册的清会话流程;只在还有 token 时通知(见 handler 注释)。
        if (status === 401 && unauthorizedHandler !== null && getStoredToken() !== null) {
          unauthorizedHandler();
        }
        const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail;
        const code = readDetail(detail, "code") ?? `HTTP_${status}`;
        const message = readDetail(detail, "message") ?? error.message;
        const details =
          typeof detail === "object" && detail !== null
            ? (detail as Record<string, unknown>)
            : undefined;
        throw new ApiError(message, code, status, details);
      }
      throw error;
    },
  );
  return client;
}

/** Unwrap the API envelope; throw on ``success=false``. */
export function unwrap<T>(envelope: ApiEnvelope<T>): T {
  if (!envelope.success) {
    const code = envelope.error?.code ?? "UNKNOWN";
    const message = envelope.error?.message ?? "request failed";
    throw new ApiError(message, code, 0);
  }
  if (envelope.data === null) {
    throw new ApiError("envelope.data was null", "EMPTY_ENVELOPE", 0);
  }
  return envelope.data;
}

/** Merge ``tenant_id`` into an axios ``params`` object when a scope is
 *  resolved. ``"*"`` triggers the system_admin cross-tenant aggregate
 *  view; ``undefined`` falls through to the caller's home tenant
 *  server-side. */
export function withTenantScope(
  params: Record<string, unknown>,
  tenantScope: TenantScope,
): Record<string, unknown> {
  if (tenantScope === undefined) return params;
  return { ...params, tenant_id: tenantScope };
}

/** Shared axios instance so the interceptors register once. */
export const apiClient: AxiosInstance = createClient();

export async function getJson<T>(path: string, config?: AxiosRequestConfig): Promise<T> {
  const response = await apiClient.get<ApiEnvelope<T>>(path, config);
  return unwrap(response.data);
}

export async function postJson<T>(
  path: string,
  body: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const response = await apiClient.post<ApiEnvelope<T>>(path, body, config);
  return unwrap(response.data);
}

export async function putJson<T>(
  path: string,
  body: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const response = await apiClient.put<ApiEnvelope<T>>(path, body, config);
  return unwrap(response.data);
}

export async function patchJson<T>(
  path: string,
  body: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  const response = await apiClient.patch<ApiEnvelope<T>>(path, body, config);
  return unwrap(response.data);
}
