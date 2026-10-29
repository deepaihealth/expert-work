/**
 * PassageEditModal — B-175 P3-PR3: edit a passage's text against the
 * revision it was read at, or add one. A 409 ``revision_conflict`` keeps the
 * editor open with the server's copy: use theirs, or overwrite with mine
 * (sent against the server's revision). Other refusals say what happened.
 */
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import i18n from "../../../../i18n";

import { ApiError } from "../../../../api/client";
import * as sdk from "../../../../api/knowledge_maintenance";
import type { Passage } from "../../../../api/knowledge_maintenance";
import { PassageEditModal, type EditTarget } from "../PassageEditModal";
import { BASE_NAME, fixturePassages } from "./fixtures";

let prior = "en";
beforeAll(async () => {
  prior = i18n.language;
  await i18n.changeLanguage("zh-CN");
});
afterAll(async () => {
  await i18n.changeLanguage(prior);
});
afterEach(() => vi.restoreAllMocks());

function renderEditor(target: EditTarget) {
  const handlers = { onClose: vi.fn(), onSaved: vi.fn(), onGone: vi.fn(), onConflict: vi.fn() };
  render(
    <App>
      <PassageEditModal baseName={BASE_NAME} documentId="d1" target={target} {...handlers} />
    </App>,
  );
  return handlers;
}

function conflictError(passage: Passage): ApiError {
  return new ApiError("conflict", "HTTP_409", 409, { error: "revision_conflict", passage });
}

async function replaceText(text: string) {
  const area = screen.getByTestId("kb-passage-editor-content");
  await userEvent.clear(area);
  await userEvent.type(area, text);
}

describe("PassageEditModal — edit", () => {
  it("sends the new text against the revision it was read at, heading path left out when unchanged", async () => {
    const [parent] = fixturePassages();
    const saved = { ...parent, content: "New text.", revision: 3 };
    const update = vi.spyOn(sdk, "updatePassage").mockResolvedValue(saved);
    const { onSaved } = renderEditor({ mode: "edit", passage: parent });

    await replaceText("New text.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(saved, "edit"));
    expect(update).toHaveBeenCalledWith(BASE_NAME, "d1", parent.id, {
      content: "New text.",
      revision: 2,
    });
    expect(await screen.findByText("已保存，当前是修订 3。")).toBeInTheDocument();
  });

  it("on a conflict shows the server's copy; overwrite resends against the server's revision", async () => {
    const [parent] = fixturePassages();
    const theirs = { ...parent, content: "Their text.", revision: 3 };
    const update = vi
      .spyOn(sdk, "updatePassage")
      .mockRejectedValueOnce(conflictError(theirs))
      .mockResolvedValueOnce({ ...parent, content: "My text.", revision: 4 });
    const { onSaved, onClose, onConflict } = renderEditor({ mode: "edit", passage: parent });

    await replaceText("My text.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    const conflict = await screen.findByTestId("kb-passage-editor-conflict");
    expect(conflict).toHaveTextContent("现在是修订 3");
    expect(conflict).toHaveTextContent("Their text.");
    expect(onSaved).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    // The list shows the server's copy meanwhile; my text stays in the editor.
    expect(onConflict).toHaveBeenCalledWith(theirs);
    expect(screen.getByTestId("kb-passage-editor-content")).toHaveValue("My text.");
    expect(screen.getByTestId("kb-passage-editor-save")).toHaveTextContent("用我的版本覆盖");

    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(update).toHaveBeenLastCalledWith(BASE_NAME, "d1", parent.id, {
      content: "My text.",
      revision: 3,
    });
  });

  it("use theirs loads the server's text into the editor and clears the conflict", async () => {
    const [parent] = fixturePassages();
    const theirs = { ...parent, content: "Their text.", heading_path: ["Theirs"], revision: 3 };
    vi.spyOn(sdk, "updatePassage").mockRejectedValueOnce(conflictError(theirs));
    renderEditor({ mode: "edit", passage: parent });

    await replaceText("My text.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));
    await userEvent.click(await screen.findByTestId("kb-passage-editor-use-theirs"));

    expect(screen.getByTestId("kb-passage-editor-content")).toHaveValue("Their text.");
    expect(screen.queryByTestId("kb-passage-editor-conflict")).toBeNull();
    // (antd spaces out a two-character label: 「保 存」.)
    expect(screen.getByTestId("kb-passage-editor-save")).not.toHaveTextContent("覆盖");
  });

  it("a busy document says so and keeps the editor open without a conflict", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "updatePassage").mockRejectedValue(
      new ApiError("busy", "HTTP_409", 409, { error: "document_busy" }),
    );
    const { onClose } = renderEditor({ mode: "edit", passage: parent });

    await replaceText("Anything.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    expect(await screen.findByText("文档正在入库或有新版待处理，完成后再改。")).toBeInTheDocument();
    expect(screen.queryByTestId("kb-passage-editor-conflict")).toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("text over the limit names the limit", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "updatePassage").mockRejectedValue(
      new ApiError("long", "HTTP_422", 422, { error: "passage_too_long", limit_tokens: 2048 }),
    );
    renderEditor({ mode: "edit", passage: parent });

    await replaceText("Long.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    expect(await screen.findByText(/超过 2048 token/)).toBeInTheDocument();
  });

  it("a passage gone meanwhile closes the editor and asks the list to reload", async () => {
    const [parent] = fixturePassages();
    vi.spyOn(sdk, "updatePassage").mockRejectedValue(new ApiError("gone", "HTTP_404", 404));
    const { onGone, onClose } = renderEditor({ mode: "edit", passage: parent });

    await replaceText("Anything.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    await waitFor(() => expect(onGone).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("nothing changed: closes without a request", async () => {
    const [parent] = fixturePassages();
    const update = vi.spyOn(sdk, "updatePassage");
    const { onClose } = renderEditor({ mode: "edit", passage: parent });
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(update).not.toHaveBeenCalled();
  });
});

describe("PassageEditModal — add", () => {
  it("adds a passage with its heading path", async () => {
    const [parent] = fixturePassages();
    const added = { ...parent, id: "new", content: "Added.", revision: 1 };
    const add = vi.spyOn(sdk, "addPassage").mockResolvedValue(added);
    const { onSaved } = renderEditor({ mode: "add" });

    await userEvent.type(screen.getByRole("combobox"), "Claims{enter}");
    await replaceText("Added.");
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(added, "add"));
    expect(add).toHaveBeenCalledWith(BASE_NAME, "d1", {
      content: "Added.",
      heading_path: ["Claims"],
    });
  });

  it("refuses empty text before any request", async () => {
    const add = vi.spyOn(sdk, "addPassage");
    renderEditor({ mode: "add" });
    await userEvent.click(screen.getByTestId("kb-passage-editor-save"));
    expect(await screen.findByText("正文不能为空。")).toBeInTheDocument();
    expect(add).not.toHaveBeenCalled();
  });
});
