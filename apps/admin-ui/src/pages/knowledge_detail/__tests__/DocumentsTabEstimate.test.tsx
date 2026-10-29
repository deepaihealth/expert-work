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
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import { ApiError } from "../../../api/client";
import * as knowledgeSdk from "../../../api/knowledge";
import { DocumentsTab } from "../DocumentsTab";
import { useEstimateActions } from "../useEstimateActions";

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
  fingerprint: "fp-shown",
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
      expect(all).toHaveBeenCalledWith(BASE_NAME, {
        count: 2,
        totalMicros: 120_000,
        fingerprint: "fp-shown",
      }),
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

    await waitFor(() =>
      expect(cancel).toHaveBeenCalledWith(BASE_NAME, [PAID.id], [
        "estimating",
        "awaiting_confirmation",
      ]),
    );
    expect(await screen.findByText("Withdrew 1 estimate.")).toBeInTheDocument();
  });

  it("「只看待确认」 lists only the waiting statuses", async () => {
    const list = vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PAID]));
    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-only-waiting"));
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(BASE_NAME, { limit: 50, offset: 0 }, undefined, {
        status: ["estimating", "awaiting_confirmation"],
        withReview: true,
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

  it("「全部取消」 withdraws only what was waiting when its confirm opened (review M3)", async () => {
    const late = doc("d0000000-0000-0000-0000-000000000009", "awaiting_confirmation", {
      ingest_estimate: estimate("e9"),
      confirm_by: IN_7_DAYS,
    });
    let waiting = [FREE, PAID];
    vi.spyOn(knowledgeSdk, "listDocuments").mockImplementation(
      async (_base, _page, _scope, options) =>
        options?.status ? docPage(waiting) : docPage([FREE, PAID]),
    );
    const cancel = vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue([]);

    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-withdraw-all"));
    expect(
      await screen.findByText(/Withdraw the 2 estimate\(s\) waiting in this base now/),
    ).toBeInTheDocument();
    waiting = [FREE, PAID, late]; // uploaded after the confirm opened
    await confirmPopover("OK");

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(cancel).toHaveBeenCalledWith(BASE_NAME, [FREE.id, PAID.id], [
      "estimating",
      "awaiting_confirmation",
    ]);
  });

  it("「全部取消」 withdraws an estimating original and its waiting copy in one request (B2b third review)", async () => {
    // Withdrawing the original frees its copy to ``estimating``: a request per
    // status read would then find the copy changed and leave it behind.
    const original = doc("d0000000-0000-0000-0000-000000000008", "estimating");
    const copy = doc("d0000000-0000-0000-0000-000000000006", "awaiting_confirmation", {
      ingest_estimate: estimate("e6", {
        estimated: false,
        items: [],
        duplicate_of: { id: original.id, doc_seq: 8, filename: "8.md" },
      }),
      confirm_by: IN_7_DAYS,
    });
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([original, copy]));
    const cancel = vi
      .spyOn(knowledgeSdk, "cancelDocuments")
      .mockImplementation(async (_base, ids) =>
        ids.map((id) => ({ document_id: id, outcome: "withdrawn_deleted" as const })),
      );

    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-withdraw-all"));
    await screen.findByText(/Withdraw the 2 estimate\(s\) waiting in this base now/);
    await confirmPopover("OK");

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(cancel).toHaveBeenCalledWith(BASE_NAME, [original.id, copy.id], [
      "estimating",
      "awaiting_confirmation",
    ]);
    expect(await screen.findByText("Withdrew 2 estimates.")).toBeInTheDocument();
    expect(screen.queryByText(/changed meanwhile/)).not.toBeInTheDocument();
  });

  it("「全部取消」 leaves one confirmed since it was read, and says so (B2b second review)", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([PAID]));
    vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue([
      { document_id: PAID.id, outcome: "status_changed" },
    ]);

    renderTab();
    await userEvent.click(await screen.findByTestId("doc-estimate-withdraw-all"));
    await screen.findByText(/Withdraw the 1 estimate\(s\) waiting in this base now/);
    await confirmPopover("OK");

    expect(
      await screen.findByText(/1 document changed meanwhile .* and was not withdrawn/),
    ).toBeInTheDocument();
  });

  it("skipping a duplicate on confirm leaves one sent for 「仍入库」 since", async () => {
    const confirmSpy = vi.spyOn(knowledgeSdk, "confirmDocuments").mockResolvedValue([]);
    const cancel = vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue([]);
    const { result } = renderHook(() => useEstimateActions(BASE_NAME, () => undefined), {
      wrapper: ({ children }) => <App>{children}</App>,
    });

    await act(() => result.current.confirm([FREE], [COPY]));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith(BASE_NAME, [COPY.id], ["awaiting_confirmation"]);
  });

  it.each([
    ["re-ingest", "doc-reingest", "reingestDocument"],
    ["recognize", "doc-recognize", "recognizeDocument"],
  ] as const)(
    "a busy %s says to try again once ready, not the endpoint's other conflict",
    async (_name, testid, call) => {
      const ready = doc("d0000000-0000-0000-0000-000000000007", "ready", {
        chunk_count: 3,
        page_stats: {
          pages_total: 2,
          pages_ocr: 1,
          pages_failed: 1,
          pages_pending: 0,
          pages_over_limit: 0,
        },
      });
      vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([ready]));
      vi.spyOn(knowledgeSdk, call).mockRejectedValue(
        new ApiError("busy", "HTTP_409", 409, { error: "document_busy" }),
      );

      renderTab();
      await userEvent.click(await screen.findByTestId(`${testid}-${ready.id}`));

      expect(
        await screen.findByText("This document's new version is being written — try again once it is ready."),
      ).toBeInTheDocument();
    },
  );

  it("「仍入库」 on a duplicate that changed meanwhile says so", async () => {
    vi.spyOn(knowledgeSdk, "listDocuments").mockResolvedValue(docPage([FREE, COPY]));
    vi.spyOn(knowledgeSdk, "estimateDuplicate").mockRejectedValue(
      new ApiError("changed", "HTTP_409", 409, { error: "estimate_changed" }),
    );

    renderTab();
    await userEvent.click(await screen.findByTestId(`doc-ingest-anyway-${COPY.id}`));

    expect(await screen.findByText(/This document changed meanwhile/)).toBeInTheDocument();
    expect(screen.queryByText(/HTTP_409/)).not.toBeInTheDocument();
  });
});

describe("estimate actions — more than one request's worth (review M4)", () => {
  function many(count: number): knowledgeSdk.KnowledgeDocument[] {
    return Array.from({ length: count }, (_, i) =>
      doc(`d${String(i).padStart(7, "0")}-0000-0000-0000-000000000000`, "awaiting_confirmation", {
        ingest_estimate: estimate(`e${i}`),
      }),
    );
  }

  it("confirms and withdraws in chunks of 200 and adds the results up", async () => {
    const confirm = vi
      .spyOn(knowledgeSdk, "confirmDocuments")
      .mockImplementation(async (_base, items) =>
        items.map((item) => ({ document_id: item.documentId, outcome: "confirmed" as const })),
      );
    const cancel = vi.spyOn(knowledgeSdk, "cancelDocuments").mockResolvedValue([]);
    const { result } = renderHook(() => useEstimateActions(BASE_NAME, () => undefined), {
      wrapper: ({ children }) => <App>{children}</App>,
    });

    await act(() => result.current.confirm(many(250), many(201)));

    expect(confirm.mock.calls.map((call) => call[1].length)).toEqual([200, 50]);
    expect(cancel.mock.calls.map((call) => call[1].length)).toEqual([200, 1]);
    expect(await screen.findByText("Confirmed 250 documents; ingesting.")).toBeInTheDocument();
  });
});
