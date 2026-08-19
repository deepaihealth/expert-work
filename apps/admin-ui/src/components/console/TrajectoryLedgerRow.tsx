/**
 * TrajectoryLedgerRow —— 账本表格的单行渲染(记录行 / 折叠轮摘要行 / 折叠调用
 * 摘要行),从 `TrajectoryLedger.tsx` 拆出来只为让两个文件都留在 400 行内。
 * 纯展示:所有状态与几何都由父级算好后按 prop 传进来,这里不持有任何 state。
 * 事件槽(轮标签 / 轮轨 / 选中轨 / 请求圆点 / 类型标签)与内容列(名字 · 参数
 * → 结果)的投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 * 见 .superpowers/sdd/2026-08-19-debug-console-pr-a2-trajectory/task-8-brief.md。
 */
import type { JSX, KeyboardEvent } from "react";
import type { TFunction } from "i18next";

import type { TrajectoryRow } from "../../api/trajectory_rows";
import { fmtDuration } from "../../pages/agent_detail/playground/duration_format";
import type { DisplayRow } from "./ledger_collapse";
import type { LedgerRecord, LedgerRequest } from "./ledger_types";
import { processHeadline } from "./process_summary";

/** 内容列写成「名字 参数JSON」的类型(spec §九「内容」)—— 也是唯一会在没有
 *  结果时补「(无输出)」的几类。 */
const NAMED_KINDS: ReadonlySet<TrajectoryRow["kind"]> = new Set(["tool", "subagent", "plan"]);

/** 类型标签文案:多数直接复用中栏的 `console.traj_kind_*`,两个例外由 spec §九
 *  的标签列点名(subagent → SUBTOOL、compaction → COMPACTED)。 */
export function kindLabel(kind: TrajectoryRow["kind"], t: TFunction): string {
  if (kind === "subagent") return t("console.ledger_kind_subtool");
  if (kind === "compaction") return t("console.ledger_kind_compacted");
  return t(`console.traj_kind_${kind}`);
}

interface ContentParts {
  /** 未回放的轮的状态前缀(`正在回放…` / `回放失败…`);正常记录 null。 */
  prefix: string | null;
  /** 工具类记录的名字;其它类型 null。 */
  name: string | null;
  body: string;
  /** `body` 是「(仅工具调用)」这类替身文案而不是真正文。 */
  muted: boolean;
  result: string | null;
  /** 单行省略,所以整格挂一个全文 `title`。 */
  title: string;
}

/** 一条记录 → 内容列的几块。正文取 `record.text`(账本层已按 spec §九 折好),
 *  工具类再按第一个空格拆成 名字 / 参数。两处例外在这里补文案 —— 账本数据层
 *  是纯的、不碰 i18n,所以「没文字的 ASSISTANT」和「MEMORY 的召回 / 写回 N 条」
 *  这两句 §九 要求的正文只能在渲染层拼。 */
export function contentPartsOf(record: LedgerRecord, t: TFunction): ContentParts {
  const prefix = record.placeholder === null
    ? null
    : t(`console.ledger_placeholder_${record.placeholder}`);
  const named = NAMED_KINDS.has(record.kind);
  const cut = named ? record.text.indexOf(" ") : -1;
  const name = !named ? null : cut === -1 ? record.text : record.text.slice(0, cut);
  const toolCallOnly = record.kind === "assistant" && record.text === "" && prefix === null;
  // `LedgerRecord.kind` 按构造就是 `row.kind`(见 ledger_types),窄化到 `row`
  // 上就不用 as 断言了。MEMORY 的 `text` 在账本层是空串,正文全靠这两个既有键。
  const memory = record.row.kind === "memory" ? record.row : null;
  const body = toolCallOnly
    ? t("console.ledger_tool_call_only")
    : memory !== null
      ? t(memory.direction === "recall" ? "console.row_memory_recall" : "console.row_memory_writeback", { n: memory.count })
      : named
        ? cut === -1 ? "" : record.text.slice(cut + 1)
        : record.text;
  // 结果没到:已结束的工具类写「(无输出)」,还在跑的留空 —— 跑到一半的调用
  // 本来就还没有输出,写「无输出」是在报一个假结论。
  const result = record.resultText !== null
    ? record.resultText
    : named && !record.running
      ? t("console.ledger_no_output")
      : null;
  const head = [prefix, name, body].filter((part): part is string => part !== null && part !== "").join(" ");
  return { prefix, name, body, muted: toolCallOnly, result, title: result === null ? head : `${head} → ${result}` };
}

export interface LedgerRowProps {
  row: DisplayRow;
  /** 1-based,含首行的「加载更早」按钮行。 */
  ariaRowIndex: number;
  /** 该记录开启的 LLM 请求(没有则 undefined)。 */
  request: LedgerRequest | undefined;
  selected: boolean;
  hovered: boolean;
  /** 时间轴选区内 / 外;无选区 undefined。 */
  focus: "inside" | "outside" | undefined;
  activeTurn: boolean;
  requestActive: boolean;
  onSelect: (id: string) => void;
  onSelectRequest: (no: number) => void;
  onHover: (id: string | null) => void;
  /** 双击的落点由父级判(它才知道哪些轮 / 哪些 assistant 折得动)。 */
  onDoubleClickRecord: (record: LedgerRecord) => void;
  onToggleTurn: (turnKey: string) => void;
  onToggleOwner: (ownerId: string) => void;
  t: TFunction;
}

/** Enter / Space 同单击 —— 两种行都要,抽出来免得写两遍。 */
function activates(event: KeyboardEvent<HTMLTableRowElement>): boolean {
  return event.key === "Enter" || event.key === " ";
}

export function LedgerRow(props: LedgerRowProps): JSX.Element {
  const { row, ariaRowIndex, t } = props;

  if (row.kind !== "record") {
    const isTurn = row.kind === "turn-summary";
    const text = isTurn
      ? `${processHeadline(row.summary, t)}${row.summary.durationMs === null ? "" : ` · ${fmtDuration(row.summary.durationMs)}`}`
      : `${row.toolBreakdown} ${t("console.ledger_calls_collapsed")}`;
    const toggle = (): void => {
      if (isTurn) props.onToggleTurn(row.turnKey);
      else props.onToggleOwner(row.ownerId);
    };
    return (
      <tr
        role="row"
        tabIndex={0}
        aria-rowindex={ariaRowIndex}
        className="ew-ledger__summary"
        data-testid={isTurn ? "console-traj-turn-summary" : "console-traj-calls-summary"}
        data-turn-key={row.turnKey}
        data-owner-id={isTurn ? undefined : row.ownerId}
        data-error={isTurn && row.hasError ? "true" : undefined}
        onClick={toggle}
        onKeyDown={(event) => {
          if (!activates(event)) return;
          event.preventDefault();
          toggle();
        }}
      >
        <td className="ew-ledger__event">
          {props.activeTurn && <span className="ew-ledger__turn-rail" aria-hidden="true" />}
        </td>
        <td className="ew-ledger__content" title={text}>
          <span className="ew-ledger__ellipsis" aria-hidden="true">…</span>
          <span className="ew-ledger__summary-text">{text}</span>
        </td>
      </tr>
    );
  }

  const { record } = row;
  const { request, selected } = props;
  const parts = contentPartsOf(record, t);
  const requestLabel = request === undefined
    ? undefined
    : t("console.ledger_request_label", {
      n: request.no,
      turn: request.turnSeq + 1,
      // `step` 只在没落过帧的占位步上才是 null,那种记录也拿不到请求号;
      // 真出现就写 `?` 而不是把标签渲染成「第  步」。
      step: request.step ?? "?",
    });

  return (
    <tr
      role="row"
      tabIndex={0}
      aria-rowindex={ariaRowIndex}
      aria-selected={selected}
      className="ew-ledger__row"
      data-testid="console-traj-row"
      data-record-id={record.id}
      data-kind={record.kind}
      data-index={record.index}
      data-turn-start={record.turnStart ? "true" : undefined}
      data-turn-end={record.turnEnd ? "true" : undefined}
      data-error={record.isError ? "true" : undefined}
      data-running={record.running ? "true" : undefined}
      data-selected={selected ? "true" : undefined}
      data-hovered={props.hovered ? "true" : undefined}
      data-focus={props.focus}
      data-placeholder={record.placeholder ?? undefined}
      onClick={() => props.onSelect(record.id)}
      onDoubleClick={(event) => {
        event.preventDefault();
        props.onDoubleClickRecord(record);
      }}
      onKeyDown={(event) => {
        if (!activates(event)) return;
        event.preventDefault();
        props.onSelect(record.id);
      }}
      onMouseEnter={() => props.onHover(record.id)}
      onMouseLeave={() => props.onHover(null)}
    >
      <td className="ew-ledger__event">
        {request !== undefined && (
          <button
            type="button"
            className="ew-ledger__dot"
            data-testid="console-traj-request-dot"
            data-no={request.no}
            data-status={request.status}
            data-active={props.requestActive ? "true" : undefined}
            aria-label={requestLabel}
            aria-pressed={props.requestActive}
            title={requestLabel}
            onClick={(event) => {
              // 圆点是行里的一个独立目标:冒泡上去会把「看这次请求」变成
              // 「选中这条记录」,两个详情面板互相顶掉。
              event.stopPropagation();
              props.onSelectRequest(request.no);
            }}
            onDoubleClick={(event) => event.stopPropagation()}
          />
        )}
        {props.activeTurn && <span className="ew-ledger__turn-rail" aria-hidden="true" />}
        {selected && <span className="ew-ledger__sel-rail" aria-hidden="true" />}
        {record.turnStart && (
          <span
            className="ew-ledger__turn-label"
            data-testid="console-traj-turn-label"
            data-active={props.activeTurn ? "true" : undefined}
          >
            <span className="ew-ledger__turn-label-full">
              {t("console.ledger_turn_label", { n: record.turnSeq + 1 })}
            </span>
            <span className="ew-ledger__turn-label-compact" aria-hidden="true">
              {t("console.ledger_turn_compact", { n: record.turnSeq + 1 })}
            </span>
          </span>
        )}
        <span className="ew-ledger__kind-slot">
          <span className={`ew-kt ew-kt--${record.kind}`} data-testid="console-traj-kind">
            {kindLabel(record.kind, t)}
          </span>
        </span>
      </td>
      <td className="ew-ledger__content" data-testid="console-traj-content" title={parts.title}>
        {parts.prefix !== null && <span className="ew-ledger__ph">{parts.prefix}</span>}
        {parts.name !== null && <span className="ew-ledger__nm">{parts.name}</span>}
        {parts.body !== "" && (
          <span className={parts.muted ? "ew-ledger__muted" : parts.name === null ? "ew-ledger__txt" : "ew-ledger__args"}>
            {parts.body}
          </span>
        )}
        {parts.result !== null && (
          <>
            <span className="ew-ledger__arr" aria-hidden="true">→</span>
            <span className="ew-ledger__res" data-error={record.isError ? "true" : undefined}>
              {parts.result}
            </span>
          </>
        )}
      </td>
    </tr>
  );
}
