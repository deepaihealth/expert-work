/**
 * 记忆行(MEMORY)的正文:召回 / 写回 N 条;B-168 B2 起写回可能是后台的 ——
 * 本轮只排了队(`detail.queued`),结果来自控制台 run 详情(`row.background`)。
 * 中栏紧凑行与账本共用这一处。
 */
import type { TFunction } from "i18next";

import type { MemoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";

export function memoryRowText(row: MemoryRow, t: TFunction): string {
  if (row.direction === "recall") return t("console.row_memory_recall", { n: row.count });
  if (row.detail.queued !== true) return t("console.row_memory_writeback", { n: row.count });
  const bg = row.background;
  if (bg === undefined) return t("console.row_memory_writeback_queued");
  if (bg.status !== "done") {
    return t("console.row_memory_writeback_bg_status", {
      status: t(`console.memory_writeback_status_${bg.status}`),
    });
  }
  const timing = {
    queued: fmtDuration(bg.queued_ms ?? 0),
    exec: fmtDuration(bg.exec_ms ?? 0),
  };
  return bg.failed === true
    ? t("console.row_memory_writeback_bg_not_written", timing)
    : t("console.row_memory_writeback_bg_done", { n: bg.written_count ?? 0, ...timing });
}
