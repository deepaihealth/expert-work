import { afterAll, beforeAll, describe, expect, it } from "vitest";

import i18n from "../../../i18n";
import { documentErrorText } from "../documentError";

/** The refusal as the server stores it — ``too_large_message`` in
 *  ``control_plane/knowledge/parsed_size.py``, whose test pins this text. */
const REFUSAL =
  "text too large: 12,345,678 characters, over the per-document limit of 2,500,000; " +
  "split the document into smaller files and upload them separately";

/** The chunk-count refusal — ``too_many_chunks_message`` in the same file. */
const CHUNKS_REFUSAL =
  "too many chunks: 58,140 chunks, over the per-document limit of 40,000; " +
  "split the document into smaller files and upload them separately";

describe("documentErrorText", () => {
  let priorLang: string;
  beforeAll(() => {
    priorLang = i18n.language;
  });
  afterAll(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("says in Chinese how large the document is, the limit, and to split it", async () => {
    await i18n.changeLanguage("zh-CN");
    expect(
      documentErrorText(i18n.t, "zh-CN", { error: REFUSAL, error_code: "text_too_large" }),
    ).toBe(
      "文档文字过多：共 12,345,678 字，超过单个文档 2,500,000 字的上限。请拆成几个较小的文件后分别上传。",
    );
  });

  it("says it in English", async () => {
    await i18n.changeLanguage("en");
    expect(documentErrorText(i18n.t, "en", { error: REFUSAL, error_code: "text_too_large" })).toBe(
      "Too much text: 12,345,678 characters, over the per-document limit of 2,500,000. " +
        "Split it into smaller files and upload them separately.",
    );
  });

  it("still asks to split when the numbers cannot be read", async () => {
    await i18n.changeLanguage("en");
    expect(
      documentErrorText(i18n.t, "en", { error: "text too large", error_code: "text_too_large" }),
    ).toBe(
      "Too much text: over the per-document limit. Split it into smaller files and upload them separately.",
    );
  });

  it("says in Chinese how many segments the document split into, the limit, and to split it", async () => {
    await i18n.changeLanguage("zh-CN");
    expect(
      documentErrorText(i18n.t, "zh-CN", { error: CHUNKS_REFUSAL, error_code: "too_many_chunks" }),
    ).toBe(
      "文档切出的段落过多：共 58,140 段，超过单个文档 40,000 段的上限。请拆成几个较小的文件后分别上传。",
    );
  });

  it("says the segment count in English, and still asks to split without numbers", async () => {
    await i18n.changeLanguage("en");
    expect(
      documentErrorText(i18n.t, "en", { error: CHUNKS_REFUSAL, error_code: "too_many_chunks" }),
    ).toBe(
      "Too many segments: 58,140, over the per-document limit of 40,000. " +
        "Split it into smaller files and upload them separately.",
    );
    expect(
      documentErrorText(i18n.t, "en", { error: "too many chunks", error_code: "too_many_chunks" }),
    ).toBe(
      "Too many segments: over the per-document limit. Split it into smaller files and upload them separately.",
    );
  });

  it("shows any other failure as recorded", () => {
    expect(
      documentErrorText(i18n.t, "en", { error: "MarkItDown parse failed", error_code: "parse_failed" }),
    ).toBe("MarkItDown parse failed");
    expect(documentErrorText(i18n.t, "en", { error: null, error_code: null })).toBeNull();
  });
});
