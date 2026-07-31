/**
 * ManifestTab tests — visual-first config.
 *
 * The tab renders the visual <ManifestEditor> (form tabs + a raw YAML
 * escape-hatch) by default — no view/edit toggle. Monaco is mocked to a
 * textarea; the schema and model-catalog SDKs are stubbed because the tab
 * mounts ManifestEditor immediately.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import "../../i18n";

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
    options,
    ["data-testid"]: testId,
  }: {
    value?: string;
    onChange?: (v: string | undefined) => void;
    options?: { readOnly?: boolean };
    "data-testid"?: string;
  }) => (
    <textarea
      data-testid={testId ?? "monaco-stub"}
      readOnly={options?.readOnly}
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  return { default: Editor };
});

// Track C W2 — 切入态 hook;这些测试不挂 Provider,mock 成 home 态;
// ``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

import { ApiError } from "../../api/client";
import * as agentsSdk from "../../api/agents";
import * as schemaSdk from "../../api/manifest_schema";
import * as catalogSdk from "../../api/model_catalog";
import { __resetSchemaCacheForTest } from "../../components/manifest-editor/schema";
import { __resetCatalogCacheForTest } from "../../components/manifest-editor/catalog";
import { ManifestTab } from "../agent_detail/ManifestTab";
import type { AgentDetailResponse } from "../../api/agents";

const sampleDetail: AgentDetailResponse = {
  record: {
    id: "11111111-1111-1111-1111-111111111111",
    tenant_id: "22222222-2222-2222-2222-222222222222",
    name: "demo-agent",
    version: "1.0.0",
    status: "active",
    spec_sha256: "abc123def456abc123def456abc123def456abc123def456abc123def456abcd",
    created_by: "user-1",
    created_at: "2026-05-25T00:00:00Z",
    updated_at: "2026-05-25T00:00:00Z",
    spec: {
      apiVersion: "expert_work.io/v1",
      kind: "Agent",
      metadata: { name: "demo-agent", version: "1.0.0" },
      spec: { model: { provider: "anthropic", name: "claude-sonnet-4-6" } },
    },
  },
} as AgentDetailResponse;

// The server's post-save re-dump — same agent, but the spec came back
// coerced/re-serialised (different sha + model), the way a real save's quiet
// refetch delivers it (#2).
const savedDetail: AgentDetailResponse = {
  ...sampleDetail,
  record: {
    ...sampleDetail.record,
    spec_sha256: "f".repeat(64),
    spec: {
      apiVersion: "expert_work.io/v1",
      kind: "Agent",
      metadata: { name: "demo-agent", version: "1.0.0" },
      spec: { model: { provider: "anthropic", name: "claude-opus-4-5" } },
    },
  },
} as AgentDetailResponse;

const onSaved = vi.fn();
// Re-installed in beforeEach: afterEach() runs vi.restoreAllMocks(), which would
// otherwise permanently restore a module-level spy after the first test.
let updateAgentMock: ReturnType<typeof vi.spyOn>;

// ManifestTab persists the active config group in the URL (?group=), so it
// needs a router context (#2).
function renderTab(detail: AgentDetailResponse = sampleDetail) {
  return render(
    <MemoryRouter>
      <ManifestTab detail={detail} onSaved={onSaved} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  onSaved.mockClear();
  // vitest 4 的 restore 不复位 vi.fn 的 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
  updateAgentMock = vi.spyOn(agentsSdk, "updateAgent");
  __resetSchemaCacheForTest();
  __resetCatalogCacheForTest();
  vi.spyOn(schemaSdk, "fetchAgentSchema").mockResolvedValue({
    type: "object",
    properties: {
      metadata: { type: "object", properties: { name: { type: "string" } } },
      spec: { type: "object", properties: { model: { type: "object", properties: { provider: { type: "string" }, name: { type: "string" } } } } },
    },
  });
  vi.spyOn(catalogSdk, "fetchModelCatalog").mockResolvedValue({
    providers: [
      { provider: "anthropic", models: [{ name: "claude-sonnet-4-6", vision: true, embeddings: false, context_window: 200000, deprecated: false }] },
    ],
  });
});

afterEach(() => vi.restoreAllMocks());

describe("ManifestTab", () => {
  it("renders the visual ManifestEditor form by default (no view/edit toggle)", async () => {
    renderTab();
    await waitFor(() => expect(screen.getByTestId("manifest-editor-edit")).toBeInTheDocument());
    // Save + Reset are always present; there is no separate read-only mode.
    expect(screen.getByTestId("manifest-save-btn")).toBeInTheDocument();
    expect(screen.getByTestId("manifest-reset-btn")).toBeInTheDocument();
    expect(screen.queryByTestId("manifest-edit-btn")).not.toBeInTheDocument();
  });

  // Track C W2 — 切入态只读:保存是写操作,置灰;Reset 只回本地缓冲,不置灰。
  // 归属态可用由本文件其余 save 测试覆盖(两态断言)。
  it("切入态置灰保存按钮", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    renderTab();
    await waitFor(() => expect(screen.getByTestId("manifest-editor-edit")).toBeInTheDocument());
    expect(screen.getByTestId("manifest-save-btn")).toBeDisabled();
    expect(screen.getByTestId("manifest-reset-btn")).not.toBeDisabled();
  });

  it("exposes the raw YAML escape-hatch toggle", async () => {
    renderTab();
    await screen.findByTestId("manifest-editor-edit");
    expect(screen.getByTestId("cfg-yaml-toggle")).toBeInTheDocument();
  });

  it("saves edits via updateAgent and stays on the editor", async () => {
    const user = userEvent.setup();
    updateAgentMock.mockResolvedValue(sampleDetail);
    renderTab();
    await screen.findByTestId("manifest-editor-edit");
    // edit via the YAML toggle for a deterministic buffer
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "edited: yaml");
    await user.click(screen.getByTestId("manifest-save-btn"));
    await waitFor(() =>
      expect(updateAgentMock).toHaveBeenCalledWith("demo-agent", "1.0.0", { manifest_yaml: "edited: yaml" }),
    );
    expect(onSaved).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("manifest-editor-edit")).toBeInTheDocument();
  });

  it("surfaces an error alert when updateAgent rejects", async () => {
    const user = userEvent.setup();
    updateAgentMock.mockRejectedValue(new ApiError("name mismatch", "MANIFEST_PATH_MISMATCH", 422));
    renderTab();
    await screen.findByTestId("manifest-editor-edit");
    await user.click(screen.getByTestId("manifest-save-btn"));
    const alert = await screen.findByTestId("manifest-error");
    expect(alert).toHaveTextContent("MANIFEST_PATH_MISMATCH");
    expect(onSaved).not.toHaveBeenCalled();
  });

  it("Reset re-seeds from the snapshot without calling updateAgent", async () => {
    const user = userEvent.setup();
    renderTab();
    await screen.findByTestId("manifest-editor-edit");
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    await user.clear(ta);
    await user.type(ta, "edited: yaml");
    await user.click(screen.getByTestId("manifest-reset-btn"));
    expect(updateAgentMock).not.toHaveBeenCalled();
    // Editor remounts and re-seeds from the server snapshot.
    await screen.findByTestId("manifest-editor-edit");
  });

  it("keeps the active group and sub-tab after a save + snapshot refresh (#2)", async () => {
    const user = userEvent.setup();
    updateAgentMock.mockResolvedValue(sampleDetail);
    const { rerender } = renderTab();
    await screen.findByTestId("manifest-editor-edit");

    // Navigate away from the default group into a non-default sub-tab.
    await user.click(screen.getByTestId("cfg-nav-security"));
    await screen.findByTestId("security-section");
    await user.click(screen.getByRole("tab", { name: "Human approval" }));
    await screen.findByTestId("security-tab-approval");

    await user.click(screen.getByTestId("manifest-save-btn"));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));

    // The parent's quiet refetch lands: a re-dumped (different) server
    // snapshot flows in through props. Before #2 this key-remounted the
    // editor and reset the form position to basic/defenses.
    rerender(
      <MemoryRouter>
        <ManifestTab detail={savedDetail} onSaved={onSaved} />
      </MemoryRouter>,
    );

    expect(screen.getByTestId("security-section")).toBeInTheDocument();
    expect(screen.getByTestId("security-tab-approval")).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "Human approval" }),
    ).toHaveAttribute("aria-selected", "true");
  });

  it("adopts the refreshed server snapshot in place — editor content resyncs (#2)", async () => {
    const user = userEvent.setup();
    const { rerender } = renderTab();
    await screen.findByTestId("manifest-editor-edit");

    rerender(
      <MemoryRouter>
        <ManifestTab detail={savedDetail} onSaved={onSaved} />
      </MemoryRouter>,
    );

    // The YAML view serialises the editor's working manifest — it must
    // carry the refreshed snapshot, not the stale mount-time seed.
    await user.click(screen.getByTestId("cfg-yaml-toggle"));
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    expect(ta.value).toContain("claude-opus-4-5");
  });

  it("restores the active group from the URL after a full remount (#2)", async () => {
    const user = userEvent.setup();
    const { rerender } = renderTab();
    await screen.findByTestId("manifest-editor-edit");
    await user.click(screen.getByTestId("cfg-nav-security"));
    await screen.findByTestId("security-section");

    // Full remount of the tab (e.g. the page-level skeleton path) — the
    // router (and its ?group= query) survives, so the editor reopens on
    // the security group instead of falling back to basic.
    rerender(
      <MemoryRouter>
        <ManifestTab key="remounted" detail={sampleDetail} onSaved={onSaved} />
      </MemoryRouter>,
    );
    await screen.findByTestId("manifest-editor-edit");
    expect(await screen.findByTestId("security-section")).toBeInTheDocument();
  });
});
