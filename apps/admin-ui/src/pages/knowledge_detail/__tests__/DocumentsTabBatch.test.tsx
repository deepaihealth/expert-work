/**
 * DocumentsTab — batch actions and row flags (B-175 P3-PR3).
 *
 * Rows of the current page can be selected (labelled checkboxes); the batch
 * bar switches them on / off, re-ingests them (after a confirm) or deletes
 * them (after a confirm, tenant admins only), says what was done and what was
 * skipped and why, refreshes the table and clears the selection. A switched
 * off document is tagged; one with something to review carries 「待复核 N」,
 * which opens the drawer on 待复核. Read-only: no selection, every action
 * disabled — the review tag still opens the (read-only) review.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
import * as maintenanceSdk from "../../../api/knowledge_maintenance";
import { DocumentsTab } from "../DocumentsTab";

const BASE_NAME = "support-docs";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

function doc(
  id: string,
  filename: string,
  extra: Partial<knowledgeSdk.KnowledgeDocument> = {},
): knowledgeSdk.KnowledgeDocument {
  return {
    id,
    filename,
    status: "ready",
    error: null,
    chunk_count: 3,
    attempts: 1,
    enabled: true,
    review: { inherited: 0, unapplied: 0 },
    created_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:05:00Z",
    ...extra,
  };
}

const A = doc("11111111-0000-0000-0000-00000000000a", "a.pdf");
const B = doc("11111111-0000-0000-0000-00000000000b", "b.pdf");

function docPage(
  documents: knowledgeSdk.KnowledgeDocument[],
  total = documents.length,
): knowledgeSdk.DocumentsPage {
  return { documents, total, offset: 0, limit: 50 };
}

function renderTab({ readonly = false, canDelete = true } = {}) {
  return render(
    <App>
      <DocumentsTab
        baseName={BASE_NAME}
        readScope={undefined}
        readonly={readonly}
        canDelete={canDelete}
      />
    </App>,
  );
}

async function selectRows(...names: string[]) {
  for (const name of names) {
    await userEvent.click(await screen.findByRole("checkbox", { name: `选择 ${name}` }));
  }
}

/** Click a Popconfirm's OK button (it mounts last, in a portal). */
async function confirmPopover(label: RegExp) {
  const buttons = await screen.findAllByRole("button", { name: label });
  await userEvent.click(buttons[buttons.length - 1]);
}

describe("DocumentsTab — batch actions (B-175 P3-PR3)", () => {
  it("switches the selected documents off, says so, refreshes and clears the selection", async () => {
    const list = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A, B]));
    const batch = vi
      .spyOn(maintenanceSdk, "batchDocuments")
      .mockResolvedValue({ action: "disable", done: [A.id, B.id], skipped: [] });
    renderTab();

    await selectRows("a.pdf", "b.pdf");
    expect(screen.getByTestId("doc-batch-count")).toHaveTextContent("已选 2 份");
    await userEvent.click(screen.getByTestId("doc-batch-disable"));

    await waitFor(() =>
      expect(batch).toHaveBeenCalledWith(BASE_NAME, "disable", [A.id, B.id]),
    );
    expect(await screen.findByText("已停用 2 份。")).toBeInTheDocument();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    // The refresh after a batch reads each row's review again.
    expect(list).toHaveBeenLastCalledWith(BASE_NAME, { limit: 50, offset: 0 }, undefined, {
      withReview: true,
    });
    expect(screen.queryByTestId("doc-batch-count")).toBeNull();
    expect(screen.getByRole("checkbox", { name: "选择 a.pdf" })).not.toBeChecked();
  });

  it("re-ingest asks first and names what was skipped and why", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A, B]));
    const batch = vi.spyOn(maintenanceSdk, "batchDocuments").mockResolvedValue({
      action: "reingest",
      done: [A.id],
      skipped: [{ id: B.id, reason: "content_missing" }],
    });
    renderTab();

    await selectRows("a.pdf", "b.pdf");
    await userEvent.click(screen.getByTestId("doc-batch-reingest"));
    expect(batch).not.toHaveBeenCalled();
    await confirmPopover(/^重新摄取$|^重新摄 取$/);

    await waitFor(() =>
      expect(batch).toHaveBeenCalledWith(BASE_NAME, "reingest", [A.id, B.id]),
    );
    expect(await screen.findByText("已提交 1 份重新摄取，正在估算费用；估算完成后在「文档」页确认才会开始。")).toBeInTheDocument();
    expect(
      await screen.findByText("跳过 1 份：1 份没保留原文件，需要重新上传"),
    ).toBeInTheDocument();
  });

  it("batch delete needs a tenant admin; with it, deletes after a confirm", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A, B]));
    const remove = vi
      .spyOn(maintenanceSdk, "batchDeleteDocuments")
      .mockResolvedValue({ action: "delete", done: [A.id], skipped: [] });
    const { unmount } = renderTab({ canDelete: false });
    await selectRows("a.pdf");
    expect(screen.getByTestId("doc-batch-delete")).toBeDisabled();
    expect(screen.getByTestId("doc-batch-delete-needs-admin")).toBeInTheDocument();
    unmount();

    renderTab({ canDelete: true });
    await selectRows("a.pdf");
    await userEvent.click(screen.getByTestId("doc-batch-delete"));
    await confirmPopover(/^删除$|^删 除$/);
    await waitFor(() => expect(remove).toHaveBeenCalledWith(BASE_NAME, [A.id]));
    expect(await screen.findByText("已删除 1 份。")).toBeInTheDocument();
  });

  it("read-only: rows cannot be selected and every batch action is disabled", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A]));
    renderTab({ readonly: true });
    expect(await screen.findByRole("checkbox", { name: "选择 a.pdf" })).toBeDisabled();
    const bar = screen.getByTestId("doc-batch-bar");
    for (const id of ["enable", "disable", "reingest", "delete"]) {
      expect(within(bar).getByTestId(`doc-batch-${id}`)).toBeDisabled();
    }
    expect(within(bar).getAllByTestId("readonly-tooltip").length).toBeGreaterThan(0);
  });

  it("turning the page clears the selection", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A, B], 60));
    renderTab();
    await selectRows("a.pdf");
    expect(screen.getByTestId("doc-batch-count")).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("2"));
    await waitFor(() => expect(screen.queryByTestId("doc-batch-count")).toBeNull());
  });
});

describe("DocumentsTab — review counts and the poll (B-175 P3-PR3 review)", () => {
  const P = doc("11111111-0000-0000-0000-00000000000c", "p.pdf", {
    status: "processing",
    review: { inherited: 1, unapplied: 2 },
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** The 5-second poll runs while an ingest is in progress. */
  async function tick() {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
  }

  it("explicit loads ask for review; the poll does not and keeps the row's tag", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bare = (d: knowledgeSdk.KnowledgeDocument) => ({ ...d, review: undefined });
    const list = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([P, B]))
      .mockResolvedValue(docPage([bare(P), bare(B)]));
    renderTab();

    expect(await screen.findByTestId(`doc-review-${P.id}`)).toHaveTextContent("待复核 3");
    expect(list).toHaveBeenLastCalledWith(BASE_NAME, { limit: 50, offset: 0 }, undefined, {
      withReview: true,
    });

    await tick();
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(list.mock.calls[1][3]).toEqual({ withReview: false });
    expect(screen.getByTestId(`doc-review-${P.id}`)).toHaveTextContent("待复核 3");
  });

  it("a refresh keeps only the selected rows still shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([P, B]))
      .mockResolvedValue(docPage([P]));
    renderTab();

    await selectRows("b.pdf");
    expect(screen.getByTestId("doc-batch-count")).toHaveTextContent("已选 1 份");
    await tick();
    await waitFor(() => expect(screen.queryByTestId("doc-batch-count")).toBeNull());
  });

  it("closing the drawer reads the rows again with their review", async () => {
    const list = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([A]));
    vi.spyOn(maintenanceSdk, "listPassages").mockResolvedValue({
      passages: [],
      figure_ids: [],
      total: 0,
      offset: 0,
      limit: 20,
    });
    vi.spyOn(maintenanceSdk, "getUnappliedEdits").mockResolvedValue({
      inherited: [],
      unapplied: [],
    });
    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-chunks-${A.id}`));
    await screen.findByTestId("kb-passage-list");
    await userEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(list).toHaveBeenLastCalledWith(BASE_NAME, { limit: 50, offset: 0 }, undefined, {
      withReview: true,
    });
  });
});

describe("DocumentsTab — row flags (B-175 P3-PR3)", () => {
  it("tags a switched-off document", async () => {
    const off = { ...A, enabled: false };
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([off, B]));
    renderTab();
    expect(await screen.findByTestId(`doc-disabled-${A.id}`)).toHaveTextContent("已停用");
    expect(screen.queryByTestId(`doc-disabled-${B.id}`)).toBeNull();
  });

  it("「待复核 N」 opens the drawer on 待复核 — read-only too", async () => {
    const waiting = { ...A, review: { inherited: 1, unapplied: 2 } };
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([waiting, B]));
    const unapplied = vi
      .spyOn(maintenanceSdk, "getUnappliedEdits")
      .mockResolvedValue({ inherited: [], unapplied: [] });
    renderTab({ readonly: true });

    const tag = await screen.findByTestId(`doc-review-${A.id}`);
    expect(tag).toHaveTextContent("待复核 3");
    expect(screen.queryByTestId(`doc-review-${B.id}`)).toBeNull();
    await userEvent.click(tag);

    expect(await screen.findByTestId("kb-review-panel")).toBeInTheDocument();
    expect(unapplied).toHaveBeenCalledWith(BASE_NAME, A.id, undefined);
  });
});
