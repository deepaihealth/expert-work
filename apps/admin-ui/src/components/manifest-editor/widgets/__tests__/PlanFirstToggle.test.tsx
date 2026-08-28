/**
 * PlanFirstToggle tests — B-35 PR-2(开关联动确认 Modal).
 *
 * spec: docs/superpowers/specs/2026-08-28-plan-first-execution-design.md §3。
 * 开启走确认 Modal(硬联动清单 + 建议检查清单),确认后**一次** onChange
 * 写入三字段;取消零写入。关闭直接写(仅删 execution_mode,不回退
 * workflow.type / dynamic_workers)并出提示。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../../../i18n";

import { PlanFirstToggle } from "../PlanFirstToggle";
import type { AgentManifest } from "../../form_model";

function renderToggle(
  formData: AgentManifest,
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(
    <App>
      <PlanFirstToggle formData={formData} onChange={onChange} />
    </App>,
  );
}

function manifest(overrides: Record<string, unknown> = {}): AgentManifest {
  return { spec: { workflow: { type: "react" }, ...overrides } } as AgentManifest;
}

describe("PlanFirstToggle", () => {
  it("renders unchecked by default and checked when plan_first is on", () => {
    const { unmount } = renderToggle(manifest());
    expect(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch"),
    ).not.toHaveAttribute("aria-checked", "true");
    unmount();
    renderToggle({
      spec: { workflow: { type: "plan_execute", execution_mode: "plan_first" } },
    } as AgentManifest);
    expect(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch"),
    ).toHaveAttribute("aria-checked", "true");
  });

  it("turning on opens the confirm modal listing hard links + checklist", async () => {
    const user = userEvent.setup();
    renderToggle(manifest({ dynamic_workers: { enabled: false } }));
    await user.click(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch")!,
    );
    // 硬联动区:两条都会改(react→plan_execute / workers 关→开)。
    expect(await screen.findByText(/plan_execute/)).toBeInTheDocument();
    const modal = document.querySelector(".ant-modal-body") as HTMLElement;
    expect(modal.textContent).toContain("react");
    // 建议检查区:token 成本大白话(~15)。
    expect(modal.textContent).toContain("15");
  });

  it("confirming writes all three linked fields in ONE onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderToggle(manifest({ dynamic_workers: { enabled: false } }), onChange);
    await user.click(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch")!,
    );
    await screen.findByText(/plan_execute/);
    await user.click(
      document.querySelector(".ant-modal-confirm-btns .ant-btn-primary")!,
    );
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.workflow?.execution_mode).toBe("plan_first");
    expect(next.spec?.workflow?.type).toBe("plan_execute");
    expect(next.spec?.dynamic_workers?.enabled).toBeUndefined();
  });

  it("cancelling writes nothing", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderToggle(manifest(), onChange);
    await user.click(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch")!,
    );
    await screen.findByText(/plan_execute/);
    const cancel = document.querySelector(
      ".ant-modal-confirm-btns .ant-btn:not(.ant-btn-primary)",
    ) as HTMLElement;
    await user.click(cancel);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("turning off removes only execution_mode — no rollback of type/workers", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderToggle(
      {
        spec: {
          workflow: { type: "plan_execute", execution_mode: "plan_first" },
        },
      } as AgentManifest,
      onChange,
    );
    await user.click(
      screen.getByTestId("plan-first-toggle").querySelector(".ant-switch")!,
    );
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1));
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.workflow?.execution_mode).toBeUndefined();
    expect(next.spec?.workflow?.type).toBe("plan_execute");
  });

  it("renders as a FieldRow: help ⓘ present, customized badge only when on", () => {
    const { rerender } = render(
      <App><PlanFirstToggle formData={manifest()} onChange={vi.fn()} /></App>,
    );
    expect(
      screen.getByTestId("field-help-workflow.execution_mode"),
    ).toBeInTheDocument();
    expect(
      screen.queryByTestId("field-customized-workflow.execution_mode"),
    ).not.toBeInTheDocument();
    const on = manifest({
      workflow: { type: "plan_execute", execution_mode: "plan_first" },
    });
    rerender(
      <App><PlanFirstToggle formData={on} onChange={vi.fn()} /></App>,
    );
    expect(
      screen.getByTestId("field-customized-workflow.execution_mode"),
    ).toBeInTheDocument();
  });
});

