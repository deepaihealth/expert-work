/**
 * UserProfile — Usage pane. The user's realtime current-month token
 * counters across all agents (reuses ``getUsageTokens({userId})``).
 */
import { useCallback } from "react";
import { Alert, Card, Skeleton } from "antd";
import { useTranslation } from "react-i18next";

import { getUsageTokens } from "../../api/usage";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { formatCompact } from "../../utils/runFormat";
import { useLoad } from "./useLoad";

export function UsagePane({ userId }: { userId: string }) {
  const { t } = useTranslation();
  // Cross-tenant W3 — user detail is single-tenant semantics: concrete UUID only.
  const { apiTenantScope } = useTenantScope();
  const load = useCallback(
    () => getUsageTokens({ userId, tenantScope: concreteTenantScope(apiTenantScope) }),
    [userId, apiTenantScope],
  );
  const { data, loading, error } = useLoad(load);

  if (loading) return <Skeleton active paragraph={{ rows: 3 }} />;
  if (error !== null) return <Alert type="error" showIcon message={error} />;
  if (data === null) return null;
  const total = data.total;
  const totalTokens = total.input_tokens + total.output_tokens;

  return (
    <div data-testid="user-usage-pane">
      <Alert
        type="info"
        showIcon
        message={t("user_detail.usage_scope_note")}
        style={{ marginBottom: 12 }}
      />
      <Card size="small" title={t("user_detail.usage_month", { month: data.month })}>
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "160px 1fr",
            rowGap: 8,
            columnGap: 16,
            margin: 0,
            fontSize: 13,
          }}
        >
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_total")}</dt>
          <dd style={{ margin: 0 }} data-testid="user-usage-total">
            {formatCompact(totalTokens)}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_in_out")}</dt>
          <dd style={{ margin: 0 }}>
            {formatCompact(total.input_tokens)} / {formatCompact(total.output_tokens)}
          </dd>
          <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("user_detail.usage_by_model")}</dt>
          <dd style={{ margin: 0 }}>
            {data.by_model.length > 0
              ? data.by_model
                  .map((g) => `${g.key}: ${formatCompact(g.input_tokens + g.output_tokens)}`)
                  .join(" · ")
              : "—"}
          </dd>
        </dl>
      </Card>
    </div>
  );
}
