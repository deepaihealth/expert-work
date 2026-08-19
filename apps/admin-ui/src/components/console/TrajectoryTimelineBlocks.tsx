/**
 * TrajectoryTimelineBlocks —— 概览时间轴的块列表(泳道层)。单独拆出来并 `memo`:
 * 悬停竖线每次 `pointermove` 都要改位置,不隔离的话每一帧都会把整排块连同 antd
 * `Tooltip` 重建一遍。props 全是稳定引用(域投影在父层 `useMemo`),只有模型 /
 * 选中 / 悬停 / 搜索 / 选区真的变了才重渲。
 *
 * 见 .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-7-brief.md。
 */
import { memo, type CSSProperties, type JSX } from "react";
import { Tooltip } from "antd";
import { useTranslation } from "react-i18next";

import type { TimelineModel } from "./ledger_timeline";
import type { LedgerRecord } from "./ledger_types";
import { tooltipLines, TOOLTIP_DELAY_S, type DomainProjection } from "./trajectory_timeline_pointer";

export interface TrajectoryTimelineBlocksProps {
  model: TimelineModel;
  records: readonly LedgerRecord[];
  projection: DomainProjection;
  selectedIndex: number | null;
  hoveredIndex: number | null;
  searchMatches: ReadonlySet<number> | null;
  /** 与选区有交集的记录;无选区 null。 */
  focused: ReadonlySet<number> | null;
  animate: boolean;
}

export const TrajectoryTimelineBlocks = memo(function TrajectoryTimelineBlocks({
  model, records, projection, selectedIndex, hoveredIndex, searchMatches, focused, animate,
}: TrajectoryTimelineBlocksProps): JSX.Element {
  const { t } = useTranslation();
  const { fullDuration, domainStart, domainEnd, style } = projection;

  return (
    <div className="ew-traj-tl__lanes" data-animate-viewport={animate ? "true" : undefined} style={style}>
      {model.spans
        .filter((span) => span.index === selectedIndex || (span.end >= domainStart && span.start <= domainEnd))
        .map((span) => {
          const left = (span.start - model.start) / fullDuration;
          const width = (span.end - span.start) / fullDuration;
          return (
            <Tooltip
              key={span.index}
              title={tooltipLines(span, records[span.index], model, t).map((line) => <div key={line}>{line}</div>)}
              mouseEnterDelay={TOOLTIP_DELAY_S}
            >
              <span
                aria-hidden="true"
                className="ew-traj-tl__block"
                data-testid="console-lane-block"
                data-index={span.index}
                data-lane={span.lane}
                data-kind={span.kind}
                data-error={span.isError ? "true" : undefined}
                data-live={span.running ? "true" : undefined}
                data-current={span.index === selectedIndex ? "true" : undefined}
                data-hovered={span.index === hoveredIndex ? "true" : undefined}
                data-search-match={searchMatches === null ? undefined : String(searchMatches.has(span.index))}
                data-selected={focused === null ? undefined : String(focused.has(span.index))}
                style={{
                  "--traj-span-left": `${left * 100}%`,
                  "--traj-span-width": `${width * 100}%`,
                  "--traj-span-gap": `min(${width * 8}%, 1px)`,
                  "--traj-span-lane": span.lane,
                } as CSSProperties}
              />
            </Tooltip>
          );
        })}
    </div>
  );
});
