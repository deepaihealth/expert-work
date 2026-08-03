/**
 * SkillDetail 页面级 readScope 接线 + readonly 判定 — Cross-tenant W4(D2)。
 *
 * 两块页面级契约(组件级两态断言在 skill_detail/__tests__/ 里):
 *
 *   1. **readScope 接线**:``?tenant_id=`` 深链的权威读口径必须真正下传到
 *      五个子面板读链路(GovernancePanel pending / EvalEvidencePanel /
 *      LineagePanel / FileEditor 文件读 / RenameModal 预读)——把任一处
 *      ``readScope={readScope}`` 换成 ``undefined`` 这里必须红。
 *   2. **readonly 跟随读目标**:"*" 聚合深链(ambient "*" + ``?tenant_id=B``)
 *      不是切入态,但读的是外租户 → 写控件必须置灰;同 ambient 无深链
 *      (读回归属租户)则保持可写,防"一律置灰"的退化实现。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import * as sdk from "../../api/skill-evolution";
import type { SkillApi } from "../../api/skillApi";
import type { SkillRecord, SkillVersion } from "../../api/skills";

const { TENANT_A, TENANT_B } = vi.hoisted(() => ({
  // 归属租户(mock identity 的 homeTenantId)。
  TENANT_A: "11111111-1111-1111-1111-111111111111",
  // 外租户(聚合行跳转的深链目标)。
  TENANT_B: "22222222-2222-2222-2222-222222222222",
}));

// Ambient scope mock — 共享工厂;ref undefined = home,"*" = 聚合。
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

// Identity mock — homeTenantId=TENANT_A 的 system_admin(readonly 判定要比对
// homeTenantId;真 AuthProvider 要拉 /v1/me,直接 mock useAuth 更确定)。
vi.mock("../../auth/AuthContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../auth/AuthContext")>();
  return {
    ...original,
    useAuth: () => ({
      status: "authenticated" as const,
      identity: {
        kind: "jwt" as const,
        subject: "u1",
        subjectType: "user" as const,
        homeTenantId: TENANT_A,
        roles: ["system_admin"],
        isSystemAdmin: true,
        homeIsPlatform: false,
        displayName: "u1",
        serverResolved: true,
      },
      token: "test-token",
      login: () => {},
      logout: () => {},
      refreshIdentity: async () => {},
    }),
  };
});

// FileEditor 的 monaco → textarea 桩(照 SkillsList.test.tsx)。
vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
  }: {
    value: string;
    onChange?: (v: string | undefined) => void;
  }) => (
    <textarea
      data-testid="monaco-stub"
      value={value}
      onChange={(e) => onChange?.(e.target.value)}
    />
  );
  const DiffEditor = () => <div data-testid="monaco-diff-stub" />;
  return { default: Editor, DiffEditor };
});

import { SkillDetail } from "../SkillDetail";

const listPromoteRequestsMock = vi.spyOn(sdk, "listPromoteRequests");
const listEvalResultsMock = vi.spyOn(sdk, "listEvalResults");
const getLineageMock = vi.spyOn(sdk, "getLineage");

const SKILL: SkillRecord = {
  id: "sk-1",
  tenant_id: TENANT_B,
  name: "researcher",
  status: "draft",
  latest_version: 1,
  description: "",
  category: "research",
  pinned: false,
  last_used_at: null,
  state_changed_at: null,
  created_at: "2026-06-08T00:00:00Z",
  updated_at: "2026-06-08T00:00:00Z",
  // agent_private + 无 pending → GovernancePanel 渲染提议按钮(写控件)。
  visibility: "agent_private",
  created_by_agent_name: "assistant",
};

const VERSION: SkillVersion = {
  id: "v1",
  skill_id: "sk-1",
  version: 1,
  prompt_fragment: "Always cite sources.",
  tool_names: ["web_search"],
  description: "First cut.",
  category: "research",
  required_models: [],
  authored_by: "human",
  supporting_files: { "notes.md": { size: 5, mime: "text/markdown" } },
  lazy_load: false,
  high_risk: false,
  created_at: "2026-06-08T00:00:00Z",
};

function makeApi(): SkillApi {
  return {
    getSkill: vi.fn().mockResolvedValue(SKILL),
    listVersions: vi.fn().mockResolvedValue({ items: [VERSION] }),
    patchStatus: vi.fn(),
    exportVersion: vi.fn(),
    // "hello" 的 base64。
    getSupportingFile: vi
      .fn()
      .mockResolvedValue({ content: "aGVsbG8=", size: 5, mime: "text/markdown" }),
    putSupportingFile: vi.fn(),
    deleteSupportingFile: vi.fn(),
    renameSupportingFile: vi.fn().mockResolvedValue({ ...VERSION, id: "v2", version: 2 }),
    putPrompt: vi.fn(),
  };
}

function renderDetail(url: string, api: SkillApi) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <App>
        <Routes>
          <Route path="/skills/:skillId" element={<SkillDetail api={api} />} />
        </Routes>
      </App>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  scopeRef.current = undefined;
  listPromoteRequestsMock
    .mockReset()
    .mockResolvedValue({ items: [], next_cursor: null, cross_tenant: false });
  listEvalResultsMock.mockReset().mockResolvedValue([]);
  getLineageMock
    .mockReset()
    .mockResolvedValue({ skill: SKILL, forked_from_source: null, versions: [VERSION] });
});

describe("SkillDetail readScope 接线(W4 D2)", () => {
  it("深链 ?tenant_id= 透传页面读 + 三面板 + FileEditor 文件读", async () => {
    const api = makeApi();
    renderDetail(`/skills/sk-1?tenant_id=${TENANT_B}`, api);
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument(),
    );

    // 页面级读。
    expect(api.getSkill).toHaveBeenCalledWith("sk-1", TENANT_B);
    expect(api.listVersions).toHaveBeenCalledWith("sk-1", TENANT_B);

    // 三个 tenant-flywheel 面板的读链路。
    await waitFor(() =>
      expect(listEvalResultsMock).toHaveBeenCalledWith("sk-1", TENANT_B),
    );
    expect(getLineageMock).toHaveBeenCalledWith("sk-1", TENANT_B);
    expect(listPromoteRequestsMock).toHaveBeenCalledWith({
      status: "pending",
      tenantScope: TENANT_B,
    });

    // FileEditor:点树里的 supporting file → 懒读带 readScope。
    const user = userEvent.setup();
    await user.click(screen.getByText("notes.md"));
    await waitFor(() =>
      expect(api.getSupportingFile).toHaveBeenCalledWith("sk-1", 1, "notes.md", TENANT_B),
    );
  });

  it("RenameModal 预读透传 readScope(本租户深链,可写态)", async () => {
    const api = makeApi();
    // 深链到归属租户 → readonly=false,重命名入口可用(外租户深链下写
    // 控件置灰,预读只在这条本租户路径上可达)。
    renderDetail(`/skills/sk-1?tenant_id=${TENANT_A}`, api);
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument(),
    );

    const user = userEvent.setup();
    await user.click(screen.getByText("notes.md"));
    await waitFor(() =>
      expect(screen.getByTestId("skill-editor-rename-btn")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("skill-editor-rename-btn"));

    const input = await screen.findByTestId("skill-rename-new-path");
    await user.clear(input);
    await user.type(input, "notes2.md");
    // 隔离断言:FileEditor 选中文件时已经用同参数读过一次,先清掉。
    vi.mocked(api.getSupportingFile).mockClear();
    await user.click(screen.getByTestId("skill-rename-submit"));

    await waitFor(() =>
      expect(api.getSupportingFile).toHaveBeenCalledWith("sk-1", 1, "notes.md", TENANT_A),
    );
    expect(api.renameSupportingFile).toHaveBeenCalled();
  });
});

describe("SkillDetail readonly 跟随读目标(W4 D2 fix)", () => {
  it('"*" 聚合 + 外租户深链 → 写控件置灰', async () => {
    scopeRef.current = "*";
    const api = makeApi();
    renderDetail(`/skills/sk-1?tenant_id=${TENANT_B}`, api);
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument(),
    );

    // 页面级(置顶)+ 子组件级(GovernancePanel 提议)各断一处。
    expect(screen.getByTestId("skill-pin-button")).toBeDisabled();
    await waitFor(() =>
      expect(screen.getByTestId("skill-propose-button")).toBeDisabled(),
    );
  });

  it('"*" 聚合无深链(读归属租户)→ 保持可写', async () => {
    scopeRef.current = "*";
    const api = makeApi();
    renderDetail("/skills/sk-1", api);
    await waitFor(() =>
      expect(screen.getByTestId("skill-detail-root")).toBeInTheDocument(),
    );

    expect(screen.getByTestId("skill-pin-button")).toBeEnabled();
    await waitFor(() =>
      expect(screen.getByTestId("skill-propose-button")).toBeEnabled(),
    );
  });
});
