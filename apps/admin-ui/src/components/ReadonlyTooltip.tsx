/**
 * Switched-readonly tooltip wrapper — Cross-tenant W3 (review I-2).
 *
 * antd v5 dropped the v4 disabled-child compatibility layer: browsers do not
 * dispatch mouse events on disabled form controls (and they don't bubble), so
 * ``<Tooltip><Button disabled/></Tooltip>`` silently never shows the tooltip.
 * This wrapper reproduces the v4 fix: hover lands on a wrapper span while the
 * child sits in a ``pointer-events: none`` container.
 *
 * ``on=false`` renders the children untouched (no wrapper, no tooltip) — so
 * the wrapper must sit OUTSIDE prop-injecting parents (Popconfirm/Dropdown):
 * ``<ReadonlyTooltip><Popconfirm><Button/></Popconfirm></ReadonlyTooltip>``,
 * never in between (a function component in between would swallow the
 * injected handlers — the ApprovalGate lesson).
 */
import type { ReactElement } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

interface ReadonlyTooltipProps {
  /** The switched-readonly state (``useIsTenantSwitched()``). */
  on: boolean;
  /** Block-level children (e.g. ``Upload.Dragger``); default inline-flex. */
  block?: boolean;
  children: ReactElement;
}

export function ReadonlyTooltip({
  on,
  block = false,
  children,
}: ReadonlyTooltipProps): ReactElement {
  const { t } = useTranslation();
  if (!on) return children;
  const display = block ? "block" : "inline-flex";
  return (
    <Tooltip title={t("common.tenant_switched_readonly")}>
      <span data-testid="readonly-tooltip" style={{ display, cursor: "not-allowed" }}>
        <span style={{ display, pointerEvents: "none" }}>{children}</span>
      </span>
    </Tooltip>
  );
}
