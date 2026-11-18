/**
 * ManifestEditor — B-315 form-errors plumbing: a widget's "can't save yet"
 * report becomes ``onValidityChange(true)``; while it holds, switching to the
 * YAML view or another group is refused with a notice (the switch would
 * unmount the widget and drop its un-applied edits); fixing it lifts the
 * block; unmounting the editor reports ``false``.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

vi.mock("@monaco-editor/react", () => ({
  default: ({ value }: { value?: string }) => <textarea data-testid="monaco-stub" value={value} readOnly />,
}));

import * as schemaSdk from "../../../api/manifest_schema";
import { __resetSchemaCacheForTest } from "../schema";
import { ManifestEditor } from "../ManifestEditor";

const SEED = `metadata:
  name: bot
spec:
  output_schema:
    json_schema:
      type: object
      properties:
        a: {type: string}
        b: {type: string}
`;

beforeEach(() => {
  __resetSchemaCacheForTest();
  vi.spyOn(schemaSdk, "fetchAgentSchema").mockResolvedValue({ type: "object" });
});
afterEach(() => vi.restoreAllMocks());

async function setup() {
  const user = userEvent.setup();
  const onValidityChange = vi.fn<(hasErrors: boolean) => void>();
  const view = render(
    <ManifestEditor mode="edit" initialYaml={SEED} onChange={vi.fn()} onValidityChange={onValidityChange} />,
  );
  await screen.findByTestId("af-basic");
  await user.click(screen.getByTestId("cfg-nav-prompt"));
  const name = await screen.findByTestId("af-output-schema-name-1");
  await user.clear(name);
  await user.type(name, "a"); // duplicates field "a"
  return { user, onValidityChange, name, view };
}

describe("ManifestEditor with a form widget reporting errors", () => {
  it("reports true, then false once fixed", async () => {
    const { user, onValidityChange, name } = await setup();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    await user.type(name, "2");
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
  });

  it("refuses the YAML view with a notice, and allows it once fixed", async () => {
    const { user, name } = await setup();
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("manifest-switch-blocked")).toBeInTheDocument();
    expect(screen.queryByTestId("manifest-yaml-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("a");

    await user.type(name, "2");
    expect(screen.queryByTestId("manifest-switch-blocked")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    expect(screen.getByTestId("manifest-yaml-view")).toBeInTheDocument();
  });

  it("refuses another group with a notice, and allows it once fixed", async () => {
    const { user, name } = await setup();
    await user.click(screen.getByTestId("cfg-nav-basic"));
    expect(screen.getByTestId("manifest-switch-blocked")).toBeInTheDocument();
    expect(screen.queryByTestId("af-basic")).not.toBeInTheDocument();
    expect(screen.getByTestId("af-output-schema-name-1")).toHaveValue("a");

    await user.type(name, "2");
    await user.click(screen.getByTestId("cfg-nav-basic"));
    expect(screen.getByTestId("af-basic")).toBeInTheDocument();
  });

  it("reports false when the editor unmounts", async () => {
    const { onValidityChange, view } = await setup();
    expect(onValidityChange).toHaveBeenLastCalledWith(true);
    view.unmount();
    expect(onValidityChange).toHaveBeenLastCalledWith(false);
  });
});
