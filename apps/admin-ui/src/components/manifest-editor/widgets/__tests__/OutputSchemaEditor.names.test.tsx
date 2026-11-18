/**
 * OutputSchemaEditor — the B-315 field-name rule, end to end in the widget:
 * while any name is blank, invalid or duplicated (also against a readonly
 * field), nothing is written back (``formData`` stays at its last valid
 * state), every offending row is flagged (both rows of a duplicate), a
 * banner explains it, and the widget reports the error up through
 * ``FormErrorsContext`` so the host blocks saving. Once all names are fixed,
 * one write carries the whole tree — no field ever silently disappears.
 * Controlled harness: ``formData`` feeds back exactly as under ManifestEditor.
 */
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../../../i18n";

import { OutputSchemaEditor } from "../OutputSchemaEditor";
import { FormErrorsContext } from "../../form_errors";
import type { AgentManifest } from "../../form_model";

function renderNames(schema: unknown) {
  const onChange = vi.fn<(next: unknown) => void>();
  const report = vi.fn<(source: string, hasErrors: boolean) => void>();
  function Harness() {
    const [data, setData] = useState<unknown>({ spec: { output_schema: { json_schema: schema } } });
    return (
      <FormErrorsContext.Provider value={report}>
        <App>
          <OutputSchemaEditor
            formData={data}
            onChange={(next) => {
              setData(next);
              onChange(next);
            }}
          />
        </App>
      </FormErrorsContext.Provider>
    );
  }
  render(<Harness />);
  return { onChange, report };
}

const written = (call: unknown[]): Record<string, unknown> =>
  ((call[0] as AgentManifest).spec?.output_schema?.json_schema as { properties: Record<string, unknown> })
    .properties;
const lastReport = (report: { mock: { calls: unknown[][] } }): unknown => report.mock.calls.at(-1)?.[1];
const invalid = (path: string): string | null =>
  screen.getByTestId(`af-output-schema-name-${path}`).getAttribute("aria-invalid");

async function rename(user: ReturnType<typeof userEvent.setup>, path: string, name: string) {
  const input = screen.getByTestId(`af-output-schema-name-${path}`);
  await user.clear(input);
  if (name !== "") await user.type(input, name);
}

const A = { type: "string", description: "A" };
const B = { type: "integer", description: "B" };
const C = { type: "boolean", description: "C" };

afterEach(() => {
  document.body.innerHTML = "";
});

describe("OutputSchemaEditor name rule", () => {
  it("swapping two names: blocked and both rows flagged mid-way, then one write with the names exchanged", async () => {
    const user = userEvent.setup();
    const { onChange, report } = renderNames({ type: "object", properties: { a: A, b: B } });

    await rename(user, "0", "b");
    expect([invalid("0"), invalid("1")]).toEqual(["true", "true"]);
    expect(screen.getByTestId("af-output-schema-name-errors")).toBeInTheDocument();
    expect(lastReport(report)).toBe(true);
    expect(onChange).not.toHaveBeenCalled();

    await rename(user, "1", "a");
    expect([invalid("0"), invalid("1")]).toEqual(["false", "false"]);
    expect(lastReport(report)).toBe(false);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(written(onChange.mock.calls[0]))).toBe(JSON.stringify({ b: A, a: B }));
  });

  it("b → x, a → x, c → c2: no field ever disappears; formData waits until the names are fixed", async () => {
    const user = userEvent.setup();
    const { onChange, report } = renderNames({ type: "object", properties: { a: A, b: B, c: C } });

    await rename(user, "1", "x");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(written(onChange.mock.calls[0])).toEqual({ a: A, x: B, c: C });

    await rename(user, "0", "x");
    expect([invalid("0"), invalid("1")]).toEqual(["true", "true"]);
    await user.type(screen.getByTestId("af-output-schema-name-2"), "2");
    expect(screen.getByTestId("af-output-schema-name-2")).toHaveValue("c2");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(lastReport(report)).toBe(true);

    await rename(user, "0", "y");
    expect(lastReport(report)).toBe(false);
    expect(JSON.stringify(written(onChange.mock.calls.at(-1) as unknown[]))).toBe(
      JSON.stringify({ y: A, x: B, c2: C }),
    );
    for (const call of onChange.mock.calls) expect(Object.keys(written(call))).toHaveLength(3);
  });

  // [label, a valid prefix (written as usual), the rest that breaks the name]
  it.each([
    ["blank", "", ""],
    ["breaks the name rule", "has", "-dash"],
    ["__proto__", "__proto_", "_"],
  ])("a %s name blocks: flagged, reported, nothing written", async (_label, prefix, rest) => {
    const user = userEvent.setup();
    const { onChange, report } = renderNames({ type: "object", properties: { a: A } });
    if (prefix !== "") await rename(user, "0", prefix);
    onChange.mockClear();
    const input = screen.getByTestId("af-output-schema-name-0");
    if (rest === "") await user.clear(input);
    else await user.type(input, rest);
    expect(invalid("0")).toBe("true");
    expect(lastReport(report)).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("a clash with a readonly field blocks too, and flags the readonly row", async () => {
    const user = userEvent.setup();
    const choice = { oneOf: [{ type: "string" }, { type: "integer" }] };
    const { onChange, report } = renderNames({ type: "object", properties: { a: A, r: choice } });
    await rename(user, "0", "r");
    expect([invalid("0"), invalid("1")]).toEqual(["true", "true"]);
    expect(lastReport(report)).toBe(true);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("other edits made while a name is broken are held, then written together", async () => {
    const user = userEvent.setup();
    const { onChange } = renderNames({ type: "object", properties: { a: A, b: { type: "string" } } });
    await rename(user, "0", "");
    await user.type(screen.getByTestId("af-output-schema-desc-1"), "note");
    await user.click(screen.getByTestId("af-output-schema-required-1"));
    await user.click(screen.getByTestId("af-output-schema-limits-1"));
    await user.type(await screen.findByTestId("af-output-schema-limit-maxLength-1"), "9");
    expect(onChange).not.toHaveBeenCalled();

    await user.type(screen.getByTestId("af-output-schema-name-0"), "z");
    expect(onChange).toHaveBeenCalledTimes(1);
    expect((onChange.mock.calls[0][0] as AgentManifest).spec?.output_schema?.json_schema).toEqual({
      type: "object",
      properties: { z: A, b: { type: "string", description: "note", maxLength: 9 } },
      required: ["b"],
    });
  });

  it("adding a field gives it the first free default name and keeps writing back", async () => {
    const user = userEvent.setup();
    const { onChange, report } = renderNames({
      type: "object",
      properties: { field_1: A, box: { type: "object", properties: {} } },
    });
    await user.click(screen.getByTestId("af-output-schema-add"));
    expect(screen.getByTestId("af-output-schema-name-2")).toHaveValue("field_2");
    await user.click(screen.getByTestId("af-output-schema-add"));
    expect(screen.getByTestId("af-output-schema-name-3")).toHaveValue("field_3");
    await user.click(screen.getByTestId("af-output-schema-remove-0"));
    await user.click(screen.getByTestId("af-output-schema-add"));
    expect(screen.getByTestId("af-output-schema-name-3")).toHaveValue("field_1");
    await user.click(screen.getByTestId("af-output-schema-add-child-0"));
    expect(screen.getByTestId("af-output-schema-name-0.0")).toHaveValue("field_1");

    expect(screen.queryByTestId("af-output-schema-name-errors")).not.toBeInTheDocument();
    expect(report).not.toHaveBeenCalledWith("output_schema", true);
    expect(onChange).toHaveBeenCalledTimes(5);
    expect(Object.keys(written(onChange.mock.calls.at(-1) as unknown[]))).toEqual([
      "box",
      "field_2",
      "field_3",
      "field_1",
    ]);
  });

  it("a manifest adopted from outside replaces paused edits even when its json_schema content is the same", async () => {
    // e.g. "discard draft" where draft and live share the schema: ManifestEditor
    // adopts a freshly parsed manifest — a new json_schema object, same content.
    const user = userEvent.setup();
    const report = vi.fn<(source: string, hasErrors: boolean) => void>();
    const schema = { type: "object", properties: { a: A, b: B } };
    const fresh = () => ({ spec: { output_schema: { json_schema: structuredClone(schema) } } });
    const ui = (formData: unknown) => (
      <FormErrorsContext.Provider value={report}>
        <App>
          <OutputSchemaEditor formData={formData} onChange={vi.fn()} />
        </App>
      </FormErrorsContext.Provider>
    );
    const { rerender } = render(ui(fresh()));
    await rename(user, "1", "a");
    expect(lastReport(report)).toBe(true);

    rerender(ui(fresh()));
    expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("b");
    expect(invalid("1")).toBe("false");
    expect(screen.queryByTestId("af-output-schema-name-errors")).not.toBeInTheDocument();
    expect(lastReport(report)).toBe(false);
  });
});
