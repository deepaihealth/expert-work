/**
 * PassageHistory — B-175 P3-PR3: a passage's revisions (the first, machine
 * one says it came from the file), and restoring an older one against the
 * revision the passage was read at; a conflict reloads the history.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { ApiError } from "../../../../api/client";
import * as sdk from "../../../../api/knowledge_maintenance";
import type { Passage } from "../../../../api/knowledge_maintenance";
import { PassageHistory } from "../PassageHistory";
import { BASE_NAME, fixturePassages, revision } from "./fixtures";

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

function renderHistory(passage: Passage, { readonly = false }: { readonly?: boolean } = {}) {
  const handlers = { onClose: vi.fn(), onChanged: vi.fn(), onGone: vi.fn() };
  render(
    <App>
      <PassageHistory
        baseName={BASE_NAME}
        documentId="d1"
        readScope={TENANT}
        passage={passage}
        readonly={readonly}
        busy={false}
        {...handlers}
      />
    </App>,
  );
  return handlers;
}

/** Click a Popconfirm's OK button (it mounts last, in a portal). */
async function confirmPopover(label: RegExp) {
  const buttons = await screen.findAllByRole("button", { name: label });
  await userEvent.click(buttons[buttons.length - 1]);
}

const HISTORY = [
  revision({ revision: 1, content: "Original.", edited_by: null }),
  revision({ revision: 2, content: "Edited.", edited_by: "user-a" }),
];

describe("PassageHistory", () => {
  it("lists the revisions newest first with the tenant scope; the current one cannot be restored", async () => {
    const [parent] = fixturePassages(); // at revision 2
    const list = vi.spyOn(sdk, "listPassageRevisions").mockResolvedValue(HISTORY);
    renderHistory(parent);

    const first = await screen.findByTestId("kb-passage-revision-1");
    expect(list).toHaveBeenCalledWith(BASE_NAME, "d1", parent.id, TENANT);
    expect(first).toHaveTextContent("入库原文");
    expect(first).toHaveTextContent("Original.");
    const second = screen.getByTestId("kb-passage-revision-2");
    expect(second).toHaveTextContent("user-a 修改");
    expect(second).toHaveTextContent("当前");
    expect(screen.queryByTestId("kb-passage-restore-2")).toBeNull();
    const order = screen.getAllByTestId(/^kb-passage-revision-/).map((el) => el.dataset.testid);
    expect(order).toEqual(["kb-passage-revision-2", "kb-passage-revision-1"]);
  });

  it("restores an older revision against the revision the passage was read at", async () => {
    const [parent] = fixturePassages();
    const restored = { ...parent, content: "Original.", revision: 3, edited: false };
    vi.spyOn(sdk, "listPassageRevisions")
      .mockResolvedValueOnce(HISTORY)
      .mockResolvedValueOnce([...HISTORY, revision({ revision: 3, content: "Original." })]);
    const restore = vi.spyOn(sdk, "restorePassage").mockResolvedValue(restored);
    const { onChanged } = renderHistory(parent);

    await userEvent.click(await screen.findByTestId("kb-passage-restore-1"));
    await confirmPopover(/恢复到这一版|恢 复/);

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(restored));
    expect(restore).toHaveBeenCalledWith(BASE_NAME, "d1", parent.id, 1, 2);
    expect(await screen.findByTestId("kb-passage-revision-3")).toBeInTheDocument();
    expect(within(screen.getByTestId("kb-passage-revision-3")).getByText("当前")).toBeInTheDocument();
  });

  it("a conflict says the passage just changed and reloads its history", async () => {
    const [parent] = fixturePassages();
    const theirs = { ...parent, revision: 3 };
    const list = vi.spyOn(sdk, "listPassageRevisions").mockResolvedValue(HISTORY);
    const restore = vi
      .spyOn(sdk, "restorePassage")
      .mockRejectedValueOnce(
        new ApiError("conflict", "HTTP_409", 409, { error: "revision_conflict", passage: theirs }),
      )
      .mockResolvedValueOnce({ ...parent, revision: 4 });
    const { onChanged } = renderHistory(parent);

    await userEvent.click(await screen.findByTestId("kb-passage-restore-1"));
    await confirmPopover(/恢复到这一版|恢 复/);

    expect(await screen.findByText("这一段刚被改过，已刷新历史，请再选一次。")).toBeInTheDocument();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    // The list shows the passage as it stands now.
    expect(onChanged).toHaveBeenCalledWith(theirs);

    // Picking again names the revision the conflict showed.
    await userEvent.click(screen.getByTestId("kb-passage-restore-1"));
    await confirmPopover(/恢复到这一版|恢 复/);
    await waitFor(() => expect(restore).toHaveBeenCalledTimes(2));
    expect(restore).toHaveBeenLastCalledWith(BASE_NAME, "d1", parent.id, 1, 3);
  });

  it("read-only: restore is disabled behind the read-only hint", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "listPassageRevisions").mockResolvedValue(HISTORY);
    renderHistory(parent, { readonly: true });
    expect(await screen.findByTestId("kb-passage-restore-1")).toBeDisabled();
    expect(screen.getByTestId("readonly-tooltip")).toBeInTheDocument();
  });

  it("a passage never edited has no history yet", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "listPassageRevisions").mockResolvedValue([]);
    renderHistory({ ...parent, revision: 1, edited: false });
    expect(await screen.findByText("还没有改过，没有修订记录。")).toBeInTheDocument();
  });
});
