/**
 * PassageCard — B-175 P3-PR3: one 整块 with its state, its chunks folded
 * below, and the switch / keep-off / edit / history actions (writes disabled
 * while read-only or while the document is being ingested).
 */
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import i18n from "../../../../i18n";

import type { Passage } from "../../../../api/knowledge_maintenance";
import { PassageCard } from "../PassageCard";
import { BASE_NAME, fixturePassages } from "./fixtures";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});

function renderCard(
  passage: Passage,
  { readonly = false, busy = false }: { readonly?: boolean; busy?: boolean } = {},
) {
  const handlers = { onSwitch: vi.fn(), onEdit: vi.fn(), onHistory: vi.fn() };
  render(
    <PassageCard
      baseName={BASE_NAME}
      documentId="d1"
      readScope={undefined}
      passage={passage}
      figureIds={new Set()}
      readonly={readonly}
      busy={busy}
      {...handlers}
    />,
  );
  return handlers;
}

describe("PassageCard", () => {
  it("names the 整块, its size, its chunks and that it was edited", () => {
    const [parent] = fixturePassages();
    renderCard(parent);
    expect(screen.getByTestId(`kb-passage-meta-${parent.id}`)).toHaveTextContent(
      `整块 #0 · ${parent.content.length} 字 · 2 个小段`,
    );
    expect(screen.getByTestId(`kb-passage-edited-${parent.id}`)).toHaveTextContent(
      "已编辑 · 修订 2",
    );
    expect(screen.getByTestId(`kb-passage-heading-${parent.id}`)).toHaveTextContent(
      "Chapter 1 › Section 1.1",
    );
    expect(screen.getByTestId(`kb-passage-toggle-${parent.id}`)).toBeChecked();
    expect(screen.queryByTestId(`kb-passage-inherited-${parent.id}`)).toBeNull();
    expect(screen.queryByTestId(`kb-passage-keep-off-${parent.id}`)).toBeNull();
  });

  it("folds its chunks until asked", async () => {
    const [parent] = fixturePassages();
    renderCard(parent);
    const [first, second] = parent.children;
    expect(screen.queryByTestId(`kb-passage-child-${first.id}`)).toBeNull();
    await userEvent.click(screen.getByTestId(`kb-passage-children-toggle-${parent.id}`));
    expect(screen.getByTestId(`kb-passage-child-${first.id}`)).toHaveTextContent("小段 #0");
    expect(screen.getByTestId(`kb-passage-child-${second.id}`)).toHaveTextContent(
      "Sample paragraph two, edited.",
    );
  });

  it("flags a passage switched off by inheritance and offers to keep it off or switch it on", async () => {
    const [, inherited] = fixturePassages();
    const { onSwitch } = renderCard(inherited);
    expect(screen.getByTestId(`kb-passage-meta-${inherited.id}`)).toHaveTextContent(
      `小段 #1 · ${inherited.content.length} 字 · 单独成块`,
    );
    expect(screen.getByTestId(`kb-passage-inherited-${inherited.id}`)).toHaveTextContent(
      "跟着停用 · 待复核",
    );
    // A lone chunk has no chunks to fold.
    expect(screen.queryByTestId(`kb-passage-children-toggle-${inherited.id}`)).toBeNull();

    await userEvent.click(screen.getByTestId(`kb-passage-keep-off-${inherited.id}`));
    expect(onSwitch).toHaveBeenLastCalledWith(inherited, false);
    await userEvent.click(screen.getByTestId(`kb-passage-toggle-${inherited.id}`));
    expect(onSwitch).toHaveBeenLastCalledWith(inherited, true);
  });

  it("a manual switch-off is just off: no review tag, no keep-off", () => {
    const [parent] = fixturePassages();
    const off = { ...parent, enabled: false, disabled_reason: "manual" as const };
    renderCard(off);
    const card = screen.getByTestId(`kb-passage-card-${parent.id}`);
    expect(within(card).getByText("已停用")).toBeInTheDocument();
    expect(screen.queryByTestId(`kb-passage-inherited-${parent.id}`)).toBeNull();
    expect(screen.queryByTestId(`kb-passage-keep-off-${parent.id}`)).toBeNull();
    expect(screen.getByTestId(`kb-passage-toggle-${parent.id}`)).not.toBeChecked();
  });

  it("read-only: every write is disabled behind the read-only hint; history still opens", async () => {
    const [, inherited] = fixturePassages();
    const { onHistory } = renderCard(inherited, { readonly: true });
    expect(screen.getByTestId(`kb-passage-toggle-${inherited.id}`)).toBeDisabled();
    expect(screen.getByTestId(`kb-passage-edit-${inherited.id}`)).toBeDisabled();
    expect(screen.getByTestId(`kb-passage-keep-off-${inherited.id}`)).toBeDisabled();
    expect(screen.getAllByTestId("readonly-tooltip")).toHaveLength(3);
    await userEvent.click(screen.getByTestId(`kb-passage-history-${inherited.id}`));
    expect(onHistory).toHaveBeenCalledWith(inherited);
  });

  it("while the document is being ingested the writes wait", async () => {
    const [parent] = fixturePassages();
    const { onEdit } = renderCard(parent, { busy: true });
    expect(screen.getByTestId(`kb-passage-edit-${parent.id}`)).toBeDisabled();
    expect(screen.getByTestId(`kb-passage-toggle-${parent.id}`)).toBeDisabled();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("edit hands the passage over", async () => {
    const [parent] = fixturePassages();
    const { onEdit } = renderCard(parent);
    await userEvent.click(screen.getByTestId(`kb-passage-edit-${parent.id}`));
    expect(onEdit).toHaveBeenCalledWith(parent);
  });
});
