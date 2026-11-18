/**
 * B-318 —— 参数表吃的是第三方 MCP 自报的 ``input_schema``。形状不对的地方只能退化
 * (当没有),不能抛:admin-ui 没有 ErrorBoundary,一抛整个控制台白屏。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { App } from "antd";
import i18n from "../../../i18n";

import { ToolParamsTable } from "../ToolParamsTable";

let langBefore = i18n.language;

beforeEach(async () => {
  langBefore = i18n.language;
  await i18n.changeLanguage("zh-CN");
});

afterEach(async () => {
  await i18n.changeLanguage(langBefore);
});

const renderTable = (schema: unknown) =>
  render(
    <App>
      <ToolParamsTable schema={schema} testId="tp" />
    </App>,
  );

const rowOf = (name: string): HTMLElement =>
  Array.from(screen.getByTestId("tp").querySelectorAll("tr")).find((tr) =>
    tr.textContent?.startsWith(name),
  ) as HTMLElement;

describe("ToolParamsTable 对畸形 schema 不抛", () => {
  it("某个参数的 spec 是 null:这一行照列(类型、描述当没有),别的行正常", () => {
    renderTable({ properties: { x: null, y: { type: "string", description: "说明" } } });
    expect(rowOf("x").textContent).toBe("x——");
    expect(rowOf("y").textContent).toContain("string");
    expect(rowOf("y").textContent).toContain("说明");
  });

  it("required 不是数组:当没有必填,不抛", () => {
    renderTable({ properties: { x: { type: "string" } }, required: 1 });
    expect(rowOf("x").textContent).not.toContain("*");
  });

  it("required 里混着非字符串:只认字符串那几项", () => {
    renderTable({ properties: { x: {}, y: {} }, required: [1, "y", null] });
    expect(rowOf("x").textContent).not.toContain("*");
    expect(rowOf("y").textContent).toContain("*");
  });

  it("type / description 不是字符串:当没有,不抛", () => {
    renderTable({ properties: { x: { type: ["string", "null"], description: { a: 1 } } } });
    expect(rowOf("x").textContent).toBe("x——");
  });

  it("properties 不是对象:当没有参数", () => {
    renderTable({ properties: ["x"] });
    expect(screen.queryByTestId("tp")).not.toBeInTheDocument();
    expect(screen.getByText("无参数。")).toBeInTheDocument();
  });
});
