import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../../i18n";

import { SecuritySection } from "../SecuritySection";
import type { AgentManifest } from "../../form_model";

const DEFENSES_FIELD_IDS = [
  "defenses.prompt_injection",
  "defenses.output_screen",
  "defenses.output_judge",
  "defenses.output_dlp",
  "defenses.action_screen",
];

const NETWORK_FIELD_IDS = [
  "dynamic_workers.enabled",
  "sandbox.network.egress",
  "sandbox.network.allowlist",
  "sandbox.network.denylist",
  "policies.tool_use_enforcement",
];

function renderSection(
  formData: AgentManifest = {},
  onChange: (d: unknown) => void = vi.fn(),
) {
  return render(<SecuritySection formData={formData} onChange={onChange} />);
}

function rowFor(fieldId: string): HTMLElement {
  return document.querySelector(`[data-field-id="${fieldId}"]`) as HTMLElement;
}

async function openTab(
  user: ReturnType<typeof userEvent.setup>,
  label: string,
): Promise<void> {
  await user.click(screen.getByRole("tab", { name: label }));
}

/**
 * In jsdom, Antd's Select renders each option twice: a visible, clickable
 * ``.ant-select-item-option`` div and a hidden ARIA ``role="option"`` mirror
 * with the same text. This opens the given combobox and clicks the real
 * ``.ant-select-item-option-content`` carrying the requested label (mirrors
 * ``field_defs.test.tsx``'s helper of the same shape).
 */
async function pickOption(
  user: ReturnType<typeof userEvent.setup>,
  combobox: HTMLElement,
  label: string,
): Promise<void> {
  await user.click(combobox);
  const item = await screen.findByText(optionContent(label));
  await user.click(item);
}

const optionContent =
  (label: string) => (_content: string, el: Element | null) =>
    el?.classList.contains("ant-select-item-option-content") === true &&
    el.textContent === label;

/** rc-select's Enter/Backspace handling checks ``keyCode``, which
 * ``userEvent``'s synthetic ``{Enter}``/``{Backspace}`` don't populate in
 * jsdom — fire the DOM event directly instead (mirrors ``field_defs.test.tsx``). */
function pressKey(el: HTMLElement, key: "Enter" | "Backspace"): void {
  const keyCode = key === "Enter" ? 13 : 8;
  fireEvent.keyDown(el, { key, code: key, keyCode, which: keyCode });
}

describe("SecuritySection", () => {
  it("renders three sub-tabs (defenses/approval/network), defenses active by default", () => {
    renderSection();
    expect(screen.getByTestId("security-tab-defenses")).toBeInTheDocument();
    const approvalTab = screen.getByRole("tab", { name: "Human approval" });
    const networkTab = screen.getByRole("tab", {
      name: "Subtasks & network",
    });
    expect(approvalTab).toBeInTheDocument();
    expect(networkTab).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Defenses" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("renders all 5 defenses FieldRows with zero clicks (defenses is the default tab)", () => {
    renderSection();
    for (const id of DEFENSES_FIELD_IDS) {
      expect(rowFor(id)).toBeInTheDocument();
    }
  });

  it("output_screen is on by default; toggling it off writes defenses.output_screen and shows the warning", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    const sw = within(rowFor("defenses.output_screen")).getByRole("switch");
    expect(sw).toBeChecked();
    await user.click(sw);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.defenses?.output_screen).toBe("off");
  });

  it("shows the output_screen off-warning only when it's off", () => {
    renderSection();
    expect(
      screen.queryByText(/no longer blocked/),
    ).not.toBeInTheDocument();
    const off: AgentManifest = { spec: { defenses: { output_screen: "off" } } };
    renderSection(off);
    expect(screen.getByText(/no longer blocked/)).toBeInTheDocument();
  });

  it("turning prompt_injection off writes defenses.prompt_injection=off", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    const sw = within(rowFor("defenses.prompt_injection")).getByRole(
      "switch",
    );
    expect(sw).toBeChecked(); // spotlight default = on
    await user.click(sw);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.defenses?.prompt_injection).toBe("off");
  });

  it("enabling the judge writes defenses.output_judge=block and reveals the on-error select", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    expect(
      screen.queryByTestId("af-defenses-output-judge-on-error"),
    ).not.toBeInTheDocument();
    const sw = within(rowFor("defenses.output_judge")).getByRole("switch");
    await user.click(sw);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.defenses?.output_judge).toBe("block");

    const judged: AgentManifest = { spec: { defenses: { output_judge: "block" } } };
    renderSection(judged);
    expect(
      screen.getByTestId("af-defenses-output-judge-on-error"),
    ).toBeInTheDocument();
  });

  it("enabling DLP writes defenses.output_dlp=redact and shows the redaction note", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    const sw = within(rowFor("defenses.output_dlp")).getByRole("switch");
    await user.click(sw);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.defenses?.output_dlp).toBe("redact");

    const redacting: AgentManifest = { spec: { defenses: { output_dlp: "redact" } } };
    renderSection(redacting);
    expect(screen.getByText(/rewrites legitimate replies/i)).toBeInTheDocument();
  });

  it("shows the action_screen on-error select only when action_screen != off", () => {
    renderSection(); // action_screen off by default
    expect(
      screen.queryByTestId("af-defenses-action-screen-on-error"),
    ).not.toBeInTheDocument();
    const withAction: AgentManifest = {
      spec: { defenses: { action_screen: "block" } },
    };
    renderSection(withAction);
    expect(
      screen.getByTestId("af-defenses-action-screen-on-error"),
    ).toBeInTheDocument();
  });

  it("shows the extends note only when spec.extends is set", () => {
    renderSection();
    expect(
      screen.queryByTestId("af-defenses-extends-note"),
    ).not.toBeInTheDocument();
    const withExtends: AgentManifest = { spec: { extends: "secure-template" } };
    renderSection(withExtends);
    expect(screen.getByTestId("af-defenses-extends-note")).toBeInTheDocument();
  });

  it("treats the backend's dumped extends: null as not-inherited — no extends note (#5)", () => {
    // The backend's full dump always writes ``extends: null`` for agents that
    // never extended a template; the note must not misfire on it.
    const nullExtends: AgentManifest = { spec: { extends: null } };
    renderSection(nullExtends);
    expect(
      screen.queryByTestId("af-defenses-extends-note"),
    ).not.toBeInTheDocument();
  });

  it("approval tab: checking bash adds it to policies.approval_required_tools", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Human approval");
    await user.click(screen.getByTestId("af-approval-bash"));
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.approval_required_tools).toEqual(["bash"]);
  });

  it("approval tab: approval_timeout defaults to 86400 and editing it writes approval_timeout_s", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Human approval");
    const input = within(rowFor("policies.approval_timeout_s")).getByRole(
      "spinbutton",
    );
    expect(input).toHaveValue("86400");
    await user.clear(input);
    await user.type(input, "3600");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.approval_timeout_s).toBe(3600);
  });

  it("approval tab: clarification_timeout defaults to 3600 and editing it writes clarification_timeout_s", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Human approval");
    const input = within(rowFor("policies.clarification_timeout_s")).getByRole(
      "spinbutton",
    );
    expect(input).toHaveValue("3600");
    await user.clear(input);
    await user.type(input, "600");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.clarification_timeout_s).toBe(600);
    // Sibling knob untouched — same policies block, different key.
    expect(last.spec?.policies?.approval_timeout_s).toBeUndefined();
  });

  it("network tab: renders all 5 FieldRows once its tab is active", async () => {
    const user = userEvent.setup();
    renderSection();
    await openTab(user, "Subtasks & network");
    for (const id of NETWORK_FIELD_IDS) {
      expect(rowFor(id)).toBeInTheDocument();
    }
  });

  it("network tab: turning dynamic workers off writes dynamic_workers.enabled=false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Subtasks & network");
    const sw = within(rowFor("dynamic_workers.enabled")).getByRole("switch");
    expect(sw).toBeChecked(); // default on
    await user.click(sw);
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.dynamic_workers?.enabled).toBe(false);
  });

  it("network tab: choosing egress=none writes spec.sandbox.network.egress", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Subtasks & network");

    const combobox = within(rowFor("sandbox.network.egress")).getByRole(
      "combobox",
    );
    await pickOption(user, combobox, "None (blocked)");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.sandbox?.network?.egress).toBe("none");
  });

  it("network tab: choosing the egress option matching the default deletes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: { sandbox: { network: { egress: "none" } } },
    };
    renderSection(seed, onChange);
    await openTab(user, "Subtasks & network");

    const combobox = within(rowFor("sandbox.network.egress")).getByRole(
      "combobox",
    );
    await pickOption(user, combobox, "Proxy (default)");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.sandbox?.network?.egress).toBeUndefined();
  });

  it("network tab: typing two domains into the allowlist writes a string array", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const { rerender } = renderSection({}, onChange);
    await openTab(user, "Subtasks & network");

    const combobox = within(rowFor("sandbox.network.allowlist")).getByRole(
      "combobox",
    );
    await user.click(combobox);
    await user.keyboard("a.example.com");
    pressKey(combobox, "Enter");

    let last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.sandbox?.network?.allowlist).toEqual(["a.example.com"]);

    // Controlled Select — feed the first patch back into formData before
    // typing the second entry (mirrors field_defs.test.tsx's tags round trip).
    rerender(<SecuritySection formData={last} onChange={onChange} />);
    await user.keyboard("b.example.com");
    const opt = await screen.findByText(optionContent("b.example.com"));
    await user.click(opt);

    last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.sandbox?.network?.allowlist).toEqual([
      "a.example.com",
      "b.example.com",
    ]);
  });

  it("network tab: clearing the denylist back to empty deletes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seed: AgentManifest = {
      spec: { sandbox: { network: { denylist: ["bad.example.com"] } } },
    };
    renderSection(seed, onChange);
    await openTab(user, "Subtasks & network");

    const combobox = within(rowFor("sandbox.network.denylist")).getByRole(
      "combobox",
    );
    await user.click(combobox);
    pressKey(combobox, "Backspace");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.sandbox?.network?.denylist).toBeUndefined();
  });

  it("network tab: choosing tool_use_enforcement=on writes policies.tool_use_enforcement", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSection({}, onChange);
    await openTab(user, "Subtasks & network");

    const combobox = within(
      rowFor("policies.tool_use_enforcement"),
    ).getByRole("combobox");
    await pickOption(user, combobox, "Always on");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.policies?.tool_use_enforcement).toBe("on");
  });

  it("renders the shortened closing dict-note", () => {
    renderSection();
    expect(screen.getByTestId("security-gates-dict-note")).toHaveTextContent(
      "Rate limiting, PII redaction, and security policy are advanced items",
    );
  });

  it("no longer renders a trajectory-recording control anywhere", () => {
    renderSection();
    expect(
      screen.queryByTestId("af-trajectory-recording"),
    ).not.toBeInTheDocument();
  });
});
