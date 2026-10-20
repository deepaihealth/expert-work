/**
 * DetailSection —— 记录详情「概要」里的一个分节预览(标题点一下跳对应 tab),
 * 从 `RecordDetails.tsx` 拆出来,好让那个文件留在 400 行内。
 *
 * B-202 —— 预览按固定高度裁剪,原来裁了也没有任何提示,一份长 Markdown 答复
 * 恰好裁在表格表头下面:只露一个表头,下面一片空白(渐隐盖住了半行数据),像是
 * 渲染坏了。现在:
 *  - 只有真被裁了才出渐隐(`data-truncated`),并在预览下面给「查看全文」,点了
 *    跳到对应 tab(与分节标题同一个落点);
 *  - 裁点不落在表格行中间,退到那一行的上沿;退完只剩表头,就把整张表让到下面
 *    (裁在表格上沿)。
 */
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

/** 预览区高度上限 —— 与 `record_details.css` 的 `.ew-detail__preview { max-height }`
 *  同一个数(CSS 那份兜首帧 / 没有布局的环境)。 */
export const PREVIEW_MAX_PX = 180;
/** 退到表格上沿后至少还要露出这么高,否则宁可照上限裁(表格就在最上面时,
 *  让开整张表等于什么都不显示)。 */
const MIN_VISIBLE_PX = 40;

/** 一行表格在预览区里的位置(相对预览区顶部,px)。 */
export interface PreviewRowBox {
  top: number;
  bottom: number;
  /** 所在表格的上沿。 */
  tableTop: number;
  /** 这一行之前(同一张表里)只有表头行 —— 它是第一条数据行;表头行自己也算 true。 */
  headOnlyAbove: boolean;
}

/** 内容高 `contentHeight` 的预览该裁在哪:不超上限 → null(不裁);否则返回裁剪
 *  高度,规则见文件头。纯函数,DOM 测量在 {@link measurePreview}。 */
export function previewCutOf(
  contentHeight: number,
  rows: readonly PreviewRowBox[],
  limit: number = PREVIEW_MAX_PX,
): number | null {
  if (contentHeight <= limit) return null;
  const crossing = rows.find((r) => r.top < limit && r.bottom > limit);
  if (crossing === undefined) return limit;
  const cut = crossing.headOnlyAbove ? crossing.tableTop : crossing.top;
  return cut >= MIN_VISIBLE_PX ? cut : limit;
}

/** 量预览区里每一行表格的位置。`thead` 里的行、以及表格第一条数据行都标
 *  `headOnlyAbove`。 */
function measurePreview(box: HTMLElement, body: HTMLElement): number | null {
  const origin = box.getBoundingClientRect().top;
  const rows: PreviewRowBox[] = [];
  for (const table of Array.from(body.querySelectorAll("table"))) {
    const tableTop = table.getBoundingClientRect().top - origin;
    let sawBody = false;
    for (const tr of Array.from(table.querySelectorAll("tr"))) {
      const rect = tr.getBoundingClientRect();
      const inHead = tr.parentElement?.tagName === "THEAD";
      rows.push({
        top: rect.top - origin,
        bottom: rect.bottom - origin,
        tableTop,
        headOnlyAbove: inHead || !sawBody,
      });
      if (!inHead) sawBody = true;
    }
  }
  return previewCutOf(body.scrollHeight, rows);
}

export function DetailSection({
  tab,
  label,
  onOpen,
  children,
}: {
  tab: string;
  label: string;
  onOpen: () => void;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const boxRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  /** null = 没被裁。 */
  const [cut, setCut] = useState<number | null>(null);

  useLayoutEffect(() => {
    const box = boxRef.current;
    const body = bodyRef.current;
    if (box === null || body === null) return;
    const measure = (): void => setCut(measurePreview(box, body));
    measure();
    // 内容换了、详情面板被拖宽 / 拖窄,行高都会变 —— 盯内容本身(预览区被
    // max-height 夹住,它自己的尺寸不跟着变)。
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    return () => observer.disconnect();
  }, [children]);

  return (
    <section className="ew-detail__section" data-testid={`console-detail-section-${tab}`}>
      <h3 className="ew-detail__section-h">
        <button
          type="button"
          className="ew-detail__section-btn"
          aria-label={t("console.detail_open_tab", { tab: label })}
          onClick={onOpen}
        >
          <span>{label}</span>
          <ChevronRight size={12} strokeWidth={1.75} aria-hidden />
        </button>
      </h3>
      <div
        ref={boxRef}
        className="ew-detail__preview"
        data-truncated={cut !== null ? "true" : undefined}
        style={cut !== null ? { maxHeight: cut } : undefined}
      >
        <div ref={bodyRef}>{children}</div>
      </div>
      {cut !== null && (
        <button
          type="button"
          className="ew-detail__preview-more"
          data-testid={`console-detail-section-more-${tab}`}
          onClick={onOpen}
        >
          {t("playground.view_full_text")}
        </button>
      )}
    </section>
  );
}
