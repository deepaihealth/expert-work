/**
 * Curation outer page + CandidatesPanel + EvalDatasetsPanel tests — H.4 PR 1.
 *
 * Each panel is exercised independently through ``apiClient`` adapter
 * mocking. Monaco is replaced by a textarea stub so JSON edits flow
 * through ``onChange`` deterministically (same approach as H.3
 * ApprovalCard tests).
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { Curation } from "../Curation";
import { TenantScopeProvider } from "../../tenant/TenantScopeContext";
import { AuthProvider } from "../../auth/AuthContext";
import { apiClient, setStoredToken } from "../../api/client";

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
    ["data-testid"]: testId,
  }: {
    value: string;
    onChange?: (v: string | undefined) => void;
    "data-testid"?: string;
  }) => (
    <textarea
      data-testid={testId ?? "monaco-stub"}
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  return { default: Editor };
});

// Cross-tenant W3 — override the hook only (the real TenantScopeProvider in
// renderCuration keeps working via the importOriginal spread); switchable per
// test, undefined = home state.
let mockScope: string | undefined;
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../tenant/TenantScopeContext")>()),
  useTenantScope: () => ({
    scope: mockScope ?? "home",
    setScope: () => {},
    apiTenantScope: mockScope,
  }),
}));

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

interface RouteHandler {
  match: (url: string, method: string) => boolean;
  respond: () => unknown;
}

function installAdapter(handlers: RouteHandler[]) {
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    const handler = handlers.find((h) => h.match(url, method));
    return Promise.resolve({
      data: handler?.respond() ?? {},
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderCuration() {
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  return render(
    <MemoryRouter>
      <AuthProvider>
        <TenantScopeProvider>
          <App>
            <Curation />
          </App>
        </TenantScopeProvider>
      </AuthProvider>
    </MemoryRouter>,
  );
}

const candidateRow = {
  id: "c1",
  tenant_id: "t1",
  agent_name: "research",
  agent_version: "1.0",
  thread_id: "th1",
  user_id: null,
  trajectory_key: "obj/c1.json",
  outcome: "negative",
  signal: "negative_feedback",
  feedback_rating: 2,
  status: "pending",
  eval_dataset_id: null,
  detected_at: "2026-05-26T10:00:00Z",
  reviewed_at: null,
};

const datasetRow = {
  id: "d1",
  tenant_id: "t1",
  agent_name: "research",
  name: "golden_v1",
  input: { q: "hello" },
  expected: { ans: "world" },
  source: "golden",
  source_trajectory_key: null,
  source_user_id: null,
  created_at: "2026-05-26T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
};

beforeEach(() => {
  vi.restoreAllMocks();
  mockScope = undefined;
});

describe("Curation outer page", () => {
  it("renders both tab labels and defaults to candidates", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/curation/candidates"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u) => u.startsWith("/v1/eval-datasets"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
    ]);
    renderCuration();
    expect(screen.getByText(/^Candidates$/)).toBeInTheDocument();
    expect(screen.getByText(/Eval Datasets/)).toBeInTheDocument();
    // Candidates panel is visible (its filter dropdown)
    await waitFor(() => {
      expect(screen.getByTestId("curation-status-filter")).toBeInTheDocument();
    });
  });

  it("switches to Eval Datasets tab when clicked", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/curation/candidates"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u) => u.startsWith("/v1/eval-datasets"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await user.click(screen.getByText(/Eval Datasets/));
    await waitFor(() => {
      expect(screen.getByTestId("evald-create-btn")).toBeInTheDocument();
    });
  });
});

describe("CandidatesPanel", () => {
  it("lists pending candidates and opens detail drawer on row click", async () => {
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [candidateRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({
          ...candidateRow,
          trajectory: { messages: [{ role: "user", content: "hi" }], step_count: 1 },
        }),
      },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-trajectory-body")).toBeInTheDocument());
    expect(screen.getByTestId("curation-promote-btn")).toBeInTheDocument();
    expect(screen.getByTestId("curation-dismiss-btn")).toBeInTheDocument();
  });

  it("threads the switched tenant scope into the getCandidate detail read (W3)", async () => {
    mockScope = "22222222-2222-2222-2222-222222222222";
    let detailParams: Record<string, unknown> | undefined;
    apiClient.defaults.adapter = (config) => {
      const url = config.url ?? "";
      let data: unknown = {};
      if (url === "/v1/curation/candidates/c1") {
        detailParams = config.params as Record<string, unknown>;
        data = { ...candidateRow, trajectory: null };
      } else if (url.startsWith("/v1/curation/candidates")) {
        data = { items: [candidateRow], total: 1, cross_tenant: false };
      }
      return Promise.resolve({
        data,
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    };
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(detailParams?.tenant_id).toBe(mockScope));
  });

  it("opens promote modal with required name input", async () => {
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [candidateRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({ ...candidateRow, trajectory: null }),
      },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-promote-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("curation-promote-btn"));
    await waitFor(() => expect(screen.getByTestId("curation-promote-name-input")).toBeInTheDocument());
  });
});

describe("EvalDatasetsPanel", () => {
  async function openDatasetsTab() {
    const user = userEvent.setup();
    renderCuration();
    await user.click(screen.getByText(/Eval Datasets/));
    return user;
  }

  it("renders the table with the row", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/curation/candidates"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u) => u.startsWith("/v1/eval-datasets"),
        respond: () => ({ items: [datasetRow], total: 1, cross_tenant: false }),
      },
    ]);
    await openDatasetsTab();
    await waitFor(() => expect(screen.getByText("golden_v1")).toBeInTheDocument());
  });

  it("disables Save when input JSON is invalid", async () => {
    installAdapter([
      {
        match: (u) => u.startsWith("/v1/curation/candidates"),
        respond: () => ({ items: [], total: 0, cross_tenant: false }),
      },
      {
        match: (u) => u.startsWith("/v1/eval-datasets"),
        respond: () => ({ items: [datasetRow], total: 1, cross_tenant: false }),
      },
    ]);
    const user = await openDatasetsTab();
    await waitFor(() => expect(screen.getByText("golden_v1")).toBeInTheDocument());
    await user.click(screen.getByTestId(`eval-edit-${datasetRow.id}`));
    const editor = await screen.findByTestId("evald-input-editor");
    fireEvent.change(editor, { target: { value: "{not valid" } });
    await waitFor(() => expect(screen.getByTestId("evald-input-error")).toBeInTheDocument());
    expect(screen.getByTestId("evald-save-btn")).toBeDisabled();
  });
});
