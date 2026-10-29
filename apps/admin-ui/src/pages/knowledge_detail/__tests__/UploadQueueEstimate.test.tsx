/**
 * Upload queue — every upload is estimated, then waits (B-222 PR-2).
 *
 * The queue reads its own documents (``documentIds``) and shows each row's
 * server status and cost, a batch line 「本批 N 份：待确认 …，预估合计 …；副本 …」
 * and 「确认 N 份 · 跳过 M 份副本」, which confirms the estimated ones by their
 * estimate id and withdraws the duplicates.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
import { DocumentsTab } from "../DocumentsTab";

const BASE_NAME = "support-docs";

function estimate(
  id: string,
  extra: Partial<knowledgeSdk.IngestEstimate> = {},
): knowledgeSdk.IngestEstimate {
  return {
    id,
    origin: "upload",
    estimated: true,
    created_at: "2026-10-28T00:00:00Z",
    duplicate_of: null,
    allow_duplicate: false,
    currency: "CNY",
    total_micros: 50_000,
    unpriced: false,
    notes: [],
    counts: { chunks: 2, scanned_pages: 0, figures: 0, pages_over_limit: 0 },
    items: [],
    confirmed_at: null,
    confirmed_by: null,
    ...extra,
  };
}

function waiting(
  id: string,
  filename: string,
  extra: Partial<knowledgeSdk.IngestEstimate> = {},
): knowledgeSdk.KnowledgeDocument {
  return {
    id,
    filename,
    status: "awaiting_confirmation",
    error: null,
    chunk_count: 0,
    created_at: null,
    updated_at: null,
    confirm_by: new Date(Date.now() + 6 * 24 * 3600 * 1000).toISOString(),
    ingest_estimate: estimate(`e-${id}`, extra),
  };
}

const A = waiting("a0000000-0000-0000-0000-000000000001", "a.md");
const B = waiting("a0000000-0000-0000-0000-000000000002", "b.md", { total_micros: 70_000 });
const COPY = waiting("a0000000-0000-0000-0000-000000000003", "c.md", {
  estimated: false,
  total_micros: 0,
  duplicate_of: { id: A.id, doc_seq: 1, filename: "a.md" },
});
const BY_NAME: Record<string, knowledgeSdk.KnowledgeDocument> = {
  "a.md": A,
  "b.md": B,
  "c.md": COPY,
};

function renderTab() {
  return render(
    <App>
      <DocumentsTab baseName={BASE_NAME} readScope={undefined} readonly={false} />
    </App>,
  );
}

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

describe("upload queue — estimates (B-222 PR-2)", () => {
  it("follows its documents and confirms the batch, skipping the duplicate", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockImplementation(
      async (_base, _page, _scope, options) => {
        const ids = options?.documentIds ?? [];
        const documents = ids.length
          ? [A, B, COPY].filter((d) => ids.includes(d.id))
          : [];
        return { documents, total: documents.length, offset: 0, limit: 50 };
      },
    );
    vi.spyOn(knowledgeSdk, "uploadDocument").mockImplementation(async (_base, file) => ({
      ...BY_NAME[file.name],
      status: "estimating",
      ingest_estimate: null,
      unchanged: false,
    }));
    const confirm = vi
      .spyOn(knowledgeSdk, "confirmDocuments")
      .mockResolvedValue([
        { document_id: A.id, outcome: "confirmed" },
        { document_id: B.id, outcome: "confirmed" },
      ]);
    const cancel = vi
      .spyOn(knowledgeSdk, "cancelDocuments")
      .mockResolvedValue([{ document_id: COPY.id, outcome: "withdrawn_deleted" }]);

    renderTab();
    await screen.findByTestId("doc-upload-dragger");
    fireEvent.change(screen.getByTestId("doc-upload-dragger"), {
      target: { files: ["a.md", "b.md", "c.md"].map((name) => new File(["x"], name)) },
    });

    const line = await screen.findByTestId("doc-upload-batch-text");
    await waitFor(() =>
      expect(line).toHaveTextContent(
        "This batch (3 files): 2 await confirmation, estimated ¥0.12 in total; 1 duplicate (not ingested, not billed)",
      ),
    );
    expect(screen.getByTestId(`doc-upload-server-${A.id}`)).toHaveAttribute(
      "data-server-status",
      "awaiting_confirmation",
    );
    const button = screen.getByTestId("doc-upload-batch-confirm");
    expect(button).toHaveTextContent("Confirm 2 · skip 1 duplicate(s)");

    await userEvent.click(button);

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith(BASE_NAME, [
        { documentId: A.id, estimateId: `e-${A.id}` },
        { documentId: B.id, estimateId: `e-${B.id}` },
      ]),
    );
    await waitFor(() => expect(cancel).toHaveBeenCalledWith(BASE_NAME, [COPY.id]));
  });
});
