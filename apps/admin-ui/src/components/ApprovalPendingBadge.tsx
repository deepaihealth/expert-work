/**
 * Approval pending badge — Stream H.3 PR 6 (Mini-ADR H-8).
 *
 * A red dot next to the "Approvals" nav item when at least one run is
 * waiting on human approval. Stream HX-7 PR 3 moved the data source to
 * ``GET /v1/approvals?status=pending&limit=1`` — the queue's own
 * endpoint (the old ``runs?status=paused`` proxy counted paused runs,
 * which only approximated the approval queue). Still a cheap 60s poll
 * rather than an SSE channel for what is fundamentally a "is the
 * queue empty" signal.
 *
 * Decision (PR 6): tab-aware polling — when the operator hides the
 * tab we pause the timer to avoid burning quota on a stale signal,
 * resuming on visibilitychange (the same page-visibility awareness
 * ``useStatusPolling`` applies to its own poll timer).
 */
import { useEffect, useRef, useState } from "react";
import { Badge, Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import { listApprovals } from "../api/approvals";

interface ApprovalPendingBadgeProps {
  /** Label rendered inline so we can wrap "Runs" in the badge. */
  children: React.ReactNode;
  intervalMs?: number;
}

export function ApprovalPendingBadge({
  children,
  intervalMs = 60_000,
}: ApprovalPendingBadgeProps) {
  const { t } = useTranslation();
  const [count, setCount] = useState(0);
  const inFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;

    const poll = async (): Promise<void> => {
      if (inFlight.current) return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") {
        return;
      }
      inFlight.current = true;
      try {
        const result = await listApprovals({ status: "pending", limit: 1 });
        if (!cancelled) {
          setCount(result.total);
        }
      } catch {
        // Soft-fail — a transient error on a background poll shouldn't
        // surface a UI error. The badge stays at its last known value.
      } finally {
        inFlight.current = false;
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), intervalMs);
    const onVisibility = (): void => {
      if (document.visibilityState === "visible") {
        void poll();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [intervalMs]);

  if (count === 0) {
    return <>{children}</>;
  }

  const tooltipKey = count === 1 ? "approval_badge.tooltip_one" : "approval_badge.tooltip_other";
  return (
    <Tooltip title={t(tooltipKey, { count })}>
      <span data-testid="approval-pending-badge">
        <Badge
          dot
          offset={[6, 4]}
          color="#ef4444"
          aria-label={t("approval_badge.aria_label")}
        >
          {children}
        </Badge>
      </span>
    </Tooltip>
  );
}
