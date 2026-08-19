/**
 * DetailsFrame —— 轨迹视图右侧详情 `aside` 的壳(PR-A.2 Task 9,spec §九
 * 「详情」):可拖宽手柄 + 头部(标题槽 + ✕)+ tab 条 + 内容面板。记录详情
 * (`RecordDetails`)与请求详情(`RequestDetails`)共用这一层,壳自己不认识
 * 任何数据形状。
 *
 * **受控宽度**:`width` 从上面传下来,手柄只往上抛 `onWidthChange(w)`;
 * `onWidthChange(null)` = 复位默认(由调用方换回 `DETAILS_DEFAULT_WIDTH`),
 * 双击手柄走的就是这条。
 *
 * 投影模型 / 交互参照 deepseek-harness ui-trajectory(MIT)重写。
 */
import { useRef, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import "./kind_tag.css";
import "./record_details.css";

/** 默认 420、可拖 320–720;账本那侧至少留 280(spec §九「详情」)。 */
export const DETAILS_DEFAULT_WIDTH = 420;
export const DETAILS_MIN_WIDTH = 320;
export const DETAILS_MAX_WIDTH = 720;
export const LEDGER_MIN_WIDTH = 280;
/** `← →` 一次挪多少像素。 */
export const DETAILS_RESIZE_STEP = 16;

/** `clamp(w, 320, min(720, splitWidth - 280))` —— 容器窄到上限低于下限时下限
 *  赢(详情宁可挤账本,也不能缩成一条缝)。 */
export function clampDetailsWidth(width: number, splitWidth: number): number {
  const upper = Math.min(DETAILS_MAX_WIDTH, splitWidth - LEDGER_MIN_WIDTH);
  return Math.max(DETAILS_MIN_WIDTH, Math.min(width, upper));
}

export interface DetailsFrameTab {
  key: string;
  label: string;
  testId: string;
}

export interface DetailsFrameProps {
  width: number;
  /** `null` = 复位默认宽。 */
  onWidthChange: (w: number | null) => void;
  /** 容器可用宽(账本 + 详情),夹取上限用。 */
  splitWidth: number;
  /** 类型标签 + 位置文案。 */
  header: ReactNode;
  tabs: DetailsFrameTab[];
  activeTab: string;
  onTabChange: (k: string) => void;
  onClose: () => void;
  children: ReactNode;
}

interface ResizeDrag {
  pointerId: number;
  startX: number;
  startWidth: number;
}

export function DetailsFrame(props: DetailsFrameProps) {
  const { width, onWidthChange, splitWidth, header, tabs, activeTab, onTabChange, onClose } = props;
  const { t } = useTranslation();
  const drag = useRef<ResizeDrag | null>(null);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return;
    drag.current = { pointerId: e.pointerId, startX: e.clientX, startWidth: width };
    // jsdom 没有 setPointerCapture —— 可选调用(global-constraints)。手柄本身
    // 没有点击语义,所以按下即捕获不会吃掉别人的 click。
    e.currentTarget.setPointerCapture?.(e.pointerId);
    e.preventDefault();
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const d = drag.current;
    if (d === null || d.pointerId !== e.pointerId) return;
    // 详情在右边:鼠标往左 = 变宽。
    onWidthChange(clampDetailsWidth(d.startWidth + d.startX - e.clientX, splitWidth));
  };
  const endDrag = (e: ReactPointerEvent<HTMLDivElement>): void => {
    if (drag.current?.pointerId !== e.pointerId) return;
    drag.current = null;
    e.currentTarget.releasePointerCapture?.(e.pointerId);
  };

  return (
    <aside className="ew-detail" data-testid="console-detail-aside" style={{ width }}>
      <div
        className="ew-detail__resize"
        role="separator"
        aria-orientation="vertical"
        // 可聚焦的 separator = window splitter,`aria-valuenow` 是必填
        // (axe `aria-required-attr`,PR-A.2 Task 11 的 e2e 扫描逮到)。
        aria-valuenow={Math.round(width)}
        aria-valuemin={DETAILS_MIN_WIDTH}
        aria-valuemax={Math.max(DETAILS_MIN_WIDTH, Math.min(DETAILS_MAX_WIDTH, splitWidth - LEDGER_MIN_WIDTH))}
        aria-label={t("console.detail_resize")}
        title={t("console.detail_resize")}
        tabIndex={0}
        data-testid="console-detail-resize"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={() => {
          drag.current = null;
        }}
        onDoubleClick={() => onWidthChange(null)}
        onKeyDown={(e) => {
          if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
          const direction = e.key === "ArrowLeft" ? 1 : -1;
          onWidthChange(clampDetailsWidth(width + direction * DETAILS_RESIZE_STEP, splitWidth));
          e.preventDefault();
        }}
      />
      <div className="ew-detail__header" data-testid="console-detail-header">
        <div className="ew-detail__title">{header}</div>
        <button
          type="button"
          className="ew-detail__close"
          aria-label={t("console.detail_close")}
          data-testid="console-detail-close"
          onClick={onClose}
        >
          <span aria-hidden="true">×</span>
        </button>
      </div>
      <div className="ew-detail__tabs" role="tablist">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            className={
              activeTab === tab.key ? "ew-detail__tab ew-detail__tab--active" : "ew-detail__tab"
            }
            data-testid={tab.testId}
            onClick={() => onTabChange(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="ew-detail__body" role="tabpanel">
        {props.children}
      </div>
    </aside>
  );
}

/** 概要 `dl` 的一行(标签列 + 值);记录详情与请求详情共用同一排版。 */
export function DetailRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

/** 概要里的层级跳转按钮:「Assistant Message ›」。 */
export function HierLink({
  testId,
  label,
  onClick,
}: {
  testId: string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button type="button" className="ew-detail__hier" data-testid={testId} onClick={onClick}>
      <span>{label}</span>
      <ChevronRight size={11} strokeWidth={1.75} aria-hidden />
    </button>
  );
}
