/**
 * B-269 / B-270 —— 排队气泡「排队中，上一轮已进行 N 分钟」与运行中气泡
 * 「已进行 N 分钟 · 第 S 步 · 调用工具 T 次 · 子任务 W 个在跑」。
 *
 * 读库的部分(``useRunProgress``)只在有 ``threadId`` + ``runId`` 时挂载;live 排队轮
 * 还没有 run id(``metadata`` 帧之前),它的领头开始时刻直接取自排队帧。
 */
import { Typography } from "antd";
import { useTranslation } from "react-i18next";

import type { ConsoleTurn } from "./types";
import { aheadStartedAtOf, progressLine, queuedText } from "./run_progress";
import { RUN_PROGRESS_POLL_MS, useNow, useRunProgress } from "./useRunProgress";

const { Text } = Typography;

function QueuedText({ aheadStartedAt, nowMs }: { aheadStartedAt: string | null; nowMs: number }) {
  const { t } = useTranslation();
  return <Text style={{ fontSize: 13 }}>{queuedText(aheadStartedAt, nowMs, t)}</Text>;
}

function FrameQueuedLabel({ aheadStartedAt }: { aheadStartedAt: string | null }) {
  const nowMs = useNow(RUN_PROGRESS_POLL_MS);
  return <QueuedText aheadStartedAt={aheadStartedAt} nowMs={nowMs} />;
}

function PolledQueuedLabel({ threadId, runId }: { threadId: string; runId: string }) {
  const { queuedBehind, nowMs } = useRunProgress(threadId, runId);
  return <QueuedText aheadStartedAt={queuedBehind?.started_at ?? null} nowMs={nowMs} />;
}

export function QueuedLabel({ turn, threadId }: { turn: ConsoleTurn; threadId: string | null }) {
  const fromFrame = aheadStartedAtOf(turn.turn.events);
  if (fromFrame === null && threadId && turn.runId) {
    return <PolledQueuedLabel threadId={threadId} runId={turn.runId} />;
  }
  return <FrameQueuedLabel aheadStartedAt={fromFrame} />;
}

export function RunningProgressLine({ threadId, runId }: { threadId: string; runId: string }) {
  const { t } = useTranslation();
  const { progress } = useRunProgress(threadId, runId);
  const line = progress !== null ? progressLine(progress, t) : null;
  if (line === null) return null;
  return (
    <div style={{ marginTop: 4 }}>
      <Text type="secondary" style={{ fontSize: 12 }} data-testid="console-turn-progress">
        {line}
      </Text>
    </div>
  );
}
