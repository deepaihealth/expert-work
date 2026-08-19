/**
 * kind_label —— 「记录类型 → 标签文案」的单一来源(spec §九 类型表)。多数
 * 类型直接复用中栏的 `console.traj_kind_*`,两个例外由 §九 的标签列点名:
 * subagent → SUBTOOL、compaction → COMPACTED。
 *
 * 账本行、详情头部、时间轴悬停提示三处都从这里取 —— 各写各的三元时,详情
 * 头部漏了这两个例外,同一条记录在账本里叫 SUBTOOL、在详情里叫 SUBAGENT。
 */
import type { TrajectoryRow } from "../../api/trajectory_rows";

/** 只用到「按键取一句话」这一种用法;`process_summary.ts` 同款窄签名,好让
 *  拿窄 `t` 的调用方(时间轴提示)也能直接传进来。 */
type TFn = (key: string) => string;

export function kindLabel(kind: TrajectoryRow["kind"], t: TFn): string {
  if (kind === "subagent") return t("console.ledger_kind_subtool");
  if (kind === "compaction") return t("console.ledger_kind_compacted");
  return t(`console.traj_kind_${kind}`);
}
