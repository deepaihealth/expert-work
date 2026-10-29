/**
 * A base's retrieval records tab — B-175 P2-PR4.
 *
 * One page at a time from the server (newest first, with the total), read
 * under the page's tenant scope (never "*"); filters by source / outcome /
 * time go to the backend and back to page 1; the retention period is stated;
 * empty, filtered-empty and error states (a 403 says which permission it
 * takes); a late reply for an older filter never lands; a row opens the record.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import dayjs from "dayjs";
import { MemoryRouter } from "react-router-dom";
import "../../../i18n";

import LOGS_FIXTURE from "../../../api/__fixtures__/knowledge_retrieval_logs.json";
import { ApiError } from "../../../api/client";
import * as logsSdk from "../../../api/knowledge_logs";
import { typedFixture } from "../../../test-utils/typedFixture";
import { RetrievalLogsTab, timeRangeFilter } from "../RetrievalLogsTab";

const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

vi.mock("../../../auth/AuthContext", async (importOriginal) => {
  const original = await importOriginal<typeof import("../../../auth/AuthContext")>();
  return {
    ...original,
    useAuth: () => ({
      status: "authenticated" as const,
      identity: {
        kind: "jwt" as const,
        subject: "u1",
        subjectType: "user" as const,
        homeTenantId: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        roles: ["viewer"],
        isSystemAdmin: false,
        homeIsPlatform: false,
        displayName: "u1",
        serverResolved: true,
      },
      token: "t",
      login: () => {},
      logout: () => {},
      refreshIdentity: async () => {},
    }),
  };
});

const PAGE = typedFixture<logsSdk.RetrievalLogsPage>(LOGS_FIXTURE.page);
const DETAIL = typedFixture<logsSdk.RetrievalLogDetail>(LOGS_FIXTURE.detail);
const [ROW] = PAGE.logs;

function page(logs: logsSdk.RetrievalLogSummary[], total = logs.length): logsSdk.RetrievalLogsPage {
  return { logs, total, offset: 0, limit: 20 };
}

function mount(readScope?: string) {
  render(
    <App>
      <MemoryRouter initialEntries={["/knowledge/support-docs/logs"]}>
        <RetrievalLogsTab baseName="support-docs" readScope={readScope} />
      </MemoryRouter>
    </App>,
  );
}

/** Antd Select renders each option twice in jsdom; click the real one. */
const optionContent =
  (label: string) => (_content: string, el: Element | null) =>
    el?.classList.contains("ant-select-item-option-content") === true &&
    el.textContent === label;

async function pick(testId: string, label: string): Promise<void> {
  await userEvent.click(within(screen.getByTestId(testId)).getByRole("combobox"));
  await userEvent.click(await screen.findByText(optionContent(label)));
}

beforeEach(() => {
  scopeRef.current = undefined;
});
afterEach(() => vi.restoreAllMocks());

describe("RetrievalLogsTab", () => {
  it("lists the first page under the page's scope and states the retention period", async () => {
    const list = vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(PAGE);
    mount();
    const table = await screen.findByTestId("kb-logs-table");
    expect(list).toHaveBeenCalledWith("support-docs", {}, { limit: 20, offset: 0 }, undefined);
    const row = within(table).getByText("What is the deductible?").closest("tr")!;
    expect(row).toHaveTextContent("Agent");
    expect(row).toHaveTextContent("Found");
    expect(row).toHaveTextContent("412 ms");
    expect(screen.getByTestId("kb-logs-retention")).toHaveTextContent("kept for 30 days");
  });

  it("tags the rows a platform admin searched, and only those", async () => {
    const staff = { ...ROW, id: "by-staff", flags: { ...ROW.flags, platform_admin: true } };
    vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(page([ROW, staff]));
    mount();
    const tag = await screen.findByTestId("kb-logs-platform-admin-by-staff");
    expect(tag).toHaveTextContent("Platform admin");
    // In the source column, next to the source.
    expect(tag.closest("td")).toHaveTextContent("Agent");
    expect(screen.queryByTestId(`kb-logs-platform-admin-${ROW.id}`)).toBeNull();
  });

  it("reads another tenant's base under that tenant, never all tenants", async () => {
    scopeRef.current = "*";
    const list = vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(PAGE);
    mount("tenant-b");
    await screen.findByTestId("kb-logs-table");
    expect(list).toHaveBeenCalledWith("support-docs", {}, { limit: 20, offset: 0 }, "tenant-b");
    expect(list.mock.calls.every((call) => call[3] !== "*")).toBe(true);
  });

  it("sends each filter to the backend and goes back to page 1", async () => {
    const list = vi
      .spyOn(logsSdk, "listRetrievalLogs")
      .mockResolvedValue(page(PAGE.logs, 45));
    mount();
    await screen.findByTestId("kb-logs-table");
    await userEvent.click(screen.getByTitle("2"));
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith("support-docs", {}, { limit: 20, offset: 20 }, undefined),
    );

    await pick("kb-logs-filter-source", "Agent");
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(
        "support-docs",
        { source: "agent" },
        { limit: 20, offset: 0 },
        undefined,
      ),
    );
    await pick("kb-logs-filter-outcome", "Zero hits");
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(
        "support-docs",
        { source: "agent", zeroHit: true },
        { limit: 20, offset: 0 },
        undefined,
      ),
    );
    await pick("kb-logs-filter-outcome", "Failed");
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith(
        "support-docs",
        { source: "agent", status: "error" },
        { limit: 20, offset: 0 },
        undefined,
      ),
    );
  });

  it("turns a picked time range into an inclusive start and an exclusive end", () => {
    const start = dayjs("2026-10-01T08:00:00Z");
    const end = dayjs("2026-10-02T08:00:00Z");
    expect(timeRangeFilter([start, end])).toEqual({
      since: "2026-10-01T08:00:00.000Z",
      until: "2026-10-02T08:00:00.000Z",
    });
    expect(timeRangeFilter([start, null])).toEqual({ since: "2026-10-01T08:00:00.000Z" });
    expect(timeRangeFilter(null)).toEqual({});
  });

  it("says there are no records yet, and how long they are kept", async () => {
    vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(page([]));
    mount();
    expect(await screen.findByTestId("kb-logs-empty")).toHaveTextContent(
      "No retrieval records yet",
    );
    expect(screen.getByTestId("kb-logs-empty")).toHaveTextContent("30 days");
  });

  it("says nothing matches the filters, and clears them", async () => {
    const list = vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(page([]));
    mount();
    await screen.findByTestId("kb-logs-empty");
    await pick("kb-logs-filter-source", "Retrieval test");
    await waitFor(() =>
      expect(screen.getByTestId("kb-logs-empty")).toHaveTextContent(
        "No records match these filters.",
      ),
    );
    await userEvent.click(screen.getByTestId("kb-logs-clear-filters"));
    await waitFor(() =>
      expect(list).toHaveBeenLastCalledWith("support-docs", {}, { limit: 20, offset: 0 }, undefined),
    );
  });

  it("says which permission a 403 takes, and retries", async () => {
    const list = vi
      .spyOn(logsSdk, "listRetrievalLogs")
      .mockRejectedValueOnce(new ApiError("principal lacks required role", "FORBIDDEN", 403))
      .mockResolvedValue(PAGE);
    mount();
    expect(await screen.findByTestId("kb-logs-error")).toHaveTextContent(
      "read access to conversation records",
    );
    await userEvent.click(screen.getByTestId("kb-logs-retry"));
    await screen.findByTestId("kb-logs-table");
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("kb-logs-error")).toBeNull();
  });

  it("shows any other failure with its code", async () => {
    vi.spyOn(logsSdk, "listRetrievalLogs").mockRejectedValue(new ApiError("boom", "INTERNAL", 500));
    mount();
    expect(await screen.findByTestId("kb-logs-error")).toHaveTextContent("INTERNAL: boom");
  });

  it("never lets a late reply for an older filter land", async () => {
    let releaseFirst: (value: logsSdk.RetrievalLogsPage) => void = () => {};
    const late = { ...ROW, id: "late", query: "stale question" };
    const fresh = { ...ROW, id: "fresh", query: "fresh question" };
    vi.spyOn(logsSdk, "listRetrievalLogs")
      .mockImplementationOnce(
        () => new Promise((resolve) => {
          releaseFirst = resolve;
        }),
      )
      .mockResolvedValue(page([fresh]));
    mount();
    await pick("kb-logs-filter-source", "Agent");
    expect(await screen.findByText("fresh question")).toBeInTheDocument();
    releaseFirst(page([late]));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("stale question")).toBeNull();
    expect(screen.getByText("fresh question")).toBeInTheDocument();
  });

  it("opens a record from its row", async () => {
    vi.spyOn(logsSdk, "listRetrievalLogs").mockResolvedValue(PAGE);
    const detail = vi
      .spyOn(logsSdk, "getRetrievalLog")
      .mockResolvedValue(DETAIL);
    vi.spyOn(logsSdk, "getRetrievalLogPassages").mockResolvedValue([]);
    mount("tenant-b");
    await userEvent.click(await screen.findByTestId(`kb-logs-view-${ROW.id}`));
    expect(await screen.findByTestId("kb-log-query")).toHaveTextContent("What is the deductible?");
    expect(detail).toHaveBeenCalledWith("support-docs", ROW.id, "tenant-b");
  });
});
