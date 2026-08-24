import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { PromptVariablesEditor } from "../PromptVariablesEditor";
import type { AgentManifest, PromptVariableFields } from "../form_model";

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: { system_prompt: { template: "hi {{ persona }}" } },
};

function jinjaSeed(variables: PromptVariableFields[]): AgentManifest {
  return {
    ...SEED,
    spec: {
      system_prompt: { template: "hi {{ persona }}", jinja: true, variables },
    },
  };
}

describe("PromptVariablesEditor", () => {
  it("renders the jinja toggle; variable rows hidden until enabled", () => {
    render(<PromptVariablesEditor formData={SEED} onChange={vi.fn()} />);
    expect(screen.getByTestId("af-prompt-jinja")).toBeInTheDocument();
    expect(screen.queryByTestId("af-prompt-var-add")).not.toBeInTheDocument();
  });

  it("toggling jinja on emits jinja:true", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<PromptVariablesEditor formData={SEED} onChange={onChange} />);
    await user.click(screen.getByTestId("af-prompt-jinja"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.jinja).toBe(true);
  });

  it("shows the variable editor + add button when jinja is on", () => {
    render(
      <PromptVariablesEditor formData={jinjaSeed([])} onChange={vi.fn()} />,
    );
    expect(screen.getByTestId("af-prompt-var-add")).toBeInTheDocument();
  });

  it("adding a variable appends a trusted+required row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PromptVariablesEditor formData={jinjaSeed([])} onChange={onChange} />,
    );
    await user.click(screen.getByTestId("af-prompt-var-add"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables).toEqual([
      { name: "", trusted: true, required: true, description: "" },
    ]);
  });

  it("editing a variable name patches that row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PromptVariablesEditor
        formData={jinjaSeed([{ name: "", trusted: true, required: true }])}
        onChange={onChange}
      />,
    );
    await user.type(screen.getByTestId("af-prompt-var-name-0"), "p");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables?.[0].name).toBe("p");
  });

  it("toggling trusted off patches the row", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PromptVariablesEditor
        formData={jinjaSeed([
          { name: "profile", trusted: true, required: true },
        ])}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByTestId("af-prompt-var-trusted-0"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.system_prompt?.variables?.[0].trusted).toBe(false);
  });
});

describe("PromptVariablesEditor capacity (BUG-4)", () => {
  it("wraps variable rows in an internal scroll container with a count", () => {
    const variables: PromptVariableFields[] = Array.from(
      { length: 12 },
      (_, i) => ({ name: `v${i}` }),
    );
    render(
      <PromptVariablesEditor formData={jinjaSeed(variables)} onChange={vi.fn()} />,
    );
    const scroll = screen.getByTestId("af-prompt-vars-scroll");
    expect(scroll.style.overflowY).toBe("auto");
    expect(scroll.style.maxHeight).toBe("40vh");
    // 12 行都在滚动容器内,添加按钮在容器外恒可见。
    expect(scroll.querySelectorAll("[data-testid^='af-prompt-var-row-']")).toHaveLength(12);
    expect(screen.getByTestId("af-prompt-vars-count").textContent).toContain("12");
    const add = screen.getByTestId("af-prompt-var-add");
    expect(scroll.contains(add)).toBe(false);
  });
});

describe("PromptVariablesEditor add-in-scroll (终审第二轮)", () => {
  it("focuses the new row's name input after add (visible feedback)", async () => {
    const user = userEvent.setup();
    // 受控回环:onChange 后重渲染出新行,才轮到 rAF 聚焦。
    function Harness() {
      const [data, setData] = useState<unknown>(
        jinjaSeed([{ name: "a" }, { name: "b" }]),
      );
      return <PromptVariablesEditor formData={data} onChange={setData} />;
    }
    render(<Harness />);
    await user.click(screen.getByTestId("af-prompt-var-add"));
    await new Promise((r) => requestAnimationFrame(() => r(null)));
    const newInput = screen.getByTestId("af-prompt-var-name-2");
    expect(document.activeElement).toBe(newInput);
  });
});
