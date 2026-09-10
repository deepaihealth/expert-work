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
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
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
  feedback_rating: "down",
  status: "pending",
  eval_dataset_id: null,
  detected_at: "2026-05-26T10:00:00Z",
  reviewed_at: null,
  feedback_run_id: "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  feedback_comment: "太慢",
  feedback_changed_at: null,
  feedback_source: "console",
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
  scopeRef.current = undefined;
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
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

  it("切入态置灰驳回/提升(两态:home 态由上方用例覆盖)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
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
    await waitFor(() =>
      expect(screen.getByTestId("curation-dismiss-btn")).toBeDisabled(),
    );
    expect(screen.getByTestId("curation-promote-btn")).toBeDisabled();
  });

  it("P-2 — 列表与详情摆出被踩的轮、用户原话与改票标记", async () => {
    const changed = { ...candidateRow, feedback_changed_at: "2026-09-09T12:00:00Z" };
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [changed], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({ ...changed, trajectory: null }),
      },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("太慢")).toBeInTheDocument());
    expect(screen.getByTestId("curation-feedback-changed-tag")).toBeInTheDocument();
    await user.click(screen.getByText("research"));
    await waitFor(() =>
      expect(screen.getByTestId("curation-detail-feedback-run")).toHaveTextContent("7c9e6679"),
    );
    expect(screen.getByTestId("curation-detail-feedback-comment")).toHaveTextContent("太慢");
  });

  it("PR4 — 候选行标出这一踩是谁打的(员工 / 终端用户),worker 兜底行不标", async () => {
    const external = { ...candidateRow, feedback_source: "external" };
    const workerBuilt = {
      ...candidateRow,
      id: "c2",
      signal: "implicit_success",
      feedback_rating: null,
      feedback_run_id: null,
      feedback_comment: null,
      feedback_source: null,
    };
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [external, workerBuilt], total: 2, cross_tenant: false }),
      },
    ]);
    renderCuration();
    // 本文件不钉 locale,两种语言都收。
    await waitFor(() =>
      expect(screen.getAllByTestId("curation-feedback-source-tag")).toHaveLength(1),
    );
    expect(screen.getByTestId("curation-feedback-source-tag").textContent).toMatch(
      /终端用户|end user/,
    );
  });

  it("PR4 — 员工打的踩标「员工」(与终端用户区分开)", async () => {
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [candidateRow], total: 1, cross_tenant: false }),
      },
    ]);
    renderCuration();
    await waitFor(() =>
      expect(screen.getByTestId("curation-feedback-source-tag").textContent).toMatch(
        /员工|employee/,
      ),
    );
  });

  it("P-2 — 没改过票就不挂「后改为 👍」标记(阴性对照)", async () => {
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
    renderCuration();
    await waitFor(() => expect(screen.getByText("太慢")).toBeInTheDocument());
    expect(screen.queryByTestId("curation-feedback-changed-tag")).not.toBeInTheDocument();
  });

  it("P-2 — worker 兜底建的候选(没有反馈快照)不渲染空的原话 / 被踩的轮", async () => {
    const noFeedback = {
      ...candidateRow,
      signal: "implicit_success",
      feedback_rating: null,
      feedback_run_id: null,
      feedback_comment: null,
      feedback_changed_at: null,
    };
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [noFeedback], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({ ...noFeedback, trajectory: null }),
      },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    expect(screen.queryByTestId("curation-feedback-changed-tag")).not.toBeInTheDocument();
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-promote-btn")).toBeInTheDocument());
    expect(screen.queryByTestId("curation-detail-feedback-run")).not.toBeInTheDocument();
    expect(screen.queryByTestId("curation-detail-feedback-comment")).not.toBeInTheDocument();
  });

  it("threads the switched tenant scope into the getCandidate detail read (W3)", async () => {
    scopeRef.current = "22222222-2222-2222-2222-222222222222";
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
    await waitFor(() => expect(detailParams?.tenant_id).toBe(scopeRef.current));
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

  it("promote posts a backend-valid source and the reviewer's expected for a negative candidate", async () => {
    const posted: unknown[] = [];
    installAdapter([
      {
        match: (u, m) => u.startsWith("/v1/curation/candidates") && m === "get" && !u.includes("/c1"),
        respond: () => ({ items: [candidateRow], total: 1, cross_tenant: false }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1" && m === "get",
        respond: () => ({ ...candidateRow, trajectory: null }),
      },
      {
        match: (u, m) => u === "/v1/curation/candidates/c1/promote" && m === "post",
        respond: () => datasetRow,
      },
    ]);
    const realAdapter = apiClient.defaults.adapter;
    apiClient.defaults.adapter = (config) => {
      if (config.method === "post") posted.push(JSON.parse(String(config.data)));
      return (realAdapter as (c: typeof config) => Promise<unknown>)(config) as never;
    };
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByText("research")).toBeInTheDocument());
    await user.click(screen.getByText("research"));
    await waitFor(() => expect(screen.getByTestId("curation-promote-btn")).toBeInTheDocument());
    await user.click(screen.getByTestId("curation-promote-btn"));
    await user.type(screen.getByTestId("curation-promote-name-input"), "neg-set");
    await user.type(screen.getByTestId("curation-promote-expected-input"), '{{"answer": "corrected"}');
    // 抽屉里的「Promote」按钮和弹窗 OK 按钮同文案,只点弹窗那颗。
    // 抽屉里的「Promote」按钮和弹窗 OK 按钮同文案,只点弹窗那颗。
    await user.click(
      document.querySelector(".ant-modal-footer .ant-btn-primary") as HTMLElement,
    );
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({ name: "neg-set", source: "regression", expected: { answer: "corrected" } });
    expect(["golden", "trajectory", "regression"]).toContain((posted[0] as { source: string }).source);
  });

  it("signal filter only offers backend-known signals", async () => {
    installAdapter([
      { match: (u) => u.startsWith("/v1/curation/candidates"), respond: () => ({ items: [], total: 0, cross_tenant: false }) },
    ]);
    const user = userEvent.setup();
    renderCuration();
    await waitFor(() => expect(screen.getByTestId("curation-signal-filter")).toBeInTheDocument());
    await user.click(screen.getByTestId("curation-signal-filter").querySelector(".ant-select-selector") as HTMLElement);
    // rc-select 的 ``role="option"`` 是给读屏用的 a11y 影子列表(只渲染 activeIndex
    // 附近两条、文本是 value 不是 label),真正的选项节点是 ``.ant-select-item-option``。
    await waitFor(() =>
      expect(document.querySelectorAll(".ant-select-item-option").length).toBeGreaterThan(0),
    );
    const labels = Array.from(document.querySelectorAll(".ant-select-item-option")).map(
      (o) => o.textContent,
    );
    expect(labels).toEqual(["All signals", "negative_feedback", "failed_outcome", "positive_feedback", "implicit_success"]);
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

  it("切入态置灰创建/编辑/删除(两态:home 态由上方用例覆盖)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
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
    expect(screen.getByTestId("evald-create-btn")).toBeDisabled();
    expect(screen.getByTestId(`eval-edit-${datasetRow.id}`)).toBeDisabled();
    expect(screen.getByTestId(`eval-delete-${datasetRow.id}`)).toBeDisabled();
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
