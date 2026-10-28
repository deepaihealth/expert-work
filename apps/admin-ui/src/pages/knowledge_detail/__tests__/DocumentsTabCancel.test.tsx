/**
 * DocumentsTab — cancel an ingest in progress (B-222 PR-1).
 *
 * A pending / processing row carries a "cancel ingest" action behind a
 * confirm; it posts ``cancelDocuments`` and refreshes the table. Read-only
 * (cross-tenant) disables it. A cancelled document shows its own status tag.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
import { DocumentsTab } from "../DocumentsTab";

const BASE_NAME = "support-docs";

function doc(
  id: string,
  status: knowledgeSdk.DocumentStatus,
  extra: Partial<knowledgeSdk.KnowledgeDocument> = {},
): knowledgeSdk.KnowledgeDocument {
  return {
    id,
    filename: `${status}.pdf`,
    status,
    error: null,
    chunk_count: 0,
    attempts: 1,
    created_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:05:00Z",
    ...extra,
  };
}

const PROCESSING = doc("11111111-0000-0000-0000-000000000001", "processing");
const PENDING = doc("11111111-0000-0000-0000-000000000002", "pending");
const READY = doc("11111111-0000-0000-0000-000000000003", "ready", { chunk_count: 3 });
const FAILED = doc("11111111-0000-0000-0000-000000000004", "failed", { error: "boom" });
const CANCELLED = doc("11111111-0000-0000-0000-000000000005", "cancelled", {
  error: "cancelled by user",
});

function docPage(documents: knowledgeSdk.KnowledgeDocument[]): knowledgeSdk.DocumentsPage {
  return { documents, total: documents.length, offset: 0, limit: 50 };
}

function renderTab(readonly = false) {
  return render(
    <App>
      <DocumentsTab baseName={BASE_NAME} readScope={undefined} readonly={readonly} />
    </App>,
  );
}

/** Click a Popconfirm's OK button — it shares the trigger's label and
 *  mounts last (portal). */
async function confirmPopover(label: string) {
  const buttons = await screen.findAllByRole("button", { name: label });
  await userEvent.click(buttons[buttons.length - 1]);
}

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

describe("DocumentsTab — cancel ingest (B-222)", () => {
  it("only a pending / processing row can be cancelled", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(
      docPage([PROCESSING, PENDING, READY, FAILED, CANCELLED]),
    );

    renderTab();

    expect(await screen.findByTestId(`doc-cancel-${PROCESSING.id}`)).toBeEnabled();
    expect(screen.getByTestId(`doc-cancel-${PENDING.id}`)).toBeEnabled();
    for (const settled of [READY, FAILED, CANCELLED]) {
      expect(screen.queryByTestId(`doc-cancel-${settled.id}`)).not.toBeInTheDocument();
    }
  });

  it("is labelled 取消入库 in Chinese", async () => {
    await i18n.changeLanguage("zh-CN");
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PROCESSING]));

    renderTab();

    expect(await screen.findByRole("button", { name: "取消入库" })).toBeInTheDocument();
  });

  it("confirming posts the document and refreshes the table", async () => {
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([PROCESSING]))
      .mockResolvedValue(docPage([{ ...PROCESSING, status: "cancelled" }]));
    const cancelSpy = vi
      .spyOn(knowledgeSdk, "cancelDocuments")
      .mockResolvedValue([{ document_id: PROCESSING.id, outcome: "cancelled" }]);

    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-cancel-${PROCESSING.id}`));
    expect(cancelSpy).not.toHaveBeenCalled(); // behind the confirm
    await confirmPopover("Cancel ingest");

    await waitFor(() => expect(cancelSpy).toHaveBeenCalledWith(BASE_NAME, [PROCESSING.id]));
    expect(await screen.findByText("Ingest cancelled")).toBeInTheDocument();
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.queryByTestId(`doc-cancel-${PROCESSING.id}`)).not.toBeInTheDocument();
  });

  it("a document that settled meanwhile says so and refreshes", async () => {
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([PROCESSING]))
      .mockResolvedValue(docPage([{ ...PROCESSING, status: "ready" }]));
    vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue([
      { document_id: PROCESSING.id, outcome: "not_cancellable" },
    ]);

    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-cancel-${PROCESSING.id}`));
    await confirmPopover("Cancel ingest");

    expect(
      await screen.findByText(/already writing its results \(about to finish\) or has finished/),
    ).toBeInTheDocument();
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
  });

  it.each([
    ["not_found", [{ document_id: PROCESSING.id, outcome: "not_found" as const }]],
    ["an empty result", []],
  ])("a document deleted meanwhile (%s) says it no longer exists", async (_, results) => {
    const listSpy = vi
      .spyOn(knowledgeSdk, "listDocuments")
      .mockResolvedValueOnce(docPage([PROCESSING]))
      .mockResolvedValue(docPage([]));
    vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue(results);

    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-cancel-${PROCESSING.id}`));
    await confirmPopover("Cancel ingest");

    expect(await screen.findByText(/no longer exists/)).toBeInTheDocument();
    expect(screen.queryByText(/has finished/)).not.toBeInTheDocument();
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
  });

  it("read-only (cross-tenant): the action is disabled", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PROCESSING]));
    const cancelSpy = vi.spyOn(knowledgeSdk, "cancelDocuments");

    renderTab(true);

    expect(await screen.findByTestId(`doc-cancel-${PROCESSING.id}`)).toBeDisabled();
    expect(cancelSpy).not.toHaveBeenCalled();
  });

  it("a cancelled document has its own status tag", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([CANCELLED]));

    renderTab();

    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
  });
});
