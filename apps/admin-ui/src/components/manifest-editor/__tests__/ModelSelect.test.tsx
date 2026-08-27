import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import "../../../i18n";
import type { ModelCatalog } from "../../../api/model_catalog";
import type { ModelFields } from "../form_model";
import { ModelSelect } from "../widgets/ModelSelect";

/**
 * In jsdom, Antd's Select renders each option twice: a visible, clickable
 * `.ant-select-item-option` div and a hidden ARIA `role="option"` mirror with
 * the same text. This helper opens the given combobox and clicks the real
 * `.ant-select-item-option-content` carrying the requested label.
 */
async function pickOption(
  user: ReturnType<typeof userEvent.setup>,
  combobox: HTMLElement,
  label: string,
): Promise<void> {
  await user.click(combobox);
  const item = await screen.findByText(
    (_content, el) =>
      el?.classList.contains("ant-select-item-option-content") === true &&
      el.textContent === label,
  );
  await user.click(item);
}

const CATALOG: ModelCatalog = {
  providers: [
    {
      provider: "anthropic",
      models: [
        {
          name: "claude-4.6-sonnet",
          vision: false,
          embeddings: false,
          context_window: 500000,
          deprecated: false,
          thinking: "toggle",
          thinking_default: true,
        },
        {
          name: "claude-4.6-haiku",
          vision: false,
          embeddings: false,
          context_window: 200000,
          deprecated: false,
          thinking: "toggle",
          thinking_default: true,
        },
      ],
    },
    {
      provider: "deepseek",
      models: [
        {
          name: "deepseek-v4-pro",
          vision: false,
          embeddings: false,
          context_window: 1000000,
          deprecated: false,
          thinking: "effort",
          thinking_default: true,
        },
      ],
    },
    {
      provider: "openai",
      models: [
        {
          name: "gpt-5.5",
          vision: true,
          embeddings: false,
          context_window: 128000,
          deprecated: false,
          thinking: "effort",
          thinking_default: true,
        },
        {
          name: "text-embedding-3-large",
          vision: false,
          embeddings: true,
          context_window: null,
          deprecated: false,
        },
      ],
    },
    {
      provider: "kimi",
      models: [
        {
          name: "kimi-k3",
          vision: true,
          embeddings: false,
          context_window: 1000000,
          deprecated: false,
          thinking: "effort",
          thinking_default: true,
        },
      ],
    },
    {
      provider: "glm",
      models: [
        {
          name: "glm-5.1",
          vision: false,
          embeddings: false,
          context_window: 200000,
          deprecated: false,
          thinking: "toggle",
          thinking_default: true,
        },
        {
          name: "glm-5.3",
          vision: false,
          embeddings: false,
          context_window: 1000000,
          deprecated: false,
          thinking: "effort",
          thinking_default: true,
        },
        {
          name: "glm-5.3-flash",
          vision: true,
          embeddings: false,
          context_window: 1000000,
          deprecated: false,
          thinking: "effort",
          thinking_default: true,
          always_thinking: true,
        },
      ],
    },
  ],
};

function renderSelect(value: ModelFields, onChange = vi.fn()) {
  return {
    onChange,
    ...render(
      <ModelSelect value={value} catalog={CATALOG} onChange={onChange} />,
    ),
  };
}

describe("ModelSelect", () => {
  it("selecting a provider resets name + supports_vision", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect({}, onChange);
    const provider = within(
      screen.getByTestId("model-select-provider"),
    ).getByRole("combobox");
    await pickOption(user, provider, "openai");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "openai",
        name: undefined,
        supports_vision: false,
      }),
    );
  });

  it("selecting a vision model auto-sets supports_vision from the catalog entry", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect({ provider: "openai" }, onChange);
    const nameSel = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await pickOption(user, nameSel, "gpt-5.5");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "openai",
        name: "gpt-5.5",
        supports_vision: true,
      }),
    );
  });

  it("renders the temperature control with the current value", () => {
    renderSelect({ provider: "openai", name: "gpt-5.5", temperature: 0.7 });
    const row = screen.getByTestId("model-select-temperature");
    expect(row).toBeInTheDocument();
    expect(row).toHaveTextContent(/0\.7/);
  });

  it("temperature slider change calls onChange with the new temperature", () => {
    // rc-slider's onChange doesn't fire from synthetic pointer/keyboard events
    // in jsdom (it reads layout geometry it can't measure), so drive the
    // handle's keyDown directly — rc-slider's keyboard handler is wired here.
    const onChange = vi.fn();
    renderSelect(
      { provider: "openai", name: "gpt-5.5", temperature: 0.2 },
      onChange,
    );
    const slider = within(
      screen.getByTestId("model-select-temperature"),
    ).getByRole("slider");
    fireEvent.keyDown(slider, { key: "ArrowUp", keyCode: 38 });
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ temperature: expect.any(Number) }),
    );
  });

  it("advanced panel exposes max_tokens and rate_limit_rpm inputs", async () => {
    const user = userEvent.setup();
    renderSelect({ provider: "openai", name: "gpt-5.5" });
    await user.click(
      within(screen.getByTestId("model-select-advanced")).getByText("Advanced"),
    );
    const advanced = screen.getByTestId("model-select-advanced");
    expect(within(advanced).getByText("max_tokens")).toBeInTheDocument();
    expect(within(advanced).getByText("rate_limit_rpm")).toBeInTheDocument();
  });

  it("advanced panel exposes a context_window input that writes onChange", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "openai", name: "gpt-5.5", context_window: 64000 },
      onChange,
    );
    await user.click(
      within(screen.getByTestId("model-select-advanced")).getByText("Advanced"),
    );
    // aria-labelled (axe) and shows the current value.
    const input = screen.getByLabelText("Context window");
    expect(input).toHaveValue("64000");
    await user.clear(input);
    await user.type(input, "32000");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ context_window: 32000 }),
    );
  });

  it("renders translated vision label, not the raw i18n key", () => {
    renderSelect({
      provider: "deepseek",
      name: "deepseek-v4-pro",
      supports_vision: false,
    });
    expect(
      screen.queryByText("model_select.vision_off"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("model-select-vision")).toHaveTextContent(
      /视觉|Vision/,
    );
  });

  const optionContent = (label: string) => (_c: string, el: Element | null) =>
    el?.classList.contains("ant-select-item-option-content") === true &&
    el.textContent === label;

  it("visionOnly hides providers with no vision model", async () => {
    const user = userEvent.setup();
    render(
      <ModelSelect
        visionOnly
        value={{}}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    const provider = within(
      screen.getByTestId("model-select-provider"),
    ).getByRole("combobox");
    await user.click(provider);
    // openai has gpt-5.5 (vision) → shown; deepseek has no vision model → hidden.
    expect(
      await screen.findByText(optionContent("openai")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(optionContent("deepseek")),
    ).not.toBeInTheDocument();
  });

  // ---- Thinking-Toggle ----

  it("renders the thinking switch for a thinking-capable model, hidden otherwise", () => {
    const { rerender } = render(
      <ModelSelect
        value={{ provider: "openai", name: "gpt-5.5" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId("model-select-thinking")).toBeInTheDocument();
    // embedding model has no thinking knob → no switch.
    rerender(
      <ModelSelect
        value={{ provider: "openai", name: "text-embedding-3-large" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("model-select-thinking"),
    ).not.toBeInTheDocument();
  });

  it("selecting a model seeds thinking_enabled from the catalog default", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect({ provider: "openai" }, onChange);
    const nameSel = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await pickOption(user, nameSel, "gpt-5.5");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ name: "gpt-5.5", thinking_enabled: true }),
    );
  });

  it("selecting a no-thinking model clears thinking_enabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "openai", name: "gpt-5.5", thinking_enabled: true },
      onChange,
    );
    const nameSel = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await pickOption(user, nameSel, "text-embedding-3-large");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "text-embedding-3-large",
        thinking_enabled: undefined,
      }),
    );
  });

  it("selecting a no-thinking model clears effort (was set on a thinking model)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "openai", name: "gpt-5.5", effort: "high" },
      onChange,
    );
    const nameSel = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await pickOption(user, nameSel, "text-embedding-3-large");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "text-embedding-3-large",
        effort: undefined,
      }),
    );
  });

  it("selecting another thinking model preserves effort", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "anthropic", name: "claude-4.6-sonnet", effort: "high" },
      onChange,
    );
    const nameSel = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await pickOption(user, nameSel, "claude-4.6-haiku");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        name: "claude-4.6-haiku",
        effort: "high",
      }),
    );
  });

  it("switching provider clears effort, adaptive_thinking, cache_enabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      {
        provider: "anthropic",
        name: "claude-4.6-sonnet",
        effort: "high",
        adaptive_thinking: true,
        cache_enabled: false,
      },
      onChange,
    );
    const provider = within(
      screen.getByTestId("model-select-provider"),
    ).getByRole("combobox");
    await pickOption(user, provider, "openai");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "openai",
        effort: undefined,
        adaptive_thinking: undefined,
        cache_enabled: undefined,
      }),
    );
  });

  it("toggling the switch writes thinking_enabled", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "openai", name: "gpt-5.5", thinking_enabled: true },
      onChange,
    );
    await user.click(
      within(screen.getByTestId("model-select-thinking")).getByRole("switch"),
    );
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ thinking_enabled: false }),
    );
  });

  it("shows the cannot-fully-disable hint only for reasoning_effort non-anthropic models", () => {
    const { rerender } = render(
      <ModelSelect
        value={{ provider: "openai", name: "gpt-5.5" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    // effort vendor (openai) → hint shown.
    expect(
      screen.getByTestId("model-select-thinking-hint"),
    ).toBeInTheDocument();
    // toggle vendor (glm) → fully disable-able, no hint.
    rerender(
      <ModelSelect
        value={{ provider: "glm", name: "glm-5.1" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("model-select-thinking-hint"),
    ).not.toBeInTheDocument();
    // glm 5.2+ is shape "effort" but keeps a REAL off
    // (thinking.type=disabled) — still no hint.
    rerender(
      <ModelSelect
        value={{ provider: "glm", name: "glm-5.3" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("model-select-thinking-hint"),
    ).not.toBeInTheDocument();
    // deepseek also has a real off (thinking.type=disabled) — no hint.
    rerender(
      <ModelSelect
        value={{ provider: "deepseek", name: "deepseek-v4-pro" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.queryByTestId("model-select-thinking-hint"),
    ).not.toBeInTheDocument();
    // kimi-k3 is always-thinking (off floors at reasoning_effort=low) — hint shown.
    rerender(
      <ModelSelect
        value={{ provider: "kimi", name: "kimi-k3" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.getByTestId("model-select-thinking-hint"),
    ).toBeInTheDocument();
    // glm-5.3-flash — provider glm normally has a real off, but the catalog
    // marks this entry always_thinking (thinking.type only supports enabled)
    // → hint shown despite the provider.
    rerender(
      <ModelSelect
        value={{ provider: "glm", name: "glm-5.3-flash" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    expect(
      screen.getByTestId("model-select-thinking-hint"),
    ).toBeInTheDocument();
  });

  it("visionOnly shows only vision-capable models for a provider", async () => {
    const user = userEvent.setup();
    render(
      <ModelSelect
        visionOnly
        value={{ provider: "openai" }}
        catalog={CATALOG}
        onChange={vi.fn()}
      />,
    );
    const model = within(screen.getByTestId("model-select-name")).getByRole(
      "combobox",
    );
    await user.click(model);
    expect(
      await screen.findByText(optionContent("gpt-5.5")),
    ).toBeInTheDocument();
    // the embedding model is non-vision → excluded.
    expect(
      screen.queryByText(optionContent("text-embedding-3-large")),
    ).not.toBeInTheDocument();
  });

  // ---- effort / adaptive_thinking / cache_enabled (advanced panel) ----

  async function openAdvanced(user: ReturnType<typeof userEvent.setup>) {
    await user.click(
      within(screen.getByTestId("model-select-advanced")).getByText(
        "Advanced",
      ),
    );
  }

  it("shows effort/adaptive/cache for an anthropic thinking-capable model", async () => {
    const user = userEvent.setup();
    renderSelect({ provider: "anthropic", name: "claude-4.6-sonnet" });
    await openAdvanced(user);
    expect(screen.getByTestId("model-select-effort")).toBeInTheDocument();
    expect(screen.getByTestId("model-select-adaptive")).toBeInTheDocument();
    expect(screen.getByTestId("model-select-cache")).toBeInTheDocument();
  });

  it("effort follows the same catalog gate as the thinking switch — shown for a non-anthropic thinking-capable model, but adaptive/cache stay hidden", async () => {
    const user = userEvent.setup();
    renderSelect({ provider: "openai", name: "gpt-5.5" });
    await openAdvanced(user);
    expect(screen.getByTestId("model-select-effort")).toBeInTheDocument();
    expect(
      screen.queryByTestId("model-select-adaptive"),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("model-select-cache")).not.toBeInTheDocument();
  });

  it("hides effort/adaptive/cache entirely for an OpenAI-family model with no thinking knob", async () => {
    const user = userEvent.setup();
    renderSelect({ provider: "openai", name: "text-embedding-3-large" });
    await openAdvanced(user);
    expect(screen.queryByTestId("model-select-effort")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("model-select-adaptive"),
    ).not.toBeInTheDocument();
    expect(screen.queryByTestId("model-select-cache")).not.toBeInTheDocument();
  });

  it("effort select writes value.effort on pick", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "anthropic", name: "claude-4.6-sonnet" },
      onChange,
    );
    await openAdvanced(user);
    const effortSel = within(
      screen.getByTestId("model-select-effort"),
    ).getByRole("combobox");
    await pickOption(user, effortSel, "high");
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ effort: "high" }),
    );
  });

  it("clearing the effort select removes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "anthropic", name: "claude-4.6-sonnet", effort: "high" },
      onChange,
    );
    await openAdvanced(user);
    const clearIcon = within(
      screen.getByTestId("model-select-effort"),
    ).getByLabelText("close-circle");
    await user.click(clearIcon);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ effort: undefined }),
    );
  });

  it("adaptive_thinking switch on writes true", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "anthropic", name: "claude-4.6-sonnet" },
      onChange,
    );
    await openAdvanced(user);
    const sw = within(
      screen.getByTestId("model-select-adaptive"),
    ).getByRole("switch");
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ adaptive_thinking: true }),
    );
  });

  it("adaptive_thinking switch off (=default) removes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      {
        provider: "anthropic",
        name: "claude-4.6-sonnet",
        adaptive_thinking: true,
      },
      onChange,
    );
    await openAdvanced(user);
    const sw = within(
      screen.getByTestId("model-select-adaptive"),
    ).getByRole("switch");
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ adaptive_thinking: undefined }),
    );
  });

  it("cache_enabled defaults to checked (on) and turning off writes false", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      { provider: "anthropic", name: "claude-4.6-sonnet" },
      onChange,
    );
    await openAdvanced(user);
    const sw = within(
      screen.getByTestId("model-select-cache"),
    ).getByRole("switch");
    expect(sw).toBeChecked();
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ cache_enabled: false }),
    );
  });

  it("cache_enabled: turning back on (=default) removes the key", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderSelect(
      {
        provider: "anthropic",
        name: "claude-4.6-sonnet",
        cache_enabled: false,
      },
      onChange,
    );
    await openAdvanced(user);
    const sw = within(
      screen.getByTestId("model-select-cache"),
    ).getByRole("switch");
    expect(sw).not.toBeChecked();
    await user.click(sw);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ cache_enabled: undefined }),
    );
  });

  it("renders max_tokens and rate_limit_rpm hint text in the advanced panel", async () => {
    const user = userEvent.setup();
    renderSelect({ provider: "openai", name: "gpt-5.5" });
    await openAdvanced(user);
    const advanced = screen.getByTestId("model-select-advanced");
    expect(
      within(advanced).getByText(/Output token ceiling for a single reply/),
    ).toBeInTheDocument();
    expect(
      within(advanced).getByText(/Request-rate ceiling for this model/),
    ).toBeInTheDocument();
  });
});
