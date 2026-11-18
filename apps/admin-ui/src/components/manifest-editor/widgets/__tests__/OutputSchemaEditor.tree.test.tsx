/**
 * OutputSchemaEditor — B-315 field-tree behaviour: add a sub-field, the
 * fixed "each item" row of a list, the Limits popover, type change clearing
 * limits that no longer apply, delete, a readonly node (no controls, raw
 * kept on sibling edits), nested name checks and the strict-mode reminder.
 * Controlled harness throughout so the echo/resync path is exercised.
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { OutputSchemaEditor } from "../OutputSchemaEditor";
import type { AgentManifest } from "../../form_model";

function renderControlled(initial: unknown, onChangeSpy: (d: unknown) => void = vi.fn()) {
  function Harness() {
    const [data, setData] = useState<unknown>(initial);
    return (
      <App>
        <OutputSchemaEditor
          formData={data}
          onChange={(next) => {
            setData(next);
            onChangeSpy(next);
          }}
        />
      </App>
    );
  }
  return render(<Harness />);
}

const withSchema = (json_schema: unknown, extra: Record<string, unknown> = {}) => ({
  spec: { output_schema: { ...extra, json_schema } },
});

function lastSchema(onChange: ReturnType<typeof vi.fn>): unknown {
  return (onChange.mock.calls.at(-1)?.[0] as AgentManifest).spec?.output_schema?.json_schema;
}

async function pickType(testId: string, typeKey: string): Promise<void> {
  const select = screen.getByTestId(testId);
  fireEvent.mouseDown(select.querySelector(".ant-select-selector") ?? select);
  const label = i18n.t(`agent_form.output_schema.type_${typeKey}`);
  const options = await screen.findAllByText(label, { selector: ".ant-select-item-option-content" });
  await userEvent.click(options[options.length - 1]);
}

const HEALTH = {
  type: "object",
  additionalProperties: false,
  required: ["messages", "media"],
  properties: {
    messages: {
      type: "array",
      maxItems: 4,
      items: { type: "string", minLength: 1, maxLength: 300, pattern: "\\S" },
    },
    media: { type: "array", maxItems: 3, uniqueItems: true, items: { type: "integer", minimum: 1 } },
  },
};

afterEach(() => {
  document.body.innerHTML = "";
});

describe("OutputSchemaEditor field tree", () => {
  it("shows a list's fixed 'each item' row with its own type", () => {
    renderControlled(withSchema(HEALTH));
    const items = screen.getByTestId("af-output-schema-row-1.items");
    expect(within(items).getByText(i18n.t("agent_form.output_schema.items_label"))).toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-limits-1.items")).toHaveTextContent("(1)");
    expect(screen.queryByTestId("af-output-schema-remove-1.items")).not.toBeInTheDocument();
  });

  it("collapsing a list hides its item row", async () => {
    const user = userEvent.setup();
    renderControlled(withSchema(HEALTH));
    await user.click(screen.getByTestId("af-output-schema-toggle-0"));
    expect(screen.queryByTestId("af-output-schema-row-0.items")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-row-1.items")).toBeInTheDocument();
  });

  it("adds a sub-field under an object field", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled(
      withSchema({ type: "object", properties: { contact: { type: "object", properties: {} } } }),
      onChange,
    );
    await user.click(screen.getByTestId("af-output-schema-add-child-0"));
    await user.type(screen.getByTestId("af-output-schema-name-0.0"), "phone");
    await user.click(screen.getByTestId("af-output-schema-required-0.0"));
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: {
        contact: {
          type: "object",
          properties: { phone: { type: "string" } },
          required: ["phone"],
        },
      },
    });
  });

  it("edits a limit in the popover (messages maxItems 4 → 3), nothing else changes", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled(withSchema(HEALTH), onChange);
    await user.click(screen.getByTestId("af-output-schema-limits-0"));
    const maxItems = await screen.findByTestId("af-output-schema-limit-maxItems-0");
    await user.clear(maxItems);
    await user.type(maxItems, "3");
    const expected = structuredClone(HEALTH);
    expected.properties.messages.maxItems = 3;
    const written = lastSchema(onChange);
    expect(written).toEqual(expected);
    expect(JSON.stringify(written)).toBe(JSON.stringify(expected));
  });

  it("sets text limits and allowed values from the popover", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled(withSchema({ type: "object", properties: { level: { type: "string" } } }), onChange);
    await user.click(screen.getByTestId("af-output-schema-limits-0"));
    await user.type(await screen.findByTestId("af-output-schema-limit-minLength-0"), "2");
    await user.type(screen.getByTestId("af-output-schema-limit-pattern-0"), "^\\w+$");
    const enumBox = within(screen.getByTestId("af-output-schema-enum-0")).getByRole("combobox");
    // Comma separates values (Enter also works in a real browser — checked in
    // Chromium — but rc-select's Enter path doesn't fire under jsdom).
    await user.type(enumBox, "low,high,");
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: { level: { type: "string", minLength: 2, pattern: "^\\w+$", enum: ["low", "high"] } },
    });
    expect(screen.getByTestId("af-output-schema-limits-0")).toHaveTextContent("(3)");
  });

  it("changing a type clears limits the new type can't use", async () => {
    const onChange = vi.fn();
    renderControlled(
      withSchema({
        type: "object",
        properties: { code: { type: "string", maxLength: 6, pattern: "^[A-Z]+$", enum: ["AB"], title: "Code" } },
      }),
      onChange,
    );
    await pickType("af-output-schema-type-0", "integer");
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: { code: { type: "integer", title: "Code" } },
    });
  });

  it("changing a field to a list adds an 'each item' row", async () => {
    const onChange = vi.fn();
    renderControlled(withSchema({ type: "object", properties: { tags: { type: "string" } } }), onChange);
    await pickType("af-output-schema-type-0", "array");
    expect(screen.getByTestId("af-output-schema-row-0.items")).toBeInTheDocument();
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: { tags: { type: "array", items: { type: "string" } } },
    });
  });

  it("deletes a nested field and drops it from required", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled(
      withSchema({
        type: "object",
        properties: {
          links: {
            type: "array",
            items: {
              type: "object",
              properties: { title: { type: "string" }, url: { type: "string" } },
              required: ["title", "url"],
            },
          },
        },
      }),
      onChange,
    );
    await user.click(screen.getByTestId("af-output-schema-remove-0.items.0"));
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: {
        links: {
          type: "array",
          items: { type: "object", properties: { url: { type: "string" } }, required: ["url"] },
        },
      },
    });
  });

  describe("readonly node", () => {
    const choice = { anyOf: [{ type: "string" }, { type: "null" }] };
    const schema = {
      type: "object",
      properties: { reason: choice, note: { type: "string" } },
      required: ["reason"],
    };

    it("greyed with the YAML note and no editable control", () => {
      renderControlled(withSchema(schema));
      const row = screen.getByTestId("af-output-schema-row-0");
      expect(within(row).getByText(i18n.t("agent_form.output_schema.readonly_node"))).toBeInTheDocument();
      expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
      expect(within(row).queryByRole("combobox")).not.toBeInTheDocument();
      expect(within(row).queryByRole("checkbox")).not.toBeInTheDocument();
      expect(screen.queryByTestId("af-output-schema-limits-0")).not.toBeInTheDocument();
      expect(screen.getByTestId("af-output-schema-name-1")).toBeEnabled();
    });

    it("editing a sibling keeps the readonly node's original text", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderControlled(withSchema(schema), onChange);
      await user.type(screen.getByTestId("af-output-schema-desc-1"), "x");
      expect(lastSchema(onChange)).toEqual({
        ...schema,
        properties: { reason: choice, note: { type: "string", description: "x" } },
      });
    });
  });

  it("flags a duplicate name among nested siblings and leaves it out of the write", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled(
      withSchema({ type: "object", properties: { c: { type: "object", properties: { a: { type: "string" } } } } }),
      onChange,
    );
    await user.click(screen.getByTestId("af-output-schema-add-child-0"));
    await user.type(screen.getByTestId("af-output-schema-name-0.1"), "a");
    expect(screen.getByTestId("af-output-schema-name-0.1")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(i18n.t("agent_form.output_schema.name_duplicate"))).toBeInTheDocument();
    expect(lastSchema(onChange)).toEqual({
      type: "object",
      properties: { c: { type: "object", properties: { a: { type: "string" } } } },
    });
  });

  describe("strict-mode reminder", () => {
    const schema = { type: "object", properties: { a: { type: "string" } } };

    it("shows for an optional field when strict is unset (default true), clears once required", async () => {
      const user = userEvent.setup();
      renderControlled(withSchema(schema));
      expect(screen.getByTestId("af-output-schema-strict-warn-0")).toBeInTheDocument();
      await user.click(screen.getByTestId("af-output-schema-required-0"));
      expect(screen.queryByTestId("af-output-schema-strict-warn-0")).not.toBeInTheDocument();
    });

    it("never shows with strict: false", () => {
      renderControlled(withSchema(schema, { strict: false }));
      expect(screen.queryByTestId("af-output-schema-strict-warn-0")).not.toBeInTheDocument();
    });
  });

  it("at depth 5 an object field cannot add sub-fields", () => {
    const lvl = (inner: Record<string, unknown>) => ({ type: "object", properties: { n: inner } });
    renderControlled(withSchema(lvl(lvl(lvl(lvl(lvl({ type: "object", properties: {} })))))));
    expect(screen.getByTestId("af-output-schema-add-child-0.0.0.0")).toBeEnabled();
    expect(screen.getByTestId("af-output-schema-add-child-0.0.0.0.0")).toBeDisabled();
  });
});
