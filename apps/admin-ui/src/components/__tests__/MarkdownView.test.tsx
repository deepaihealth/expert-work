/**
 * MarkdownView tests — assistant/history text renders as markdown, and
 * untrusted raw HTML from model output is stripped (no injection).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { MarkdownView } from "../MarkdownView";

describe("MarkdownView", () => {
  it("renders headings, bold and lists as elements", () => {
    const { container } = render(
      <MarkdownView>{"## 标题\n\n**粗体** 文本\n\n- 一\n- 二"}</MarkdownView>,
    );
    expect(container.querySelector("h2")?.textContent).toContain("标题");
    expect(container.querySelector("strong")?.textContent).toBe("粗体");
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("renders links with a safe target/rel", () => {
    render(<MarkdownView>{"[deer](https://example.com)"}</MarkdownView>);
    const link = screen.getByRole("link", { name: "deer" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noreferrer");
  });

  it("escapes raw HTML from untrusted output instead of rendering it", () => {
    const { container } = render(
      <MarkdownView>
        {'hi <b>BOLD</b> <img src=x onerror="alert(1)"> <script>alert(1)</script>'}
      </MarkdownView>,
    );
    const scope = container.querySelector(".ew-markdown");

    // 断言 ``disableParsingRawHTML`` 的**真实效果**:整段 raw HTML 被转义成
    // 文本,一个元素都不生成。
    //
    // 早先这里只断言 ``querySelector("script")`` 为 null。那条在
    // markdown-to-jsx 9.x 下恒真、杀不死变异:把选项改成 ``false`` 让它真的
    // 解析 raw HTML,9.x 也只会把 ``script``/``iframe`` 降级成 ``<span>``、
    // 把 ``img`` 的 ``onerror`` 剥掉,于是"没有 script 元素"这件事与选项开
    // 关无关。2026-08-06 升级到 9.10.1 时用变异验证发现的——测试仍绿但已经
    // 不再守护任何东西。下面三条断言在选项改成 ``false`` 时会红。
    expect(scope?.querySelectorAll("*")).toHaveLength(0);
    expect(scope?.textContent).toContain("<b>BOLD</b>");
    expect(scope?.textContent).toContain("onerror");
  });
});
