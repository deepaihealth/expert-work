/**
 * OutputSchemaEditor — top-level basics (the flat cases this widget started
 * with; nested / tree cases: OutputSchemaEditor.tree.test.tsx; the name
 * rule end to end: OutputSchemaEditor.names.test.tsx). Top-level rows keep
 * the old test ids (path "0", "1" …). Contract under test:
 *  - off by default; switching on writes the empty-object default schema.
 *  - every edit writes the exact ``setOutputSchemaTree``-shaped manifest.
 *  - B-315 name rule: while any field name is blank, invalid or duplicated,
 *    nothing is written back — ``formData`` keeps its last valid state and
 *    every offending row is flagged (both rows of a duplicate). Fixing the
 *    names writes the whole tree, including edits made in the meantime.
 *    Nothing is ever picked, pruned or dropped automatically.
 *  - a new ``formData`` whose ``json_schema`` equals what the widget last
 *    wrote (our own echo, or a sibling field's edit) is ignored, so local
 *    edits (an unnamed new row and its typed text) survive it.
 *  - a root the form can't show at all renders read-only: the Switch is
 *    hidden, only an info Alert shows, and nothing calls ``onChange``.
 *  - turning off with existing fields confirms first (App.useApp() modal);
 *    cancelling applies nothing.
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { OutputSchemaEditor } from "../OutputSchemaEditor";
import type { AgentManifest } from "../../form_model";

function renderEditor(
  formData: unknown = { spec: {} },
  onChange: (d: unknown) => void = vi.fn(),
) {
  // The Switch's off-with-existing-fields confirm rides App.useApp()'s modal
  // — the antd <App> provider is part of the component's contract (same
  // wrapping the real app root provides; mirrors BasicSection's RunProfileCard).
  return render(
    <App>
      <OutputSchemaEditor formData={formData} onChange={onChange} />
    </App>,
  );
}

// A real controlled wrapper (state feeds back into ``formData``) — needed for
// multi-step flows so the editor's own resync-from-props effect is exercised
// the same way it is under the real ``ManifestEditor`` parent, rather than
// relying solely on the widget's local row state to carry the UI forward.
function renderControlled(initial: unknown, onChangeSpy: (d: unknown) => void) {
  function Harness() {
    const [data, setData] = useState<unknown>(initial);
    const handleChange = (next: unknown): void => {
      setData(next);
      onChangeSpy(next);
    };
    return (
      <App>
        <OutputSchemaEditor formData={data} onChange={handleChange} />
      </App>
    );
  }
  return render(<Harness />);
}

afterEach(() => {
  // the confirm dialog portals outside the RTL container.
  document.body.innerHTML = "";
});

describe("OutputSchemaEditor", () => {
  it("renders off with the hint when not configured", () => {
    renderEditor();
    expect(screen.getByTestId("af-output-schema-switch")).not.toBeChecked();
    expect(screen.queryByTestId("af-output-schema-row-0")).not.toBeInTheDocument();
  });

  it("turning the switch on writes the empty-object default schema", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderEditor({ spec: {} }, onChange);
    await user.click(screen.getByTestId("af-output-schema-switch"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.output_schema).toEqual({
      json_schema: { type: "object", properties: {}, additionalProperties: false },
    });
  });

  it("turning the switch off with no fields deletes the block without confirming", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          json_schema: { type: "object", properties: {}, additionalProperties: false },
        },
      },
    };
    renderEditor(seeded, onChange);
    await user.click(screen.getByTestId("af-output-schema-switch"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.output_schema).toBeUndefined();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("adding two fields and naming them produces the exact json_schema shape", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderControlled({ spec: {} }, onChange);

    // turn on
    await user.click(screen.getByTestId("af-output-schema-switch"));

    // add field 1, name it "title", mark required
    await user.click(screen.getByTestId("af-output-schema-add"));
    await user.clear(screen.getByTestId("af-output-schema-name-0"));
    await user.type(screen.getByTestId("af-output-schema-name-0"), "title");
    await user.click(screen.getByTestId("af-output-schema-required-0"));

    // add field 2, name it "count" (default type: string)
    await user.click(screen.getByTestId("af-output-schema-add"));
    await user.clear(screen.getByTestId("af-output-schema-name-1"));
    await user.type(screen.getByTestId("af-output-schema-name-1"), "count");

    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.output_schema?.json_schema).toEqual({
      type: "object",
      properties: {
        title: { type: "string" },
        count: { type: "string" },
      },
      required: ["title"],
      additionalProperties: false,
    });
  });

  it("an invalid field name writes nothing back and shows an error state", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          json_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderEditor(seeded, onChange);
    const nameInput = screen.getByTestId("af-output-schema-name-0");
    await user.clear(nameInput);
    await user.type(nameInput, "1bad");
    expect(onChange).not.toHaveBeenCalled();
    expect(nameInput).toHaveValue("1bad");
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("af-output-schema-name-errors")).toBeInTheDocument();
  });

  it("removing the invalid-name row un-blocks (no leftover invalid field)", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          json_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderEditor(seeded, onChange);
    await user.clear(screen.getByTestId("af-output-schema-name-0"));
    onChange.mockClear();
    await user.click(screen.getByTestId("af-output-schema-remove-0"));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.output_schema?.json_schema).toEqual({
      type: "object",
      properties: {},
      additionalProperties: false,
    });
  });

  describe("name problems pause write-back; nothing is dropped", () => {
    it("edits to a valid row wait while another row's name is invalid, then land together", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      const seeded = {
        spec: {
          output_schema: {
            json_schema: {
              type: "object",
              properties: { title: { type: "string" } },
              additionalProperties: false,
            },
          },
        },
      };
      renderEditor(seeded, onChange);
      await user.click(screen.getByTestId("af-output-schema-add"));
      await user.clear(screen.getByTestId("af-output-schema-name-1"));
      onChange.mockClear();
      await user.type(screen.getByTestId("af-output-schema-desc-0"), "hi");
      await user.click(screen.getByTestId("af-output-schema-required-0"));
      expect(onChange).not.toHaveBeenCalled();

      // The first keystroke makes every name valid: that write carries all of it.
      await user.type(screen.getByTestId("af-output-schema-name-1"), "c");
      expect(onChange).toHaveBeenCalledTimes(1);
      await user.type(screen.getByTestId("af-output-schema-name-1"), "ount");
      expect((onChange.mock.calls.at(-1)?.[0] as AgentManifest).spec?.output_schema?.json_schema).toEqual({
        type: "object",
        properties: { title: { type: "string", description: "hi" }, count: { type: "string" } },
        additionalProperties: false,
        required: ["title"],
      });
    });

    it("a sibling field's edit (new formData reference, unchanged output_schema) preserves an in-progress WIP row untouched", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      const seeded = {
        spec: {
          description: "v1",
          output_schema: {
            json_schema: {
              type: "object",
              properties: { title: { type: "string" } },
              additionalProperties: false,
            },
          },
        },
      };
      const { rerender } = renderEditor(seeded, onChange);

      // Add a row (written as field_1), then blank its name and type into its
      // description — with the name broken, those edits stay local only.
      await user.click(screen.getByTestId("af-output-schema-add"));
      const written = onChange.mock.calls.at(-1)?.[0] as typeof seeded;
      await user.clear(screen.getByTestId("af-output-schema-name-1"));
      await user.type(screen.getByTestId("af-output-schema-desc-1"), "wip notes");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("");

      // Simulate a SIBLING control (e.g. the system-prompt textarea in the
      // same FormView section) producing a brand-new formData object whose
      // output_schema content is byte-identical to what's already synced —
      // exactly the "any formData change" trigger that used to blow away
      // local state wholesale.
      const siblingEdited = {
        ...written,
        spec: { ...written.spec, description: "v2" },
      };
      rerender(
        <App>
          <OutputSchemaEditor formData={siblingEdited} onChange={onChange} />
        </App>,
      );

      // The WIP row and its already-typed text survive the resync.
      expect(screen.getByTestId("af-output-schema-row-1")).toBeInTheDocument();
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("");
      expect(screen.getByTestId("af-output-schema-desc-1")).toHaveValue(
        "wip notes",
      );
    });

    it("a transiently duplicate name flags both rows and writes nothing until it resolves (typing title2 next to title)", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      const seeded = {
        spec: {
          output_schema: {
            json_schema: {
              type: "object",
              properties: { title: { type: "integer", description: "keep me" } },
              additionalProperties: false,
            },
          },
        },
      };
      renderControlled(seeded, onChange);

      await user.click(screen.getByTestId("af-output-schema-add"));
      const nameInput = screen.getByTestId("af-output-schema-name-1");
      await user.clear(nameInput);
      await user.type(nameInput, "titl");
      onChange.mockClear();
      await user.type(nameInput, "e");

      expect(nameInput).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByTestId("af-output-schema-name-0")).toHaveAttribute("aria-invalid", "true");
      expect(screen.getAllByText(i18n.t("agent_form.output_schema.name_duplicate"))).toHaveLength(2);
      expect(onChange).not.toHaveBeenCalled();

      await user.type(nameInput, "2");
      expect(nameInput).toHaveAttribute("aria-invalid", "false");
      expect(screen.queryByText(i18n.t("agent_form.output_schema.name_duplicate"))).not.toBeInTheDocument();
      expect(onChange).toHaveBeenCalledTimes(1);
      const last = onChange.mock.calls[0][0] as AgentManifest;
      expect(last.spec?.output_schema?.json_schema).toEqual({
        type: "object",
        properties: {
          title: { type: "integer", description: "keep me" },
          title2: { type: "string" },
        },
        additionalProperties: false,
      });
    });
  });

  it("turning off with existing fields confirms first; OK deletes the block", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          json_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderEditor(seeded, onChange);
    await user.click(screen.getByTestId("af-output-schema-switch"));
    expect(onChange).not.toHaveBeenCalled();

    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "OK" }));
    expect(onChange).toHaveBeenCalledTimes(1);
    const next = onChange.mock.calls[0][0] as AgentManifest;
    expect(next.spec?.output_schema).toBeUndefined();
  });

  it("cancelling the off-confirm applies nothing", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          json_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderEditor(seeded, onChange);
    await user.click(screen.getByTestId("af-output-schema-switch"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it("preserves an existing name/strict when editing rows", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const seeded = {
      spec: {
        output_schema: {
          name: "custom",
          strict: false,
          json_schema: {
            type: "object",
            properties: { title: { type: "string" } },
            additionalProperties: false,
          },
        },
      },
    };
    renderEditor(seeded, onChange);
    await user.type(screen.getByTestId("af-output-schema-desc-0"), "hi");
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.output_schema?.name).toBe("custom");
    expect(last.spec?.output_schema?.strict).toBe(false);
  });

  describe("root the form can't show (root-level oneOf)", () => {
    const seeded = {
      spec: {
        output_schema: {
          json_schema: { oneOf: [{ type: "object" }, { type: "string" }] },
        },
      },
    };

    it("shows a read-only alert and hides the switch", () => {
      renderEditor(seeded);
      expect(screen.getByTestId("af-output-schema-readonly")).toBeInTheDocument();
      expect(
        screen.queryByTestId("af-output-schema-switch"),
      ).not.toBeInTheDocument();
    });

    it("has nothing clickable that produces onChange (mutation-proof)", async () => {
      const user = userEvent.setup();
      const onChange = vi.fn();
      renderEditor(seeded, onChange);
      const alert = screen.getByTestId("af-output-schema-readonly");
      await user.click(alert);
      expect(onChange).not.toHaveBeenCalled();
      expect(screen.queryByTestId("af-output-schema-add")).not.toBeInTheDocument();
      expect(screen.queryByTestId("af-output-schema-row-0")).not.toBeInTheDocument();
    });
  });
});
