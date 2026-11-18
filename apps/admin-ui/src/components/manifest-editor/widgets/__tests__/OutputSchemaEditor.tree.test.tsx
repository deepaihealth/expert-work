/**
 * OutputSchemaEditor — B-315 field-tree behaviour (top-level basics live in
 * OutputSchemaEditor.test.tsx):
 *  - structure: the fixed "each item" row, collapse, add sub-field, delete,
 *    type change clearing limits that no longer apply, the depth-5 limit;
 *  - Limits popover: editing limits / allowed values, hints (min > max,
 *    rejected values, ``uniqueItems: false`` not counted), and that a mere
 *    focus + blur never rewrites a stored value;
 *  - readonly nodes: only "required" and remove; raw kept on sibling edits;
 *    the depth-specific note;
 *  - names: duplicates, renames that collide with untouched or readonly
 *    siblings, the field renamed last yielding, a full swap;
 *  - identity: collapse state follows its field (uids), survives an
 *    external re-read by name path, and new uids never clash;
 *  - the strict-mode reminder.
 * Controlled harness unless noted, so the echo/resync path is exercised.
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

    it("greyed with the YAML note; only 'required' (a parent setting) is editable", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderControlled(withSchema(schema), onChange);
      const row = screen.getByTestId("af-output-schema-row-0");
      expect(within(row).getByText(i18n.t("agent_form.output_schema.readonly_node"))).toBeInTheDocument();
      expect(within(row).queryByRole("textbox")).not.toBeInTheDocument();
      expect(within(row).queryByRole("combobox")).not.toBeInTheDocument();
      expect(within(row).getAllByRole("checkbox")).toHaveLength(1);
      expect(screen.queryByTestId("af-output-schema-limits-0")).not.toBeInTheDocument();
      expect(screen.getByTestId("af-output-schema-name-1")).toBeEnabled();

      await user.click(screen.getByTestId("af-output-schema-required-0"));
      expect(lastSchema(onChange)).toEqual({ ...schema, required: [] });
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

  it("renaming onto a readonly sibling flags the renamed row and leaves the original untouched", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const choice = { oneOf: [{ type: "string" }, { type: "integer" }] };
    renderControlled(
      withSchema({
        type: "object",
        properties: { a: { type: "string", description: "keep me" }, b: choice },
        required: ["a"],
      }),
      onChange,
    );
    const name = screen.getByTestId("af-output-schema-name-0");
    await user.clear(name);
    await user.type(name, "b");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(i18n.t("agent_form.output_schema.name_duplicate"))).toBeInTheDocument();
    // "a" is out of the write while its name collides; "b" is exactly the original.
    expect(lastSchema(onChange)).toEqual({ type: "object", properties: { b: choice }, required: [] });
  });

  it("renaming onto a later untouched sibling flags the renamed row, the sibling keeps its subtree", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const b = { type: "object", properties: { x: { type: "integer" } }, required: ["x"] };
    renderControlled(withSchema({ type: "object", properties: { a: { type: "string" }, b } }), onChange);
    const name = screen.getByTestId("af-output-schema-name-0");
    await user.clear(name);
    await user.type(name, "b");
    expect(name).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("af-output-schema-name-1")).toHaveAttribute("aria-invalid", "false");
    expect(lastSchema(onChange)).toEqual({ type: "object", properties: { b } });
  });

  it("collapse state follows its field when an earlier field is removed", async () => {
    const user = userEvent.setup();
    renderControlled(
      withSchema({
        type: "object",
        properties: {
          a: { type: "array", items: { type: "string" } },
          b: { type: "array", items: { type: "integer" } },
        },
      }),
    );
    await user.click(screen.getByTestId("af-output-schema-toggle-1"));
    expect(screen.queryByTestId("af-output-schema-row-1.items")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-row-0.items")).toBeInTheDocument();

    await user.click(screen.getByTestId("af-output-schema-remove-0"));
    expect(screen.getByTestId("af-output-schema-name-0")).toHaveValue("b");
    expect(screen.queryByTestId("af-output-schema-row-0.items")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-toggle-0")).toHaveTextContent("+");
  });

  it("re-reads the tree when json_schema really changes from outside", () => {
    const first = withSchema({ type: "object", properties: { old: { type: "string" } } });
    const second = withSchema({ type: "object", properties: { fresh: { type: "integer" } } });
    const { rerender } = render(
      <App>
        <OutputSchemaEditor formData={first} onChange={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("af-output-schema-name-0")).toHaveValue("old");
    rerender(
      <App>
        <OutputSchemaEditor formData={second} onChange={vi.fn()} />
      </App>,
    );
    expect(screen.getByTestId("af-output-schema-name-0")).toHaveValue("fresh");
  });

  describe("limit hints", () => {
    it("uniqueItems: false is not counted as a limit", () => {
      renderControlled(
        withSchema({
          type: "object",
          properties: { tags: { type: "array", uniqueItems: false, maxItems: 2, items: { type: "string" } } },
        }),
      );
      expect(screen.getByTestId("af-output-schema-limits-0")).toHaveTextContent(/\(1\)$/);
    });

    it("warns, in the row and the panel, when a minimum is above its maximum", async () => {
      const user = userEvent.setup();
      renderControlled(
        withSchema({
          type: "object",
          properties: { bad: { type: "string", minLength: 5, maxLength: 2 }, ok: { type: "string", minLength: 1, maxLength: 2 } },
        }),
      );
      expect(screen.getByTestId("af-output-schema-row-range-warn-0")).toBeInTheDocument();
      expect(screen.queryByTestId("af-output-schema-row-range-warn-1")).not.toBeInTheDocument();
      await user.click(screen.getByTestId("af-output-schema-limits-0"));
      expect(await screen.findByTestId("af-output-schema-range-warn-0")).toBeInTheDocument();
    });

    it("says which allowed values were dropped for an integer field", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderControlled(withSchema({ type: "object", properties: { n: { type: "integer" } } }), onChange);
      await user.click(screen.getByTestId("af-output-schema-limits-0"));
      const box = within(await screen.findByTestId("af-output-schema-enum-0")).getByRole("combobox");
      await user.type(box, "1,x,");
      expect(screen.getByTestId("af-output-schema-enum-rejected-0")).toHaveTextContent(/: x$/);
      await user.type(box, "2.5,");
      expect(screen.getByTestId("af-output-schema-enum-rejected-0")).toHaveTextContent(/: 2\.5$/);
      expect(lastSchema(onChange)).toEqual({ type: "object", properties: { n: { type: "integer", enum: [1] } } });
      await user.type(box, "3,");
      expect(screen.queryByTestId("af-output-schema-enum-rejected-0")).not.toBeInTheDocument();
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

  it("focusing and leaving a fractional integer bound changes nothing", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const schema = { type: "object", properties: { n: { type: "integer", minimum: 0.5, maximum: 2.5 } } };
    renderControlled(withSchema(schema), onChange);
    await user.click(screen.getByTestId("af-output-schema-limits-0"));
    await user.click(await screen.findByTestId("af-output-schema-limit-maximum-0"));
    await user.click(screen.getByTestId("af-output-schema-limit-minimum-0"));
    await user.click(document.body);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("af-output-schema-limit-maximum-0")).toHaveValue("2.5");
  });

  it("a node past level 5 says so in its readonly note", () => {
    const lvl = (inner: Record<string, unknown>) => ({ type: "object", properties: { n: inner } });
    renderControlled(withSchema(lvl(lvl(lvl(lvl(lvl(lvl({ type: "string" }))))))));
    expect(screen.getByTestId("af-output-schema-readonly-0.0.0.0.0.0")).toHaveTextContent(
      i18n.t("agent_form.output_schema.readonly_depth"),
    );
  });

  describe("renames: the field renamed last yields", () => {
    const A = { type: "string", description: "A" };
    const B = { type: "integer", description: "B" };

    async function rename(user: ReturnType<typeof userEvent.setup>, path: string, name: string) {
      const input = screen.getByTestId(`af-output-schema-name-${path}`);
      await user.clear(input);
      await user.type(input, name);
    }

    it("b → a, then a → c, then c back to a: the row typed last is flagged, b's row is kept", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderControlled(withSchema({ type: "object", properties: { a: A, b: B } }), onChange);
      await rename(user, "1", "a");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveAttribute("aria-invalid", "true");
      await rename(user, "0", "c");
      expect(lastSchema(onChange)).toEqual({ type: "object", properties: { c: A, a: B } });
      await rename(user, "0", "a");
      expect(screen.getByTestId("af-output-schema-name-0")).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveAttribute("aria-invalid", "false");
      expect(lastSchema(onChange)).toEqual({ type: "object", properties: { a: B } });
    });

    it("swapping two names ends with both fields, names exchanged", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderControlled(withSchema({ type: "object", properties: { a: A, b: B } }), onChange);
      await rename(user, "0", "b");
      expect(screen.getByTestId("af-output-schema-name-0")).toHaveAttribute("aria-invalid", "true");
      await rename(user, "1", "a");
      expect(screen.getByTestId("af-output-schema-name-0")).toHaveAttribute("aria-invalid", "false");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveAttribute("aria-invalid", "false");
      const written = lastSchema(onChange);
      expect(JSON.stringify(written)).toBe(JSON.stringify({ type: "object", properties: { b: A, a: B } }));
    });
  });

  describe("identity across an external re-read (uncontrolled)", () => {
    const list = (type: string) => ({ type: "array", items: { type } });
    const mount = (schema: unknown) =>
      render(
        <App>
          <OutputSchemaEditor formData={withSchema(schema)} onChange={vi.fn()} />
        </App>,
      );
    const remount = (rerender: (ui: React.ReactElement) => void, schema: unknown) =>
      rerender(
        <App>
          <OutputSchemaEditor formData={withSchema(schema)} onChange={vi.fn()} />
        </App>,
      );

    it("a collapsed field stays collapsed when it is still at the same name path", async () => {
      const user = userEvent.setup();
      const { rerender } = mount({ type: "object", properties: { y: list("string") } });
      await user.click(screen.getByTestId("af-output-schema-toggle-0"));
      remount(rerender, { type: "object", properties: { w: list("string"), y: list("string") } });
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("y");
      expect(screen.queryByTestId("af-output-schema-row-1.items")).not.toBeInTheDocument();
      expect(screen.getByTestId("af-output-schema-row-0.items")).toBeInTheDocument();
    });

    it("a field added after the re-read gets a uid of its own", async () => {
      const user = userEvent.setup();
      const { rerender } = mount({ type: "object", properties: { x: list("string") } });
      remount(rerender, { type: "object", properties: { y: list("string"), z: { type: "string" } } });
      await user.click(screen.getByTestId("af-output-schema-add"));
      await user.type(screen.getByTestId("af-output-schema-name-2"), "fresh");
      await pickType("af-output-schema-type-2", "array");
      await user.click(screen.getByTestId("af-output-schema-toggle-2"));
      expect(screen.queryByTestId("af-output-schema-row-2.items")).not.toBeInTheDocument();
      expect(screen.getByTestId("af-output-schema-row-0.items")).toBeInTheDocument();
    });
  });
});
