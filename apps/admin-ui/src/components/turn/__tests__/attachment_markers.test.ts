import { describe, expect, it } from "vitest";

import en from "../../../i18n/locales/en";
import zhCN from "../../../i18n/locales/zh-CN";
import { splitAttachmentMarkers } from "../attachment_markers";

const doc = (value: string, name: string) => ({ kind: "document", name, value });
const img = (value: string, name: string) => ({ kind: "image", name, value });
const shape = (input: string) => {
  const out = splitAttachmentMarkers(input);
  return { text: out.text, attachments: out.attachments.map(({ kind, name, value }) => ({ kind, name, value })) };
};

describe("splitAttachmentMarkers", () => {
  it("external run, no images: lifts the file paragraph and names the chip by its last path segment", () => {
    expect(shape("帮我解读这份报告\n\n[file attached: uploads/报告-1.pdf]")).toEqual({
      text: "帮我解读这份报告",
      attachments: [doc("uploads/报告-1.pdf", "报告-1.pdf")],
    });
  });

  it("text-only model: lifts the image and file paragraphs in order and keeps the untrusted block", () => {
    expect(
      shape(
        "look\n\n[image attached: expert_work://image/u1.png]\n[image attached: expert_work://image/u2.png]" +
          "\n\n[file attached: uploads/b.docx]\n\n[untrusted content]\nbody",
      ),
    ).toEqual({
      text: "look\n\n[untrusted content]\nbody",
      attachments: [
        img("expert_work://image/u1.png", "u1.png"),
        img("expert_work://image/u2.png", "u2.png"),
        doc("uploads/b.docx", "b.docx"),
      ],
    });
  });

  it("vision model: the file lines arrive glued to the text (blocks joined without a separator)", () => {
    expect(shape("read this[file attached: uploads/a.pdf]\n[file attached: uploads/b.pdf]")).toEqual({
      text: "read this",
      attachments: [doc("uploads/a.pdf", "a.pdf"), doc("uploads/b.pdf", "b.pdf")],
    });
    expect(shape("read this[file attached: uploads/a.pdf]\n\n[untrusted content]\nbody")).toEqual({
      text: "read this\n\n[untrusted content]\nbody",
      attachments: [doc("uploads/a.pdf", "a.pdf")],
    });
  });

  it("playground: lifts the uploaded-documents note it puts in front of the text, in either language", () => {
    for (const note of [zhCN.playground.uploaded_docs_note, en.playground.uploaded_docs_note]) {
      expect(shape(`${note}: uploads/x.pdf, uploads/y.pdf\n\nq`)).toEqual({
        text: "q",
        attachments: [doc("uploads/x.pdf", "x.pdf"), doc("uploads/y.pdf", "y.pdf")],
      });
    }
  });

  it("a marker-shaped line the user typed inside their text stays text", () => {
    const typed = "Please explain:\n[file attached: config]\nthanks";
    expect(shape(typed)).toEqual({ text: typed, attachments: [] });
    const lastLine = "Please explain:\n[file attached: config]";
    expect(shape(lastLine)).toEqual({ text: lastLine, attachments: [] });
  });

  it("never lifts anything from inside an untrusted block, fenced or plain", () => {
    for (const data of [
      "q\n\n[untrusted content]\nintro\n\n[file attached: uploads/evil.pdf]",
      "q\n\n«UNTRUSTED nonce=abc»\nintro\n\n[file attached: uploads/evil.pdf]\n«/UNTRUSTED nonce=abc»",
    ]) {
      expect(shape(data)).toEqual({ text: data, attachments: [] });
    }
  });

  it("an image line that is not a platform image ref stays text", () => {
    const text = "q\n\n[image attached: https://example.com/a.png]";
    expect(shape(text)).toEqual({ text, attachments: [] });
  });

  it("an attachment-only turn has empty text", () => {
    expect(shape("[file attached: uploads/x.pdf]")).toEqual({ text: "", attachments: [doc("uploads/x.pdf", "x.pdf")] });
  });

  it("leaves the user's own paragraphs byte for byte, blank lines included", () => {
    expect(shape("a\n\n\n\nb\n\n[file attached: uploads/x.pdf]").text).toBe("a\n\n\n\nb");
    const plain = "a\n\n\n\nb [file attached: inline mention] c\n";
    expect(splitAttachmentMarkers(plain)).toEqual({ text: plain, attachments: [] });
  });

  it("gives every attachment of a turn a distinct id", () => {
    const out = splitAttachmentMarkers("q\n\n[file attached: uploads/x.pdf]\n[file attached: uploads/x.pdf]");
    expect(new Set(out.attachments.map((a) => a.id)).size).toBe(2);
  });
});
