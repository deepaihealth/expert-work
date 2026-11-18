/**
 * AgentTemplateConfigForm — B-315: while the embedded ManifestEditor reports
 * form errors (here a duplicated output-schema field name), ``submit()``
 * refuses: no create / patch / spec update call, and a ``form_has_errors``
 * message. Once fixed, the same ``submit()`` goes through.
 */
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

vi.mock("@monaco-editor/react", () => ({
  default: ({ value }: { value?: string }) => <textarea data-testid="monaco-stub" value={value} readOnly />,
}));
vi.mock("../../../api/agent-templates", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../api/agent-templates")>()),
  createAgentTemplate: vi.fn(),
  patchTemplateMeta: vi.fn(),
  updateTemplateSpec: vi.fn(),
}));

import * as schemaSdk from "../../../api/manifest_schema";
import {
  createAgentTemplate,
  patchTemplateMeta,
  updateTemplateSpec,
  type AgentTemplate,
} from "../../../api/agent-templates";
import { __resetSchemaCacheForTest } from "../../manifest-editor/schema";
import {
  AgentTemplateConfigForm,
  type AgentTemplateConfigFormHandle,
} from "../AgentTemplateConfigForm";

const TEMPLATE = {
  id: "t1",
  tenant_id: null,
  name: "tpl",
  version: "1.0.0",
  spec: {
    apiVersion: "expert_work.io/v1",
    kind: "Agent",
    metadata: { name: "tpl", version: "1.0.0" },
    spec: {
      output_schema: {
        json_schema: { type: "object", properties: { a: { type: "string" }, b: { type: "string" } } },
      },
    },
  },
  spec_sha256: "s".repeat(64),
  display_name: "Template",
  description: "",
  category: "general",
  icon: null,
  required_tier: "free",
  status: "draft",
  enabled: true,
  created_by: "u",
  created_at: "2026-11-18T00:00:00Z",
  updated_at: "2026-11-18T00:00:00Z",
} as unknown as AgentTemplate;

beforeEach(() => {
  __resetSchemaCacheForTest();
  vi.spyOn(schemaSdk, "fetchAgentSchema").mockResolvedValue({ type: "object" });
  vi.mocked(patchTemplateMeta).mockResolvedValue(TEMPLATE);
  vi.mocked(updateTemplateSpec).mockResolvedValue(TEMPLATE);
  vi.mocked(createAgentTemplate).mockResolvedValue(TEMPLATE);
});
afterEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("AgentTemplateConfigForm submit with form errors", () => {
  it("refuses while an output-schema name is duplicated, goes through once fixed", async () => {
    const user = userEvent.setup();
    const ref = createRef<AgentTemplateConfigFormHandle>();
    const onSaved = vi.fn();
    render(
      <App>
        <AgentTemplateConfigForm ref={ref} editing={TEMPLATE} onSaved={onSaved} />
      </App>,
    );
    await user.click(await screen.findByTestId("cfg-nav-prompt"));
    const name = await screen.findByTestId("af-output-schema-name-1");
    await user.clear(name);
    await user.type(name, "a");

    await act(() => ref.current!.submit());
    expect(createAgentTemplate).not.toHaveBeenCalled();
    expect(patchTemplateMeta).not.toHaveBeenCalled();
    expect(updateTemplateSpec).not.toHaveBeenCalled();
    expect(await screen.findByText(i18n.t("manifest_editor.form_has_errors"))).toBeInTheDocument();

    await user.type(name, "2");
    await act(() => ref.current!.submit());
    await waitFor(() => expect(updateTemplateSpec).toHaveBeenCalledTimes(1));
    expect(onSaved).toHaveBeenCalledTimes(1);
  });
});
