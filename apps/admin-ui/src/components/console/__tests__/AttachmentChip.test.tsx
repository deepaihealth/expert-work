/** B-295 —— 对话里的附件标签:点开预览 / 下载;过期或已删除显示「文件已过期或已删除」。 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../../../i18n";

import * as attachmentsSdk from "../../../api/attachments";
import { ApiError } from "../../../api/client";
import type { Attachment } from "../../turn/types";
import { AttachmentChip } from "../AttachmentChip";
import { UserBubble } from "../UserBubble";

const fetchAttachment = vi.spyOn(attachmentsSdk, "fetchSessionAttachment");
const saveBlob = vi.spyOn(attachmentsSdk, "saveBlob");
const createObjectURL = vi.fn((_blob: Blob) => "blob:preview-1");
const revokeObjectURL = vi.fn();

const TENANT = "22222222-2222-2222-2222-222222222222";
const IMAGE: Attachment = {
  id: "a1",
  name: "微信图片_20261107135228_3352_6.jpg",
  kind: "image",
  value: "expert_work://image/t/th/11111111-1111-1111-1111-111111111111.jpg",
};
const PDF: Attachment = { id: "a2", name: "报告.pdf", kind: "document", value: "uploads/报告.pdf" };
const TXT: Attachment = { id: "a3", name: "notes.txt", kind: "document", value: "uploads/notes.txt" };
const DOCX: Attachment = { id: "a4", name: "合同.docx", kind: "document", value: "uploads/合同.docx" };

function bytes(body: string, contentType: string): attachmentsSdk.AttachmentBytes {
  return { blob: new Blob([body], { type: contentType }), contentType };
}

beforeEach(() => {
  vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });
  saveBlob.mockImplementation(() => undefined);
});

afterEach(() => {
  fetchAttachment.mockReset();
  saveBlob.mockReset();
  createObjectURL.mockClear();
  revokeObjectURL.mockClear();
  vi.unstubAllGlobals();
});

function renderChip(attachment: Attachment, threadId: string | null = "th-1") {
  return render(<AttachmentChip attachment={attachment} threadId={threadId} tenantScope={TENANT} />);
}

async function openChip(name: string) {
  await userEvent.click(screen.getByRole("button", { name: new RegExp(name) }));
  return screen.findByTestId("console-attachment-preview");
}

describe("AttachmentChip", () => {
  it("previews an image in a modal and downloads the fetched bytes", async () => {
    const payload = bytes("png-bytes", "image/png");
    fetchAttachment.mockResolvedValue(payload);
    renderChip(IMAGE);

    const modal = await openChip("微信图片");

    expect(fetchAttachment).toHaveBeenCalledWith("th-1", IMAGE.value, TENANT);
    const img = await within(modal).findByTestId("console-attachment-image");
    expect(img).toHaveAttribute("src", "blob:preview-1");
    expect(createObjectURL.mock.calls[0][0].type).toBe("image/png");

    await userEvent.click(screen.getByTestId("console-attachment-download"));
    expect(saveBlob).toHaveBeenCalledWith(payload.blob, IMAGE.name);
  });

  it("previews a PDF inline as an application/pdf blob", async () => {
    // The object URL's blob is re-typed to the whitelisted server type — whatever
    // type the fetched blob itself carried (here: an untyped one).
    fetchAttachment.mockResolvedValue({ blob: new Blob(["%PDF-1.7"]), contentType: "application/pdf" });
    renderChip(PDF);

    const modal = await openChip("报告");

    const frame = await within(modal).findByTestId("console-attachment-pdf");
    expect(frame).toHaveAttribute("src", "blob:preview-1");
    expect(createObjectURL.mock.calls[0][0].type).toBe("application/pdf");
  });

  it("previews a text file as text, never as a blob URL", async () => {
    fetchAttachment.mockResolvedValue(bytes("hello 世界", "text/plain"));
    renderChip(TXT);

    const modal = await openChip("notes");

    expect(await within(modal).findByTestId("console-attachment-text")).toHaveTextContent(
      "hello 世界",
    );
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(within(modal).queryByText(/仅显示前|first \d+ KB/)).toBeNull();
  });

  it("caps a long text preview and says so", async () => {
    fetchAttachment.mockResolvedValue(bytes("x".repeat(300 * 1024), "text/plain"));
    renderChip(TXT);

    const modal = await openChip("notes");

    const pre = await within(modal).findByTestId("console-attachment-text");
    expect(pre.textContent?.length).toBe(200 * 1024);
    expect(within(modal).getByText(/仅显示前 200 KB|first 200 KB/)).toBeInTheDocument();
  });

  it("renders by the server's content type, not the file name", async () => {
    // A ``.pdf`` the server would not vouch for comes back as opaque bytes.
    fetchAttachment.mockResolvedValue(bytes("<html>", "application/octet-stream"));
    renderChip(PDF);

    const modal = await openChip("报告");

    expect(await within(modal).findByText(/不支持预览|can't be previewed/)).toBeInTheDocument();
    expect(within(modal).queryByTestId("console-attachment-pdf")).toBeNull();
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(screen.getByTestId("console-attachment-download")).toBeEnabled();
  });

  it("downloads an Office document directly, labelled as a download", async () => {
    const payload = bytes("PK", "application/octet-stream");
    fetchAttachment.mockResolvedValue(payload);
    renderChip(DOCX);

    const chip = screen.getByRole("button", { name: /合同/ });
    expect(chip).toHaveAccessibleName(/点击下载|click to download/);
    await userEvent.click(chip);

    await waitFor(() => expect(saveBlob).toHaveBeenCalledWith(payload.blob, DOCX.name));
    expect(screen.queryByTestId("console-attachment-preview")).toBeNull();
  });

  it("shows 「文件已过期或已删除」 when the file is gone", async () => {
    fetchAttachment.mockRejectedValue(new ApiError("gone", "ATTACHMENT_GONE", 410));
    renderChip(IMAGE);

    const modal = await openChip("微信图片");

    expect(await within(modal).findByTestId("console-attachment-expired")).toHaveTextContent(
      /文件已过期或已删除|expired or was deleted/,
    );
    expect(within(modal).queryByTestId("console-attachment-image")).toBeNull();
    expect(screen.getByTestId("console-attachment-download")).toBeDisabled();
  });

  it("an expired Office document opens the expired state instead of failing silently", async () => {
    fetchAttachment.mockRejectedValue(new ApiError("gone", "ATTACHMENT_GONE", 410));
    renderChip(DOCX);

    await userEvent.click(screen.getByRole("button", { name: /合同/ }));

    const modal = await screen.findByTestId("console-attachment-preview");
    expect(within(modal).getByTestId("console-attachment-expired")).toBeInTheDocument();
    expect(saveBlob).not.toHaveBeenCalled();
  });

  it("any other failure is an error with the reason, not the expired state", async () => {
    fetchAttachment.mockRejectedValue(new ApiError("boom", "HTTP_500", 500));
    renderChip(IMAGE);

    const modal = await openChip("微信图片");

    expect(await within(modal).findByText(/附件加载失败.*boom|Could not load.*boom/)).toBeInTheDocument();
    expect(within(modal).queryByTestId("console-attachment-expired")).toBeNull();
  });

  it("revokes the object URL when the preview closes", async () => {
    fetchAttachment.mockResolvedValue(bytes("png", "image/png"));
    renderChip(IMAGE);

    const modal = await openChip("微信图片");
    await within(modal).findByTestId("console-attachment-image");
    expect(revokeObjectURL).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /close|关闭/i }));

    await waitFor(() => expect(revokeObjectURL).toHaveBeenCalledWith("blob:preview-1"));
  });

  it("without a thread the chip is a plain label", async () => {
    renderChip(IMAGE, null);

    expect(screen.getByText(IMAGE.name)).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("UserBubble attachments", () => {
  it("makes each chip clickable when the bubble knows its thread", async () => {
    fetchAttachment.mockResolvedValue(bytes("hello", "text/plain"));
    render(
      <UserBubble input="看看这个" attachments={[TXT]} threadId="th-9" tenantScope={TENANT} />,
    );

    await userEvent.click(screen.getByRole("button", { name: /notes/ }));

    await screen.findByTestId("console-attachment-text");
    expect(fetchAttachment).toHaveBeenCalledWith("th-9", TXT.value, TENANT);
  });
});
