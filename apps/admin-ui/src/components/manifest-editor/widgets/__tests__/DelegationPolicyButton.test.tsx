/**
 * 委派增强层 3 — 配置页「生成委派策略」:
 *  - 按钮可见性(有 agentRef 且 dynamic_workers 开启才渲染);
 *  - 生成 → 只读预览 → 「插入到提示词末尾」把草稿追加进 prompt;
 *  - 失败态用 message 展示后端 detail。
 * 经 FormView 的 prompt 区渲染(按钮的宿主),App 包一层供 message 用。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../../../i18n";

import * as catalog from "../../catalog";
import { FormView } from "../../FormView";
import type { AgentManifest } from "../../form_model";
import { readSystemPrompt } from "../../form_model";
import { ApiError } from "../../../../api/client";
import { generateDelegationPolicy } from "../../../../api/agents";

vi.mock("../../../../api/agents", () => ({
  generateDelegationPolicy: vi.fn(),
}));

vi.spyOn(catalog, "loadModelCatalog").mockResolvedValue({ providers: [] });

const mockGenerate = vi.mocked(generateDelegationPolicy);

const SEED: AgentManifest = {
  apiVersion: "expert_work/v1",
  kind: "Agent",
  metadata: { name: "bot", version: "1.0.0" },
  spec: {
    model: { provider: "openai", name: "gpt-4o" },
    system_prompt: { template: "你是资深审阅者" },
    sandbox: { kind: "none" },
  },
};

const SEED_WORKERS_OFF: AgentManifest = {
  ...SEED,
  spec: { ...SEED.spec, dynamic_workers: { enabled: false } },
};

const AGENT_REF = { name: "bot", version: "1.0.0" };

function renderPrompt(
  formData: AgentManifest = SEED,
  onChange: (d: unknown) => void = vi.fn(),
  agentRef: { name: string; version: string } | undefined = AGENT_REF,
) {
  return render(
    <App>
      <FormView
        formData={formData}
        onChange={onChange}
        section="prompt"
        agentRef={agentRef}
      />
    </App>,
  );
}

beforeEach(() => {
  mockGenerate.mockReset();
});

describe("DelegationPolicyButton via FormView prompt section", () => {
  it("renders the button when agentRef is set and dynamic_workers is on (default)", () => {
    renderPrompt();
    expect(
      screen.getByTestId("af-delegation-policy-generate"),
    ).toBeInTheDocument();
  });

  it("hides the button when dynamic_workers is disabled", () => {
    renderPrompt(SEED_WORKERS_OFF);
    expect(
      screen.queryByTestId("af-delegation-policy-generate"),
    ).not.toBeInTheDocument();
  });

  it("hides the button without an agentRef (create flow)", () => {
    render(
      <App>
        <FormView formData={SEED} onChange={vi.fn()} section="prompt" />
      </App>,
    );
    expect(
      screen.queryByTestId("af-delegation-policy-generate"),
    ).not.toBeInTheDocument();
  });

  it("generates, previews the draft read-only, and appends it to the prompt on insert", async () => {
    mockGenerate.mockResolvedValue({ draft: "一、同构批量活下放…" });
    const onChange = vi.fn();
    renderPrompt(SEED, onChange);

    await userEvent.click(screen.getByTestId("af-delegation-policy-generate"));

    expect(mockGenerate).toHaveBeenCalledWith("bot", "1.0.0");
    const preview = await screen.findByTestId("af-delegation-policy-draft");
    expect(preview.textContent).toBe("一、同构批量活下放…");

    await userEvent.click(screen.getByTestId("af-delegation-policy-insert"));

    // 插入 = 追加到现有 prompt 末尾(空行分隔),不是替换。
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0];
    expect(readSystemPrompt(next)).toBe("你是资深审阅者\n\n一、同构批量活下放…");
    // 采纳后 Modal 关闭。
    await waitFor(() =>
      expect(
        screen.queryByTestId("af-delegation-policy-insert"),
      ).not.toBeVisible(),
    );
  });

  it("closes the preview without touching the prompt", async () => {
    mockGenerate.mockResolvedValue({ draft: "草稿" });
    const onChange = vi.fn();
    renderPrompt(SEED, onChange);

    await userEvent.click(screen.getByTestId("af-delegation-policy-generate"));
    await screen.findByTestId("af-delegation-policy-draft");
    await userEvent.click(screen.getByTestId("af-delegation-policy-close"));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("surfaces the backend detail in an error message on failure", async () => {
    mockGenerate.mockRejectedValue(
      new ApiError(
        "dynamic_workers is disabled for this agent",
        "DYNAMIC_WORKERS_DISABLED",
        400,
      ),
    );
    renderPrompt();

    await userEvent.click(screen.getByTestId("af-delegation-policy-generate"));

    await waitFor(() =>
      expect(
        screen.getByText(/DYNAMIC_WORKERS_DISABLED: dynamic_workers is disabled/),
      ).toBeInTheDocument(),
    );
    // 失败不弹预览。
    expect(
      screen.queryByTestId("af-delegation-policy-draft"),
    ).not.toBeInTheDocument();
  });
});
