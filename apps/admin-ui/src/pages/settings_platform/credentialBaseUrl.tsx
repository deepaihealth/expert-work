/**
 * B-308 — the optional request address on a provider credential (a platform
 * key or a per-tenant override): the list cell, the address comparison the
 * edit dialog uses, and the save errors turned into plain words.
 *
 * Contract: design §4.8 (`docs/superpowers/specs/2026-11-16-b308-credential-base-url-design.md`).
 * The backend normalises and validates the address; the page only compares
 * what was typed with what the dialog opened with.
 */
import { Space, Tag, Typography } from "antd";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";

const { Text } = Typography;

/** ``host[:port]`` of an address — the list shows only this, never the path
 *  (a relay may carry a token in it). Falls back to the raw text when the
 *  browser cannot parse it. */
export function addressHost(baseUrl: string): string {
  try {
    return new URL(baseUrl).host;
  } catch {
    return baseUrl;
  }
}

/** What the dialog compares: trimmed, without trailing ``/``; the provider's
 *  official root counts as empty (the backend stores it as "official"). */
export function comparableAddress(
  raw: string | null | undefined,
  official: string | null | undefined,
): string {
  const text = (raw ?? "").trim().replace(/\/+$/, "");
  return official && text === official.replace(/\/+$/, "") ? "" : text;
}

interface BaseUrlCellProps {
  baseUrl: string | null | undefined;
  unverified: boolean | undefined;
  /** ``null`` / absent: this provider takes no request address. */
  officialBaseUrl: string | null | undefined;
  testId: string;
}

/** The "request address" column: "official" or the host, plus the
 *  "check failed" marker of a row saved with "save anyway". */
export function BaseUrlCell({
  baseUrl,
  unverified,
  officialBaseUrl,
  testId,
}: BaseUrlCellProps) {
  const { t } = useTranslation();
  if (!baseUrl && !officialBaseUrl) {
    return <Text type="secondary">—</Text>;
  }
  return (
    <Space size={4} wrap data-testid={testId}>
      {baseUrl ? (
        <Text>{addressHost(baseUrl)}</Text>
      ) : (
        <Text type="secondary">{t("settings_platform.base_url_official")}</Text>
      )}
      {baseUrl && unverified && (
        <Tag color="warning">{t("settings_platform.base_url_unverified")}</Tag>
      )}
    </Space>
  );
}

/** A failed connectivity check (422 ``BASE_URL_CHECK_FAILED``) — the only
 *  refusal the admin may override with "save anyway". */
export interface CheckFailure {
  reason: string;
  status: number | null;
  interface: string | null;
}

export function checkFailureOf(err: unknown): CheckFailure | null {
  if (!(err instanceof ApiError) || err.code !== "BASE_URL_CHECK_FAILED") {
    return null;
  }
  const d = err.details ?? {};
  return {
    reason: typeof d.reason === "string" ? d.reason : "vendor_error",
    status: typeof d.status === "number" ? d.status : null,
    interface: typeof d.interface === "string" ? d.interface : null,
  };
}

/** One line for a failed connectivity check: the reason (with the vendor's
 *  status when there is one) and which interface was tested. */
export function checkFailureText(failure: CheckFailure, t: TFunction): string {
  const reasonKey = `settings_platform.check_reason_${failure.reason}`;
  let reason = t(reasonKey, { defaultValue: failure.reason });
  if (failure.status !== null) {
    reason = t("settings_platform.check_reason_with_status", {
      reason,
      status: failure.status,
    });
  }
  const parts = [t("settings_platform.check_failed", { reason })];
  if (failure.interface !== null) {
    const iface = t(`settings_platform.check_interface_${failure.interface}`, {
      defaultValue: failure.interface,
    });
    parts.push(t("settings_platform.check_failed_interface", { interface: iface }));
  }
  return parts.join(t("settings_platform.sentence_separator"));
}

const PLAIN_CODES = new Set([
  "BASE_URL_INVALID",
  "BASE_URL_BLOCKED_HOST",
  "BASE_URL_UNRESOLVABLE",
  "BASE_URL_NOT_SUPPORTED",
  "BASE_URL_NEEDS_NEW_KEY",
  "SECRET_NOT_FOUND",
  "BASE_URL_CHECK_UNAVAILABLE",
]);

/** The B-308 save refusals in plain words; ``null`` for any other error
 *  (the page keeps its generic toast for those). ``BASE_URL_CHECK_FAILED`` is
 *  shown through :func:`checkFailureText` instead. */
export function baseUrlErrorText(err: unknown, t: TFunction): string | null {
  if (!(err instanceof ApiError)) {
    return null;
  }
  if (PLAIN_CODES.has(err.code)) {
    return t(`settings_platform.base_url_err_${err.code}`);
  }
  if (err.code === "BASE_URL_FORMAT") {
    const d = err.details ?? {};
    const hint = typeof d.hint === "string" ? d.hint : "version_segment";
    return t(`settings_platform.base_url_err_FORMAT_${hint}`, {
      suffix: typeof d.expected_suffix === "string" ? d.expected_suffix : "",
      example: typeof d.example === "string" ? d.example : "",
      defaultValue: err.message,
    });
  }
  return null;
}
