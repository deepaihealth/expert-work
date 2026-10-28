/**
 * SegmentPreviewDrawer — B-175 P3-PR3: opens on the passages; reads what
 * waits for review when a document opens (the 「待复核（n）」 view and an alert
 * pointing to it); an edit saved in its editor reads the passages and the
 * review again; a document being ingested takes no writes.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import type { KnowledgeDocument } from "../../../api/knowledge";
import * as sdk from "../../../api/knowledge_maintenance";
import { SegmentPreviewDrawer, type DrawerView } from "../SegmentPreviewDrawer";
import {
  BASE_NAME,
  fixturePage,
  fixturePassages,
  readyDocument,
  revision,
} from "../passages/__tests__/fixtures";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

function renderDrawer(
  document: KnowledgeDocument = readyDocument(),
  { initialView = "passages" as DrawerView } = {},
) {
  render(
    <App>
      <SegmentPreviewDrawer
        baseName={BASE_NAME}
        readScope={undefined}
        document={document}
        onClose={() => {}}
        canDelete
        initialView={initialView}
      />
    </App>,
  );
}

function threeToReview(): sdk.UnappliedEdits {
  const [, inherited] = fixturePassages();
  return {
    inherited: [inherited],
    unapplied: [
      revision({ passage_id: "a", revision: 2, edited_by: "user-a", orphaned_at: "2026-10-20T00:00:00Z" }),
      revision({ passage_id: "b", revision: 2, edited_by: "user-a", orphaned_at: "2026-10-20T00:00:00Z" }),
    ],
  };
}

describe("SegmentPreviewDrawer (B-175 P3-PR3)", () => {
  it("counts what waits for review and points to it", async () => {
    vi.spyOn(sdk, "listPassages").mockResolvedValue(fixturePage());
    const review = vi.spyOn(sdk, "getUnappliedEdits").mockResolvedValue(threeToReview());
    const doc = readyDocument();
    renderDrawer(doc);

    const alert = await screen.findByTestId("kb-review-alert");
    expect(alert).toHaveTextContent("这份文档有 3 处待复核");
    expect(review).toHaveBeenCalledWith(BASE_NAME, doc.id, undefined);
    expect(screen.getByRole("radio", { name: "待复核（3）" })).not.toBeChecked();

    await userEvent.click(screen.getByTestId("kb-review-open"));
    expect(await screen.findByTestId("kb-review-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-review-alert")).toBeNull();
    expect(screen.getByRole("radio", { name: "待复核（3）" })).toBeChecked();
  });

  it("says nothing when nothing waits", async () => {
    vi.spyOn(sdk, "listPassages").mockResolvedValue(fixturePage());
    vi.spyOn(sdk, "getUnappliedEdits").mockResolvedValue({ inherited: [], unapplied: [] });
    renderDrawer();
    await screen.findByTestId("kb-passage-list");
    await waitFor(() => expect(screen.getByRole("radio", { name: "待复核（0）" })).toBeInTheDocument());
    expect(screen.queryByTestId("kb-review-alert")).toBeNull();
  });

  it("opens on 待复核 when asked (the row's tag)", async () => {
    vi.spyOn(sdk, "getUnappliedEdits").mockResolvedValue(threeToReview());
    const list = vi.spyOn(sdk, "listPassages");
    renderDrawer(readyDocument(), { initialView: "review" });
    expect(await screen.findByTestId("kb-review-panel")).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
  });

  it("an edit saved in the editor reads the passages and the review again", async () => {
    const [parent] = fixturePassages();
    const list = vi.spyOn(sdk, "listPassages").mockResolvedValue(fixturePage());
    const review = vi
      .spyOn(sdk, "getUnappliedEdits")
      .mockResolvedValue({ inherited: [], unapplied: [] });
    vi.spyOn(sdk, "updatePassage").mockResolvedValue({ ...parent, content: "New.", revision: 3 });
    renderDrawer();

    await userEvent.click(await screen.findByTestId(`kb-passage-edit-${parent.id}`));
    const area = await screen.findByTestId("kb-passage-editor-content");
    await userEvent.clear(area);
    await userEvent.type(area, "New.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(review).toHaveBeenCalledTimes(2);
  });

  it("a document being ingested takes no writes, with the reason", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "listPassages").mockResolvedValue(fixturePage());
    vi.spyOn(sdk, "getUnappliedEdits").mockResolvedValue({ inherited: [], unapplied: [] });
    renderDrawer(readyDocument({ status: "processing" }));
    expect(await screen.findByTestId(`kb-passage-edit-${parent.id}`)).toBeDisabled();
    expect(screen.getByTestId("kb-passage-add")).toBeDisabled();
    // Not the read-only hint: this one says why.
    expect(screen.queryByTestId("readonly-tooltip")).toBeNull();
  });
});
