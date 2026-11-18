/**
 * OutputSchemaEditor — config-page redesign v2 Task 7 (flat rows), now the
 * B-315 field tree (nested cases: OutputSchemaEditor.tree.test.tsx). Contract
 * under test (top-level rows keep the old test ids, path "0", "1" …):
 *  - off by default, on writes the empty-object default schema.
 *  - editing rows emits the exact ``setOutputSchemaRows``-shaped manifest.
 *  - PER-ROW commit, no whole-table gate (review-fixed Important): a row
 *    whose name is currently invalid is excluded from the write and shown
 *    with an error state, but every OTHER row's own edit still lands in the
 *    manifest immediately — one bad/blank row never blocks unrelated edits.
 *  - a resync from a genuinely new ``formData`` (not our own echo) preserves
 *    any local WIP (invalid-name) row untouched — including already-typed
 *    text in its other fields — rather than clobbering it.
 *  - a name that duplicates an EARLIER row's is locally invalid too
 *    (review-fixed Important #2, transient-collision data loss): the later
 *    row is excluded from the write (duplicate-specific error message), so
 *    typing "title2" next to an existing "title" never last-wins-overwrites
 *    the seeded property's type/description at the "title" keystroke.
 *  - a root the form can't show at all renders read-only: the Switch is
 *    hidden, only an info Alert shows, and NOTHING in that state ever calls
 *    ``onChange`` (there is nothing interactive to click).
 *  - turning off with existing fields confirms first (App.useApp() modal,
 *    not the static Modal.confirm — renders under test); cancelling applies
 *    nothing.
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
    await user.type(screen.getByTestId("af-output-schema-name-0"), "title");
    await user.click(screen.getByTestId("af-output-schema-required-0"));

    // add field 2, name it "count" (default type: string)
    await user.click(screen.getByTestId("af-output-schema-add"));
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

  it("an invalid field name is excluded from the write and shows an error state (review-fixed: no whole-table gate)", async () => {
    // Contract change from the pre-fix version: that gated the ENTIRE
    // commit (no onChange at all) on every row's name being valid, which is
    // exactly the Important bug this test now documents the fix for — one
    // invalid row (here, the ONLY row) must not block a write; it's simply
    // excluded, so the manifest's `properties` reflects "no valid rows"
    // rather than going stale/frozen.
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
    onChange.mockClear();
    await user.type(nameInput, "1bad");
    expect(onChange).toHaveBeenCalled();
    const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(last.spec?.output_schema?.json_schema).toEqual({
      type: "object",
      properties: {},
      additionalProperties: false,
    });
    expect(nameInput).toHaveValue("1bad");
    expect(nameInput).toHaveAttribute("aria-invalid", "true");
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

  describe("per-row commit (review-fixed Important: no whole-table gate + no clobber-on-resync)", () => {
    it("editing a valid row while another row's name is invalid commits immediately with just the valid subset", async () => {
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
      await user.type(screen.getByTestId("af-output-schema-name-1"), "1bad");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveAttribute(
        "aria-invalid",
        "true",
      );

      await user.type(screen.getByTestId("af-output-schema-desc-0"), "hi");

      const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
      // Only "title" (the valid row) made it into the write; "1bad" is
      // excluded — one invalid row never blocks another row's edit.
      expect(last.spec?.output_schema?.json_schema).toEqual({
        type: "object",
        properties: { title: { type: "string", description: "hi" } },
        additionalProperties: false,
      });
    });

    it("adding a field (blank row) does not block other rows' edits from committing", async () => {
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
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("");

      onChange.mockClear();
      await user.click(screen.getByTestId("af-output-schema-required-0"));

      expect(onChange).toHaveBeenCalled();
      const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
      // The freshly-added blank row (index 1, name "") is excluded — but
      // "title"'s own edit still landed, immediately.
      expect(last.spec?.output_schema?.json_schema).toEqual({
        type: "object",
        properties: { title: { type: "string" } },
        required: ["title"],
        additionalProperties: false,
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

      // Add a blank row and type into its description — its name stays ""
      // (invalid), so this row never reaches the manifest; it only lives in
      // local overlay state.
      await user.click(screen.getByTestId("af-output-schema-add"));
      await user.type(screen.getByTestId("af-output-schema-desc-1"), "wip notes");
      expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("");

      // Simulate a SIBLING control (e.g. the system-prompt textarea in the
      // same FormView section) producing a brand-new formData object whose
      // output_schema content is byte-identical to what's already synced —
      // exactly the "any formData change" trigger that used to blow away
      // local state wholesale.
      const siblingEdited = {
        ...seeded,
        spec: { ...seeded.spec, description: "v2" },
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

    it("a transiently duplicate name never clobbers the earlier row's property (typing title2 next to seeded title)", async () => {
      // Review-fixed Important #2. Pre-fix repro: at the 5th keystroke of
      // "title2" the new row's name is exactly "title" — pattern-valid — so
      // BOTH rows entered the write; ``properties`` being a record, the
      // later row last-wins-overwrote the seeded title (integer + "keep me"
      // → bare {type:"string"}), and the collapsed 1-row readback no longer
      // matched lastWrittenRef's 2 written rows, so the echo check misfired
      // into the external-resync branch and the typing row itself vanished
      // mid-word. Controlled harness on purpose — the echo/resync half only
      // exists when formData feeds back.
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
      await user.type(nameInput, "title");

      // Mid-collision: the later row is locally invalid — duplicate-specific
      // message, error state, still holding the typed text.
      expect(nameInput).toHaveValue("title");
      expect(nameInput).toHaveAttribute("aria-invalid", "true");
      expect(
        screen.getByText(i18n.t("agent_form.output_schema.name_duplicate")),
      ).toBeInTheDocument();
      // The EARLIER row is not the one flagged (first occurrence wins).
      expect(screen.getByTestId("af-output-schema-name-0")).toHaveAttribute(
        "aria-invalid",
        "false",
      );

      // EVERY write so far — add-click and each keystroke — preserved the
      // seeded property byte-for-byte (the mutation-level core: pre-fix, the
      // "title" keystroke's write destroyed it).
      expect(onChange).toHaveBeenCalled();
      for (const call of onChange.mock.calls) {
        const m = call[0] as AgentManifest;
        const props = m.spec?.output_schema?.json_schema?.properties as Record<
          string,
          unknown
        >;
        expect(props.title).toEqual({ type: "integer", description: "keep me" });
      }

      // Finish the word — "title2" no longer collides: both rows land.
      await user.type(nameInput, "2");
      expect(nameInput).toHaveAttribute("aria-invalid", "false");
      expect(
        screen.queryByText(i18n.t("agent_form.output_schema.name_duplicate")),
      ).not.toBeInTheDocument();
      const last = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
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
