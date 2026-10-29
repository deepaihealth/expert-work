/**
 * PassageList — B-175 P3-PR3: a document's passages page by page (with the
 * read scope), switched on / off in place, read again when the drawer says
 * something changed, and 「新增整块」 (disabled while read-only).
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import * as sdk from "../../../../api/knowledge_maintenance";
import type { Passage, PassagesPage } from "../../../../api/knowledge_maintenance";
import { PassageList } from "../PassageList";
import { BASE_NAME, fixturePage, fixturePassages } from "./fixtures";

const TENANT = "33333333-3333-3333-3333-333333333333";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

interface Props {
  version?: number;
  readonly?: boolean;
}

function handlerSet() {
  return {
    onEdit: vi.fn<(passage: Passage) => void>(),
    onHistory: vi.fn<(passage: Passage) => void>(),
    onAdd: vi.fn<() => void>(),
    onSwitched: vi.fn<() => void>(),
  };
}

function tree(handlers: ReturnType<typeof handlerSet>, { version = 0, readonly = false }: Props) {
  return (
    <App>
      <PassageList
        baseName={BASE_NAME}
        documentId="d1"
        readScope={TENANT}
        readonly={readonly}
        busy={false}
        version={version}
        onEdit={handlers.onEdit}
        onHistory={handlers.onHistory}
        onAdd={handlers.onAdd}
        onSwitched={handlers.onSwitched}
      />
    </App>
  );
}

function renderList(props: Props = {}) {
  const handlers = handlerSet();
  const view = render(tree(handlers, props));
  return { handlers, rerender: (next: Props) => view.rerender(tree(handlers, next)) };
}

function page(passages: Passage[], total = passages.length, offset = 0): PassagesPage {
  return { passages, figure_ids: [], total, offset, limit: 20 };
}

describe("PassageList", () => {
  it("reads the first page with the read scope and shows each passage", async () => {
    const [parent, inherited] = fixturePassages();
    const list = vi.spyOn(sdk, "listPassages").mockResolvedValue(fixturePage());
    renderList();
    expect(await screen.findByTestId(`kb-passage-card-${parent.id}`)).toBeInTheDocument();
    expect(screen.getByTestId(`kb-passage-card-${inherited.id}`)).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(BASE_NAME, "d1", { offset: 0, limit: 20 }, TENANT);
  });

  it("switches a passage in place and tells the drawer", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "listPassages").mockResolvedValue(page([parent]));
    const update = vi
      .spyOn(sdk, "updatePassage")
      .mockResolvedValue({ ...parent, enabled: false, disabled_reason: "manual" });
    const { handlers } = renderList();

    await userEvent.click(await screen.findByTestId(`kb-passage-toggle-${parent.id}`));

    await waitFor(() => expect(handlers.onSwitched).toHaveBeenCalled());
    expect(update).toHaveBeenCalledWith(BASE_NAME, "d1", parent.id, { enabled: false });
    expect(screen.getByTestId(`kb-passage-toggle-${parent.id}`)).not.toBeChecked();
    expect(await screen.findByText("已停用，检索不再返回这一块。")).toBeInTheDocument();
  });

  it("pages by 20 and reads the page again when the drawer bumps its version", async () => {
    const [parent] = fixturePassages();
    const list = vi.spyOn(sdk, "listPassages").mockResolvedValue(page([parent], 45));
    const { rerender } = renderList();
    await screen.findByTestId(`kb-passage-card-${parent.id}`);

    await userEvent.click(screen.getByTitle("2"));
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(BASE_NAME, "d1", { offset: 20, limit: 20 }, TENANT),
    );

    rerender({ version: 1 });
    await waitFor(() => expect(list).toHaveBeenCalledTimes(3));
    expect(list).toHaveBeenLastCalledWith(BASE_NAME, "d1", { offset: 20, limit: 20 }, TENANT);
  });

  it("新增整块 opens the editor; read-only disables it", async () => {
    vi.spyOn(sdk, "listPassages").mockResolvedValue(page([]));
    const { handlers } = renderList();
    await userEvent.click(await screen.findByTestId("kb-passage-add"));
    expect(handlers.onAdd).toHaveBeenCalled();
    expect(screen.getByText("这份文档还没有整块（可能仍在入库）。")).toBeInTheDocument();
  });

  it("read-only: 新增整块 is disabled behind the read-only hint", async () => {
    vi.spyOn(sdk, "listPassages").mockResolvedValue(page([]));
    renderList({ readonly: true });
    expect(await screen.findByTestId("kb-passage-add")).toBeDisabled();
    expect(screen.getByTestId("readonly-tooltip")).toBeInTheDocument();
  });
});
