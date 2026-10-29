/**
 * SettingsTab — B-175 P3-PR3 (B-206 §6 库设置 / 「保存即问」).
 *
 * The tab says how many documents are still cut by older chunking parameters
 * and offers to re-ingest them. Saving changed chunking parameters asks right
 * away when documents are stale; other saves do not ask. The description box
 * says agents read it; the passage cap shows read-only. Read-only viewers see
 * the count, with the action disabled.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
import type { KnowledgeBase } from "../../../api/knowledge";
import * as sdk from "../../../api/knowledge_maintenance";
import { SettingsTab } from "../SettingsTab";

const TENANT = "33333333-3333-3333-3333-333333333333";

const BASE: KnowledgeBase = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "support-docs",
  chunk_max_tokens: 400,
  chunk_overlap_tokens: 0,
  parent_max_tokens: 1024,
  chunk_params_hash: "hash-400",
  created_at: "2026-06-12T00:00:00Z",
  description: "FAQ",
  retrieval_config: { top_k: 5, score_threshold: null, method: "hybrid", rerank_enabled: true },
};

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

function renderTab({ readonly = false, onSaved = vi.fn() } = {}) {
  render(
    <App>
      <SettingsTab base={BASE} onSaved={onSaved} readonly={readonly} readScope={TENANT} />
    </App>,
  );
  return onSaved;
}

function staleCounts(...counts: number[]) {
  const spy = vi.spyOn(sdk, "getStaleDocuments");
  for (const stale of counts) spy.mockResolvedValueOnce({ stale, chunk_params_hash: "h" });
  return spy.mockResolvedValue({ stale: counts[counts.length - 1] ?? 0, chunk_params_hash: "h" });
}

async function setChunkCap(value: string) {
  const field = screen.getByRole("spinbutton", { name: "小段上限（token）" });
  await userEvent.clear(field);
  await userEvent.type(field, value);
}

describe("SettingsTab — stale documents (B-175 P3-PR3)", () => {
  it("says how many documents use older parameters, read with the read scope", async () => {
    const stale = staleCounts(2);
    renderTab();
    expect(await screen.findByTestId("kb-settings-stale")).toHaveTextContent(
      "2 份文档还是按旧的切块参数切的。",
    );
    expect(stale).toHaveBeenCalledWith(BASE.name, TENANT);
    expect(screen.getByTestId("kb-settings-reingest-stale")).toBeEnabled();
  });

  it("with none stale says so and offers nothing", async () => {
    staleCounts(0);
    renderTab();
    expect(await screen.findByTestId("kb-settings-stale")).toHaveTextContent(
      "所有文档都按当前的切块参数切好了。",
    );
    expect(screen.queryByTestId("kb-settings-reingest-stale")).toBeNull();
  });

  it("saving changed chunking asks right away; yes re-ingests and says what was submitted", async () => {
    staleCounts(0, 2);
    vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);
    const reingest = vi
      .spyOn(sdk, "reingestStale")
      .mockResolvedValue({ queued: ["d1"], skipped: [{ id: "d2", reason: "content_missing" }], remaining: 3 });
    const onSaved = renderTab();
    await screen.findByTestId("kb-settings-stale");

    await setChunkCap("300");
    await userEvent.click(screen.getByTestId("kb-settings-save"));

    // One dialog; antd's confirm carries its title twice (heading + aria title).
    expect(
      await screen.findAllByText("2 份文档还是按旧的切块参数切的，现在全部重新摄取吗？"),
    ).toHaveLength(2);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(onSaved).not.toHaveBeenCalled();
    await userEvent.click(screen.getByTestId("kb-stale-confirm-ok"));

    await waitFor(() => expect(reingest).toHaveBeenCalledWith(BASE.name));
    expect(await screen.findByText("已提交 1 份重新摄取，正在估算费用；估算完成后在「文档」页确认才会开始。")).toBeInTheDocument();
    expect(await screen.findByText("1 份没保留原文件，已跳过，需要重新上传。")).toBeInTheDocument();
    expect(await screen.findByText("还有 3 份没提交，再点一次「全部重新摄取」。")).toBeInTheDocument();
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("Save stays busy through the whole sequence, question included", async () => {
    staleCounts(0, 2);
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);
    const onSaved = renderTab();
    await screen.findByTestId("kb-settings-stale");

    await setChunkCap("300");
    await userEvent.click(screen.getByTestId("kb-settings-save"));
    await screen.findAllByText("2 份文档还是按旧的切块参数切的，现在全部重新摄取吗？");

    const save = screen.getByTestId("kb-settings-save");
    expect(save).toHaveClass("ant-btn-loading");
    await userEvent.click(save);
    expect(update).toHaveBeenCalledTimes(1);

    await userEvent.click(await screen.findByRole("button", { name: "以后再说" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    await waitFor(() =>
      expect(screen.getByTestId("kb-settings-save")).not.toHaveClass("ant-btn-loading"),
    );
  });

  it("「以后再说」 re-ingests nothing; the save still lands", async () => {
    staleCounts(0, 2);
    vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);
    const reingest = vi.spyOn(sdk, "reingestStale");
    const onSaved = renderTab();
    await screen.findByTestId("kb-settings-stale");

    await setChunkCap("300");
    await userEvent.click(screen.getByTestId("kb-settings-save"));
    await userEvent.click(await screen.findByRole("button", { name: "以后再说" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(reingest).not.toHaveBeenCalled();
  });

  it("a save that did not change chunking does not ask", async () => {
    const stale = staleCounts(2);
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);
    const onSaved = renderTab();
    await screen.findByTestId("kb-settings-stale");

    await userEvent.click(screen.getByTestId("kb-settings-save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(update).toHaveBeenCalled();
    expect(stale).toHaveBeenCalledTimes(1); // the count shown on opening, nothing more
    expect(screen.queryByText(/现在全部重新摄取吗/)).toBeNull();
  });

  it("changed chunking with nothing stale does not ask", async () => {
    staleCounts(0, 0);
    vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(BASE);
    const onSaved = renderTab();
    await screen.findByTestId("kb-settings-stale");
    await setChunkCap("300");
    await userEvent.click(screen.getByTestId("kb-settings-save"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(screen.queryByText(/现在全部重新摄取吗/)).toBeNull();
  });

  it("asks to re-upload only what has no file, and says a busy one is skipped for now (B-222)", async () => {
    staleCounts(3, 2);
    vi.spyOn(sdk, "reingestStale").mockResolvedValue({
      queued: ["d1"],
      skipped: [
        { id: "d2", reason: "content_missing" },
        { id: "d3", reason: "busy" },
      ],
      remaining: 0,
    });
    renderTab();
    await userEvent.click(await screen.findByTestId("kb-settings-reingest-stale"));
    await userEvent.click(await screen.findByTestId("kb-stale-confirm-ok"));

    expect(await screen.findByText("1 份没保留原文件，已跳过，需要重新上传。")).toBeInTheDocument();
    expect(await screen.findByText("1 份正在写入新版本，已跳过，稍后再试。")).toBeInTheDocument();
    expect(screen.queryByText("2 份没保留原文件，已跳过，需要重新上传。")).toBeNull();
  });

  it("the line's button asks too, then re-ingests", async () => {
    staleCounts(2, 0);
    const reingest = vi
      .spyOn(sdk, "reingestStale")
      .mockResolvedValue({ queued: ["d1", "d2"], skipped: [], remaining: 0 });
    renderTab();
    await userEvent.click(await screen.findByTestId("kb-settings-reingest-stale"));
    await userEvent.click(await screen.findByTestId("kb-stale-confirm-ok"));
    await waitFor(() => expect(reingest).toHaveBeenCalled());
    expect(await screen.findByText("已提交 2 份重新摄取，正在估算费用；估算完成后在「文档」页确认才会开始。")).toBeInTheDocument();
    expect(await screen.findByText("所有文档都按当前的切块参数切好了。")).toBeInTheDocument();
  });

  it("read-only: the count shows, the action is disabled behind the read-only hint", async () => {
    staleCounts(2);
    renderTab({ readonly: true });
    expect(await screen.findByTestId("kb-settings-reingest-stale")).toBeDisabled();
    expect(
      within(screen.getByTestId("kb-settings-stale")).getByTestId("readonly-tooltip"),
    ).toBeInTheDocument();
  });
});

describe("SettingsTab — 库设置 (B-175 P3-PR3)", () => {
  it("says agents read the description and shows the passage cap read-only", async () => {
    staleCounts(0);
    renderTab();
    expect(screen.getByTestId("kb-settings-description-hint")).toHaveTextContent(
      "智能体按这段描述决定查哪个库",
    );
    expect(screen.getByTestId("kb-settings-parent-max")).toHaveTextContent(
      "整块上限：1024 token",
    );
    expect(screen.getByRole("spinbutton", { name: "小段上限（token）" })).toHaveValue("400");
  });
});
