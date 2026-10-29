/**
 * DocumentsTab — estimate first, confirm before the ingest starts (B-222 PR-2).
 *
 * A waiting row shows its estimate (hover: the lines) and the days left, with
 * 「确认入库」 even at ¥0.00; a duplicate shows the document it copies and
 * 「仍入库」; the banner adds the base up and confirms / withdraws it all; an
 * ingested row reads 「预估 ¥x · 实际 ¥y」. Read-only (cross-tenant) shows
 * every amount and disables every action.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import { ApiError } from "../../../api/client";
import * as knowledgeSdk from "../../../api/knowledge";
import { DocumentsTab } from "../DocumentsTab";

const BASE_NAME = "support-docs";
const IN_7_DAYS = new Date(Date.now() + 6.5 * 24 * 3600 * 1000).toISOString();

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
    total_micros: 0,
    unpriced: false,
    notes: [],
    counts: { chunks: 3, scanned_pages: 0, figures: 0, pages_over_limit: 0 },
    items: [
      {
        kind: "embedding",
        units: 3,
        unit: "chunks",
        reused_units: 1,
        model: "acme/embed-1",
        input_tokens: 900,
        output_tokens: 0,
        micros: 0,
        basis: "local_count",
        priced: true,
      },
    ],
    confirmed_at: null,
    confirmed_by: null,
    ...extra,
  };
}

function doc(
  id: string,
  status: knowledgeSdk.DocumentStatus,
  extra: Partial<knowledgeSdk.KnowledgeDocument> = {},
): knowledgeSdk.KnowledgeDocument {
  return {
    id,
    filename: `${id.slice(-1)}.md`,
    status,
    error: null,
    chunk_count: 0,
    attempts: 0,
    created_at: "2026-10-28T00:00:00Z",
    updated_at: "2026-10-28T00:00:00Z",
    ...extra,
  };
}

const FREE = doc("d0000000-0000-0000-0000-000000000001", "awaiting_confirmation", {
  ingest_estimate: estimate("e1"),
  confirm_by: IN_7_DAYS,
});
const PAID = doc("d0000000-0000-0000-0000-000000000002", "awaiting_confirmation", {
  ingest_estimate: estimate("e2", { total_micros: 120_000 }),
  confirm_by: IN_7_DAYS,
});
const COPY = doc("d0000000-0000-0000-0000-000000000003", "awaiting_confirmation", {
  ingest_estimate: estimate("e3", {
    estimated: false,
    items: [],
    duplicate_of: { id: FREE.id, doc_seq: 12, filename: "1.md" },
  }),
  confirm_by: IN_7_DAYS,
});
const DONE = doc("d0000000-0000-0000-0000-000000000004", "ready", {
  chunk_count: 3,
  ingest_estimate: estimate("e4", { total_micros: 120_000, confirmed_at: "2026-10-28T00:01:00Z" }),
  ingest_actual: {
    estimate_id: "e4",
    generation: 3,
    currency: "CNY",
    total_micros: 110_000,
    unpriced: false,
    outcome: "ready",
    finished_at: "2026-10-28T00:02:00Z",
  },
});
const SUMMARY: knowledgeSdk.EstimateSummary = {
  estimating: 0,
  awaiting: 2,
  duplicates: 1,
  total_micros: 120_000,
  unpriced: false,
  earliest_confirm_by: IN_7_DAYS,
};

function docPage(
  documents: knowledgeSdk.KnowledgeDocument[],
  summary: knowledgeSdk.EstimateSummary = SUMMARY,
): knowledgeSdk.DocumentsPage {
  return { documents, total: documents.length, offset: 0, limit: 50, estimate_summary: summary };
}

function renderTab(readonly = false) {
  return render(
    <App>
      <DocumentsTab baseName={BASE_NAME} readScope={undefined} readonly={readonly} />
    </App>,
  );
}

async function confirmPopover(label: string) {
  const buttons = await screen.findAllByRole("button", { name: label });
  await userEvent.click(buttons[buttons.length - 1]);
}

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

describe("DocumentsTab — estimates (B-222 PR-2)", () => {
  it("a waiting row shows its estimate and days left, and ¥0.00 can be confirmed", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE, PAID, DONE]));
    const confirmSpy = vi
      .spyOn(knowledgeSdk, "confirmDocuments")
      .mockResolvedValue([{ document_id: FREE.id, outcome: "confirmed" }]);

    renderTab();

    expect(await screen.findByTestId(`doc-cost-${FREE.id}`)).toHaveTextContent("Estimate ¥0.00");
    expect(screen.getByTestId(`doc-cost-${PAID.id}`)).toHaveTextContent("Estimate ¥0.12");
    expect(screen.getByTestId(`doc-days-left-${FREE.id}`)).toHaveTextContent(
      "confirm within 7 days",
    );
    expect(screen.getByTestId(`doc-cost-${DONE.id}`)).toHaveTextContent(
      "Estimate ¥0.12 · Actual ¥0.11",
    );
    expect(screen.queryByTestId(`doc-confirm-${DONE.id}`)).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId(`doc-confirm-${FREE.id}`));

    await waitFor(() =>
      expect(confirmSpy).toHaveBeenCalledWith(BASE_NAME, [
        { documentId: FREE.id, estimateId: "e1" },
      ]),
    );
    expect(await screen.findByText("Confirmed 1 document; ingesting.")).toBeInTheDocument();
  });

  it("the estimate's lines are listed on hover", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE]));
    renderTab();
    const tag = within(await screen.findByTestId(`doc-cost-${FREE.id}`)).getByText(/Estimate/);
    await userEvent.hover(tag);
    const breakdown = await screen.findByTestId("doc-estimate-breakdown");
    expect(breakdown).toHaveTextContent(
      "Embedding: 3 chunks · 900 tokens (1 already embedded, not billed again) — ¥0.00",
    );
  });

  it("a duplicate names its original and is ingested anyway, not confirmed", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([COPY]));
    const anyway = vi
      .spyOn(knowledgeSdk, "estimateDuplicate")
      .mockResolvedValue({ ...COPY, status: "estimating" });

    renderTab();

    expect(await screen.findByTestId(`doc-cost-${COPY.id}`)).toHaveTextContent(
      'Same content as D12 "1.md"',
    );
    expect(screen.queryByTestId(`doc-confirm-${COPY.id}`)).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId(`doc-ingest-anyway-${COPY.id}`));
    await waitFor(() => expect(anyway).toHaveBeenCalledWith(BASE_NAME, COPY.id));
  });

  it("the banner adds the base up and confirms it with what it showed", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE, PAID, COPY]));
    const all = vi.spyOn(knowledgeSdk, "confirmAllDocuments").mockResolvedValue({
      confirmed: [FREE.id, PAID.id],
      withdrawn: [],
      estimate_summary: { ...SUMMARY, awaiting: 0, total_micros: 0 },
    });

    renderTab();

    const banner = await screen.findByTestId("doc-estimate-banner-text");
    expect(banner).toHaveTextContent("2 documents await confirmation, estimated ¥0.12 in total");
    expect(banner).toHaveTextContent("1 duplicate (not ingested, not billed)");
    expect(banner).toHaveTextContent("withdrawn automatically unless confirmed within 7 days");
    await userEvent.click(screen.getByTestId("doc-estimate-confirm-all"));
    await waitFor(() =>
      expect(all).toHaveBeenCalledWith(BASE_NAME, { count: 2, totalMicros: 120_000 }),
    );
  });

  it("a confirm-all that no longer adds up asks again", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE, PAID]));
    vi.spyOn(knowledgeSdk, "confirmAllDocuments").mockRejectedValue(
      new ApiError("changed", "HTTP_409", 409, { error: "estimate_changed" }),
    );

    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-confirm-all"));

    expect(
      await screen.findByText(/The documents awaiting confirmation changed/),
    ).toBeInTheDocument();
  });

  it("withdrawing posts a cancel and says what it did", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PAID]));
    const cancel = vi
      .spyOn(knowledgeSdk, "cancelDocuments")
      .mockResolvedValue([{ document_id: PAID.id, outcome: "withdrawn_deleted" }]);

    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-withdraw-${PAID.id}`));
    expect(cancel).not.toHaveBeenCalled(); // behind the confirm
    await confirmPopover("OK");

    await waitFor(() => expect(cancel).toHaveBeenCalledWith(BASE_NAME, [PAID.id]));
    expect(await screen.findByText("Withdrew 1 estimate.")).toBeInTheDocument();
  });

  it("「只看待确认」 lists only the waiting statuses", async () => {
    const list = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PAID]));
    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-only-waiting"));
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(BASE_NAME, { limit: 50, offset: 0 }, undefined, {
        status: ["estimating", "awaiting_confirmation"],
      }),
    );
  });

  it("read-only shows the amounts and disables every action", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE, COPY]));

    renderTab(true);

    expect(await screen.findByTestId(`doc-cost-${FREE.id}`)).toHaveTextContent("Estimate ¥0.00");
    expect(screen.getByTestId(`doc-confirm-${FREE.id}`)).toBeDisabled();
    expect(screen.getByTestId(`doc-withdraw-${FREE.id}`)).toBeDisabled();
    expect(screen.getByTestId(`doc-ingest-anyway-${COPY.id}`)).toBeDisabled();
    expect(screen.getByTestId("doc-estimate-confirm-all")).toBeDisabled();
    expect(screen.getByTestId("doc-estimate-withdraw-all")).toBeDisabled();
    expect(screen.getByTestId("doc-estimate-banner-text")).toHaveTextContent("¥0.12");
  });

  it("is worded in Chinese", async () => {
    await i18n.changeLanguage("zh-CN");
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PAID, COPY]));
    renderTab();
    expect(await screen.findByTestId(`doc-cost-${PAID.id}`)).toHaveTextContent("预估 ¥0.12");
    expect(screen.getByTestId(`doc-cost-${COPY.id}`)).toHaveTextContent(
      "已有同样内容的文档 D12《1.md》，默认不入库",
    );
    expect(screen.getByTestId("doc-estimate-banner-text")).toHaveTextContent(
      "2 份文档等待确认入库，预估合计 ¥0.12",
    );
    expect(screen.getByTestId(`doc-confirm-${PAID.id}`)).toHaveTextContent("确认入库");
    expect(screen.getByTestId(`doc-ingest-anyway-${COPY.id}`)).toHaveTextContent("仍入库");
  });
});
