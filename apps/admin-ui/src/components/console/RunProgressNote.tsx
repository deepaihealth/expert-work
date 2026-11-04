/**
 * B-269 / B-270 —— 排队气泡「排队中，上一轮已进行 N 分钟」与运行中气泡
 * 「已进行 N 分钟 · 第 S 步 · 调用工具 T 次 · 子任务 W 个在跑」。
 *
 * 运行中的进度从这一轮自己的帧算,不读库。排队中先用排队帧(受理那一刻的快照),
 * 有 ``threadId`` + run id 时再定时读 run 详情刷新(条件见 ``useQueuedBehind``)。
 */
import { useMemo } from "react";
import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { SseEvent } from "../../api/sessions";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import type { ConsoleTurn } from "./types";
import {
  aheadStartedAtOf,
  progressFromEvents,
  progressLine,
  queuedRunIdOf,
  queuedText,
} from "./run_progress";
import { QUEUED_POLL_MS, useDocumentVisible, useNow, useQueuedBehind } from "./useRunProgress";

const { Text } = Typography;

function QueuedText({ aheadStartedAt }: { aheadStartedAt: string | null }) {
  const { t } = useTranslation();
  const nowMs = useNow(QUEUED_POLL_MS, useDocumentVisible());
  return <Text style={{ fontSize: 13 }}>{queuedText(aheadStartedAt, nowMs, t)}</Text>;
}

function PolledQueuedText(props: { threadId: string; runId: string; fromFrame: string | null }) {
  const { apiTenantScope } = useTenantScope();
  const ownTenant = concreteTenantScope(apiTenantScope) === undefined;
  const visible = useDocumentVisible();
  const polled = useQueuedBehind(props.threadId, props.runId, ownTenant && visible);
  const startedAt = polled === undefined ? props.fromFrame : (polled?.started_at ?? null);
  return <QueuedText aheadStartedAt={startedAt} />;
}

export function QueuedLabel({ turn, threadId }: { turn: ConsoleTurn; threadId: string | null }) {
  const events = turn.turn.events;
  const fromFrame = aheadStartedAtOf(events);
  const runId = turn.runId ?? queuedRunIdOf(events);
  if (threadId && runId) {
    return <PolledQueuedText threadId={threadId} runId={runId} fromFrame={fromFrame} />;
  }
  return <QueuedText aheadStartedAt={fromFrame} />;
}

export function RunningProgressLine({ events }: { events: readonly SseEvent[] }) {
  const { t } = useTranslation();
  const nowMs = useNow(QUEUED_POLL_MS, useDocumentVisible());
  const line = useMemo(() => {
    const progress = progressFromEvents(events, nowMs);
    return progress !== null ? progressLine(progress, t) : null;
  }, [events, nowMs, t]);
  if (line === null) return null;
  return (
    <div style={{ marginTop: 4 }}>
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="console-turn-progress">
        {line}
      </Text>
    </div>
  );
}
