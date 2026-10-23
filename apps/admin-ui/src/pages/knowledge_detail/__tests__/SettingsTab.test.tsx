/**
 * Settings tab — B-206 K-7b: a rerank-score threshold set under another rerank
 * model than the platform's current one is paused, and the tab says so next to
 * the field.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../i18n";

import * as knowledgeSdk from "../../../api/knowledge";
import type { KnowledgeBase, RetrievalConfig } from "../../../api/knowledge";
import { SettingsTab } from "../SettingsTab";

afterEach(() => vi.restoreAllMocks());

function base(config: Partial<RetrievalConfig>): KnowledgeBase {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    name: "support-docs",
    chunk_max_tokens: 512,
    chunk_overlap_tokens: 64,
    created_at: "2026-06-12T00:00:00Z",
    retrieval_config: {
      top_k: 5,
      score_threshold: null,
      method: "hybrid",
      rerank_enabled: true,
      rerank_score_threshold: 0.7,
      ...config,
    },
  };
}

async function renderInChinese(
  kb: KnowledgeBase,
  { onSaved = () => {}, readonly = false }: { onSaved?: () => void; readonly?: boolean } = {},
): Promise<() => Promise<void>> {
  const prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
  render(
    <App>
      <SettingsTab base={kb} onSaved={onSaved} readonly={readonly} />
    </App>,
  );
  return async () => {
    await i18n.changeLanguage(prior);
  };
}

describe("SettingsTab rerank-score threshold binding (B-206 K-7b)", () => {
  it("says which model the threshold was set under and that it is paused", async () => {
    const restore = await renderInChinese(
      base({
        rerank_threshold_model: "qwen/qwen3-vl-rerank",
        rerank_threshold_paused: true,
        current_rerank_model: "qwen/qwen3-rerank",
      }),
    );
    try {
      const notice = screen.getByTestId("kb-settings-rerank-threshold-paused");
      expect(notice).toHaveTextContent(
        "重排分数下限是在 qwen/qwen3-vl-rerank 下标定的，当前重排模型是 qwen/qwen3-rerank",
      );
      expect(notice).toHaveTextContent("已暂停生效，请重新标定");
    } finally {
      await restore();
    }
  });

  it("names an unrecorded calibration model as such", async () => {
    const restore = await renderInChinese(
      base({
        rerank_threshold_model: null,
        rerank_threshold_paused: true,
        current_rerank_model: "qwen/qwen3-rerank",
      }),
    );
    try {
      expect(screen.getByTestId("kb-settings-rerank-threshold-paused")).toHaveTextContent(
        "重排分数下限是在 未记录的模型 下标定的",
      );
    } finally {
      await restore();
    }
  });

  it("says nothing while the threshold is in force", async () => {
    const restore = await renderInChinese(
      base({
        rerank_threshold_model: "qwen/qwen3-vl-rerank",
        rerank_threshold_paused: false,
        current_rerank_model: "qwen/qwen3-vl-rerank",
      }),
    );
    try {
      expect(screen.queryByTestId("kb-settings-rerank-threshold-paused")).toBeNull();
    } finally {
      await restore();
    }
  });
});

const PAUSED: Partial<RetrievalConfig> = {
  rerank_threshold_model: "qwen/qwen3-vl-rerank",
  rerank_threshold_paused: true,
  current_rerank_model: "qwen/qwen3-rerank",
};

describe("SettingsTab sends the rerank-score threshold only to calibrate it (B-206 K-7b)", () => {
  it("leaves an untouched threshold out of the save, so saving other settings does not rebind it", async () => {
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(base({}));
    const restore = await renderInChinese(base(PAUSED));
    try {
      await userEvent.click(screen.getByTestId("kb-settings-save"));
      await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
      expect(update.mock.calls[0][1]).not.toHaveProperty("rerank_score_threshold");
    } finally {
      await restore();
    }
  });

  it("sends an edited threshold, the same value included", async () => {
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(base({}));
    const restore = await renderInChinese(base(PAUSED));
    try {
      const input = screen.getByTestId("kb-settings-rerank-threshold");
      await userEvent.clear(input);
      await userEvent.type(input, "0.7");
      await userEvent.click(screen.getByTestId("kb-settings-save"));
      await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
      expect(update.mock.calls[0][1]).toMatchObject({ rerank_score_threshold: 0.7 });
    } finally {
      await restore();
    }
  });

  it("does not send it again on a later save that did not edit it", async () => {
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(base({}));
    const restore = await renderInChinese(base(PAUSED));
    try {
      const input = screen.getByTestId("kb-settings-rerank-threshold");
      await userEvent.clear(input);
      await userEvent.type(input, "0.6");
      await userEvent.click(screen.getByTestId("kb-settings-save"));
      await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
      await userEvent.click(screen.getByTestId("kb-settings-save"));
      await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
      expect(update.mock.calls[0][1]).toMatchObject({ rerank_score_threshold: 0.6 });
      expect(update.mock.calls[1][1]).not.toHaveProperty("rerank_score_threshold");
    } finally {
      await restore();
    }
  });

  it("confirms a paused threshold under the current model without sending a value", async () => {
    const update = vi.spyOn(knowledgeSdk, "updateBase").mockResolvedValue(base({}));
    const onSaved = vi.fn();
    const restore = await renderInChinese(base(PAUSED), { onSaved });
    try {
      const confirm = screen.getByTestId("kb-settings-rerank-threshold-confirm");
      expect(confirm).toHaveTextContent("按当前模型确认此下限");
      await userEvent.click(confirm);
      await waitFor(() => expect(onSaved).toHaveBeenCalled());
      // No value: confirming must not put back one another admin has changed since.
      expect(update).toHaveBeenCalledWith("support-docs", { rerank_threshold_confirm: true });
    } finally {
      await restore();
    }
  });

  it("offers no confirmation while read-only", async () => {
    const restore = await renderInChinese(base(PAUSED), { readonly: true });
    try {
      expect(screen.getByTestId("kb-settings-rerank-threshold-confirm")).toBeDisabled();
    } finally {
      await restore();
    }
  });
});
