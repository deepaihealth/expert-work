/**
 * ReviewPanel — B-175 P3-PR3: what a re-ingest left for a person. Passages
 * switched off by inheritance (switch on, or keep off: either settles it) and
 * edits / switch-offs it could not put back (discard all: tenant admins only,
 * it deletes history). Read-only viewers see it all, with writes disabled.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import * as sdk from "../../../../api/knowledge_maintenance";
import type { UnappliedEdits } from "../../../../api/knowledge_maintenance";
import { ReviewPanel } from "../ReviewPanel";
import { BASE_NAME, fixturePassages, revision } from "./fixtures";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

const ORPHANED_AT = "2026-10-20T08:00:00Z";

function review(): UnappliedEdits {
  const [, inherited] = fixturePassages();
  return {
    inherited: [inherited],
    unapplied: [
      revision({
        passage_id: "gone-edit",
        revision: 2,
        content: "An edit the file lost.",
        edited_by: "user-a",
        orphaned_at: ORPHANED_AT,
      }),
      revision({
        passage_id: "gone-switch",
        revision: 0,
        content: "A switched-off paragraph.",
        orphaned_at: ORPHANED_AT,
        switch_off_lost: true,
      }),
    ],
  };
}

function renderPanel({
  data = review(),
  readonly = false,
  canDelete = true,
}: { data?: UnappliedEdits; readonly?: boolean; canDelete?: boolean } = {}) {
  const handlers = {
    onChanged: vi.fn(),
    onEdit: vi.fn(),
    onHistory: vi.fn(),
  };
  render(
    <App>
      <ReviewPanel
        baseName={BASE_NAME}
        documentId="d1"
        readScope={undefined}
        review={data}
        figureIds={new Set()}
        readonly={readonly}
        busy={false}
        canDelete={canDelete}
        {...handlers}
      />
    </App>,
  );
  return handlers;
}

describe("ReviewPanel", () => {
  it("lists the inherited passages and what was not put back, each kind named", () => {
    const [, inherited] = fixturePassages();
    renderPanel();
    expect(screen.getByTestId("kb-review-inherited")).toHaveTextContent("跟着停用的整块（1）");
    expect(screen.getByTestId(`kb-passage-card-${inherited.id}`)).toBeInTheDocument();
    expect(screen.getByTestId("kb-review-unapplied")).toHaveTextContent("没套回去的修改（2）");
    const edit = screen.getByTestId("kb-review-unapplied-gone-edit-2");
    expect(edit).toHaveTextContent("修改 · 修订 2");
    expect(edit).toHaveTextContent("An edit the file lost.");
    expect(edit).toHaveTextContent(new Date(ORPHANED_AT).toLocaleString());
    const record = screen.getByTestId("kb-review-unapplied-gone-switch-0");
    expect(record).toHaveTextContent("停用记录");
    expect(record).toHaveTextContent("新文件里已经没有这段原文");
  });

  it("switching an inherited passage on settles it and reports the change", async () => {
    const [, inherited] = fixturePassages();
    const update = vi
      .spyOn(sdk, "updatePassage")
      .mockResolvedValue({ ...inherited, enabled: true, disabled_reason: null });
    const { onChanged } = renderPanel();

    await userEvent.click(screen.getByTestId(`kb-passage-toggle-${inherited.id}`));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(update).toHaveBeenCalledWith(BASE_NAME, "d1", inherited.id, { enabled: true });
  });

  it("keeping it off is the person's own switch-off from then on", async () => {
    const [, inherited] = fixturePassages();
    const update = vi
      .spyOn(sdk, "updatePassage")
      .mockResolvedValue({ ...inherited, disabled_reason: "manual" });
    const { onChanged } = renderPanel();

    await userEvent.click(screen.getByTestId(`kb-passage-keep-off-${inherited.id}`));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(update).toHaveBeenCalledWith(BASE_NAME, "d1", inherited.id, { enabled: false });
  });

  it("discard all asks first, then deletes the unapplied history", async () => {
    const discard = vi.spyOn(sdk, "discardUnappliedEdits").mockResolvedValue(3);
    const { onChanged } = renderPanel();

    await userEvent.click(screen.getByTestId("kb-review-discard"));
    const ok = await screen.findAllByRole("button", { name: /全部丢弃|丢 弃/ });
    await userEvent.click(ok[ok.length - 1]);

    await waitFor(() => expect(discard).toHaveBeenCalledWith(BASE_NAME, "d1"));
    expect(onChanged).toHaveBeenCalled();
    expect(await screen.findByText("已丢弃没套回去的内容。")).toBeInTheDocument();
  });

  it("without manifest:delete the discard is disabled and says who may", async () => {
    const discard = vi.spyOn(sdk, "discardUnappliedEdits");
    renderPanel({ canDelete: false });
    const button = screen.getByTestId("kb-review-discard");
    expect(button).toBeDisabled();
    expect(screen.getByTestId("kb-review-discard-needs-admin")).toBeInTheDocument();
    expect(discard).not.toHaveBeenCalled();
  });

  it("read-only: everything is listed, every write disabled behind the read-only hint", () => {
    const [, inherited] = fixturePassages();
    renderPanel({ readonly: true });
    expect(screen.getByTestId("kb-review-unapplied-gone-edit-2")).toBeInTheDocument();
    expect(screen.getByTestId("kb-review-discard")).toBeDisabled();
    expect(screen.getByTestId(`kb-passage-toggle-${inherited.id}`)).toBeDisabled();
    expect(screen.getByTestId(`kb-passage-keep-off-${inherited.id}`)).toBeDisabled();
    expect(screen.getAllByTestId("readonly-tooltip").length).toBeGreaterThanOrEqual(3);
  });

  it("nothing to review says so", () => {
    renderPanel({ data: { inherited: [], unapplied: [] } });
    expect(screen.getByText("没有待复核的内容。")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-review-discard")).toBeNull();
  });
});
