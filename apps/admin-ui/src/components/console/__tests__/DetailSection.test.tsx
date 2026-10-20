/**
 * DetailSection — 概要分节预览的裁剪(B-202):真被裁了才渐隐 + 「查看全文」,
 * 裁点不落在表格行中间,只剩表头时整张表让到下面。jsdom 没有布局,几何用
 * `data-top` / `data-bottom` 喂给 `getBoundingClientRect`,内容高喂给 `scrollHeight`。
 */
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import "../../../i18n";

import { DetailSection, PREVIEW_MAX_PX, previewCutOf, type PreviewRowBox } from "../DetailSection";

const row = (over: Partial<PreviewRowBox>): PreviewRowBox => ({
  top: 0, bottom: 0, tableTop: 0, headOnlyAbove: false, ...over,
});

describe("previewCutOf", () => {
  it("fits → not cut", () => {
    expect(previewCutOf(PREVIEW_MAX_PX, [])).toBeNull();
    expect(previewCutOf(120, [row({ top: 100, bottom: 128, tableTop: 100, headOnlyAbove: true })])).toBeNull();
  });

  it("too tall without a table in the way → cut at the limit", () => {
    expect(previewCutOf(600, [])).toBe(PREVIEW_MAX_PX);
    // 表格整张在上限以内 / 以外都不挡裁点。
    expect(
      previewCutOf(600, [
        row({ top: 20, bottom: 48, tableTop: 20, headOnlyAbove: true }),
        row({ top: 48, bottom: 76, tableTop: 20 }),
        row({ top: 200, bottom: 228, tableTop: 200, headOnlyAbove: true }),
      ]),
    ).toBe(PREVIEW_MAX_PX);
  });

  it("会话 0e878fd6 的形状:只放得下表头 → 整张表让到下面,裁在表格上沿", () => {
    const rows = [
      row({ top: 130, bottom: 158, tableTop: 130, headOnlyAbove: true }), // 表头
      row({ top: 158, bottom: 186, tableTop: 130, headOnlyAbove: true }), // 第一条数据行,跨过 180
      row({ top: 186, bottom: 214, tableTop: 130 }),
    ];
    expect(previewCutOf(600, rows)).toBe(130);
  });

  it("放得下几条数据行 → 裁在跨线那一行的上沿,不拦腰截", () => {
    const rows = [
      row({ top: 40, bottom: 68, tableTop: 40, headOnlyAbove: true }),
      row({ top: 68, bottom: 96, tableTop: 40, headOnlyAbove: true }),
      row({ top: 96, bottom: 124, tableTop: 40 }),
      row({ top: 124, bottom: 152, tableTop: 40 }),
      row({ top: 152, bottom: 190, tableTop: 40 }),
    ];
    expect(previewCutOf(600, rows)).toBe(152);
  });

  it("表格就在最上面、第一条数据行就跨线 → 照上限裁(让开整张表等于什么都不显示)", () => {
    const rows = [
      row({ top: 0, bottom: 28, tableTop: 0, headOnlyAbove: true }),
      row({ top: 28, bottom: 260, tableTop: 0, headOnlyAbove: true }),
    ];
    expect(previewCutOf(600, rows)).toBe(PREVIEW_MAX_PX);
  });
});

describe("DetailSection", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  /** 喂几何:内容高 `contentHeight`;元素自己的 `data-top` / `data-bottom` 当矩形。 */
  function fakeLayout(contentHeight: number) {
    vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(contentHeight);
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
      const el = this as HTMLElement;
      const top = Number(el.dataset?.top ?? 0);
      const bottom = Number(el.dataset?.bottom ?? 0);
      return { top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) };
    });
  }

  const answer = (
    <div>
      <p>结论段落</p>
      <table data-top="130" data-bottom="214">
        <thead>
          <tr data-top="130" data-bottom="158"><th>指标</th></tr>
        </thead>
        <tbody>
          <tr data-top="158" data-bottom="186"><td>血压</td></tr>
          <tr data-top="186" data-bottom="214"><td>血糖</td></tr>
        </tbody>
      </table>
    </div>
  );

  it("被裁:渐隐标记 + 裁在表格上沿 + 「查看全文」跳 tab", () => {
    fakeLayout(600);
    const onOpen = vi.fn();
    render(<DetailSection tab="preview" label="Preview" onOpen={onOpen}>{answer}</DetailSection>);
    const preview = screen.getByTestId("console-detail-section-preview").querySelector<HTMLElement>(
      ".ew-detail__preview",
    );
    expect(preview).toHaveAttribute("data-truncated", "true");
    expect(preview?.style.maxHeight).toBe("130px");
    const more = screen.getByTestId("console-detail-section-more-preview");
    fireEvent.click(more);
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("放得下:没有渐隐、没有「查看全文」", () => {
    fakeLayout(150);
    render(<DetailSection tab="preview" label="Preview" onOpen={vi.fn()}>{answer}</DetailSection>);
    const preview = screen.getByTestId("console-detail-section-preview").querySelector<HTMLElement>(
      ".ew-detail__preview",
    );
    expect(preview).not.toHaveAttribute("data-truncated");
    expect(preview?.style.maxHeight).toBe("");
    expect(screen.queryByTestId("console-detail-section-more-preview")).not.toBeInTheDocument();
  });

  it("渐隐只挂在被裁的预览上(样式表契约;vitest 配了 css: false,只能读盘)", () => {
    const css = readFileSync("src/components/console/record_details.css", "utf8");
    expect(css).toContain('.ew-detail__preview[data-truncated="true"]::after');
    expect(css).not.toMatch(/\.ew-detail__preview::after/);
  });
});
