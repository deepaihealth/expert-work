/**
 * 切入态置灰两态断言 — Cross-tenant W3(review M-2)。
 *
 * F1 清单里的独立条目组件(MetadataPanel 改分类 / AddFileModal 新增 /
 * RenameDeleteModals 重命名+删除 / FileEditor 保存)在此做组件级两态断言;
 * 页面级入口(SkillDetail 的 pin/编辑钮)由 SkillsList.test.tsx 覆盖。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import "../../../i18n";

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

// Cross-tenant W4(D2)— RenameModal/FileEditor 不再自读 ambient scope,
// 改收 SkillDetail 下传的 readScope prop(这里传 undefined = home 态)。

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

import type { SkillApi } from "../../../api/skillApi";
import type { SkillRecord, SkillVersion } from "../../../api/skills";
import { MetadataPanel } from "../MetadataPanel";
import { AddFileModal } from "../AddFileModal";
import { DeleteConfirmModal, RenameModal } from "../RenameDeleteModals";
import { FileEditor } from "../FileEditor";
import { SKILL_MD_PATH } from "../FileTree";

const skill: SkillRecord = {
  id: "sk1",
  name: "web_search",
  status: "active",
  latest_version: 1,
  description: "Search the web.",
  category: "web",
  pinned: false,
  last_used_at: "2026-05-25T10:00:00Z",
  state_changed_at: "2026-05-20T10:00:00Z",
  created_at: "2026-05-20T10:00:00Z",
  updated_at: "2026-05-26T10:00:00Z",
} as SkillRecord;

const version: SkillVersion = {
  id: "v1",
  skill_id: "sk1",
  version: 1,
  prompt_fragment: "Always cite sources.",
  tool_names: ["web_search"],
  description: "First cut.",
  category: "web",
  required_models: [],
  authored_by: "human",
  supporting_files: {},
  lazy_load: false,
  high_risk: false,
  created_at: "2026-05-20T10:00:00Z",
} as SkillVersion;

const api: SkillApi = {
  getSkill: vi.fn(),
  listVersions: vi.fn(),
  patchStatus: vi.fn(),
  exportVersion: vi.fn(),
  getSupportingFile: vi.fn(),
  putSupportingFile: vi.fn(),
  deleteSupportingFile: vi.fn(),
  renameSupportingFile: vi.fn(),
  putPrompt: vi.fn(),
};

beforeEach(() => {
  // vitest 4 的 restore 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("MetadataPanel 改分类(两态)", () => {
  function renderPanel() {
    return render(
      <MetadataPanel
        skill={skill}
        version={version}
        categoryOptions={["web", "data"]}
        onSaveCategory={vi.fn()}
      />,
    );
  }

  it("home 态改动后保存可用;切入态置灰", async () => {
    const user = userEvent.setup();
    const first = renderPanel();
    // testid 落在 AutoComplete 根上,真输入框是里面的 combobox。
    await user.type(screen.getByRole("combobox"), "x");
    expect(screen.getByTestId("skill-category-save")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderPanel();
    await user.type(screen.getByRole("combobox"), "x");
    expect(screen.getByTestId("skill-category-save")).toBeDisabled();
  });
});

describe("AddFileModal 新增(两态)", () => {
  function renderModal() {
    return render(
      <App>
        <AddFileModal
          api={api}
          open
          skillId="sk1"
          versionNumber={1}
          onClose={vi.fn()}
          onAdded={vi.fn()}
        />
      </App>,
    );
  }

  it("home 态提交可用;切入态置灰", () => {
    const first = renderModal();
    expect(screen.getByTestId("skill-add-file-submit")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    renderModal();
    expect(screen.getByTestId("skill-add-file-submit")).toBeDisabled();
  });
});

describe("RenameDeleteModals 重命名/删除(两态)", () => {
  it("home 态重命名提交可用;切入态置灰", () => {
    const props = {
      api,
      open: true,
      skillId: "sk1",
      versionNumber: 1,
      oldPath: "notes.md",
      readScope: undefined,
      onClose: vi.fn(),
      onRenamed: vi.fn(),
    };
    const first = render(
      <App>
        <RenameModal {...props} />
      </App>,
    );
    expect(screen.getByTestId("skill-rename-submit")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    render(
      <App>
        <RenameModal {...props} />
      </App>,
    );
    expect(screen.getByTestId("skill-rename-submit")).toBeDisabled();
  });

  it("home 态回打路径后删除提交可用;切入态即使回打也置灰", async () => {
    const user = userEvent.setup();
    const props = {
      api,
      open: true,
      skillId: "sk1",
      versionNumber: 1,
      path: "notes.md",
      onClose: vi.fn(),
      onDeleted: vi.fn(),
    };
    const first = render(
      <App>
        <DeleteConfirmModal {...props} />
      </App>,
    );
    await user.type(screen.getByTestId("skill-delete-confirm-input"), "notes.md");
    expect(screen.getByTestId("skill-delete-submit")).toBeEnabled();
    first.unmount();

    isTenantSwitchedMock.mockReturnValue(true);
    render(
      <App>
        <DeleteConfirmModal {...props} />
      </App>,
    );
    await user.type(screen.getByTestId("skill-delete-confirm-input"), "notes.md");
    expect(screen.getByTestId("skill-delete-submit")).toBeDisabled();
  });
});

describe("FileEditor 保存(两态)", () => {
  it("home 态改脏后保存可用;编辑中切入(rerender)保存置灰", async () => {
    const user = userEvent.setup();
    // 每次 rerender 造新 element——同引用 element 会被 React bailout,
    // 不重跑 FileEditor 的 hooks,翻 mock 就不生效。callback 保持同引用
    // (reset effect 挂在 onDirtyChange dep 上,新引用会把 mode 打回 view)。
    const onDirtyChange = vi.fn();
    const onSaved = vi.fn();
    const onRequestDelete = vi.fn();
    const onRequestRename = vi.fn();
    const tree = () => (
      <App>
        <FileEditor
          api={api}
          skillId="sk1"
          version={version}
          readScope={undefined}
          selectedPath={SKILL_MD_PATH}
          onDirtyChange={onDirtyChange}
          onSaved={onSaved}
          onRequestDelete={onRequestDelete}
          onRequestRename={onRequestRename}
        />
      </App>
    );
    const view = render(tree());

    await user.click(screen.getByTestId("skill-editor-edit-btn"));
    fireEvent.change(screen.getByTestId("monaco-stub"), {
      target: { value: "Always cite sources. And dates." },
    });
    expect(screen.getByTestId("skill-editor-save-btn")).toBeEnabled();

    // 编辑中途从顶栏切入他租户(实时可达)——保存必须当场置灰。
    isTenantSwitchedMock.mockReturnValue(true);
    view.rerender(tree());
    expect(screen.getByTestId("skill-editor-save-btn")).toBeDisabled();
  });
});
