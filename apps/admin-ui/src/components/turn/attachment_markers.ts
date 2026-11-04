/**
 * B-274 — the platform tells the model about a turn's uploads in the user
 * message itself, and the checkpoint keeps that message, so a history turn's
 * input carries the scaffolding; shown verbatim it reads as if the user had
 * typed it. This lifts it back out into attachments, the tags a live turn
 * shows. The shapes are exactly what the producers write:
 *
 * - control-plane ``_build_human_message`` joins ``(text, image mentions,
 *   file mentions, untrusted)`` with a blank line, each mention block one
 *   ``[image attached: expert_work://image/…]`` / ``[file attached: …]`` per
 *   line. On a vision model the text and the file block are separate content
 *   blocks that ``message_text`` joins with no separator, so the first file
 *   line arrives glued to the end of the text.
 * - the playground (``useRunEngine``) puts ``<uploaded_docs_note>: a, b`` as
 *   the first paragraph in front of the text.
 *
 * Like the backend's ``_strip_image_mentions`` it works paragraph by
 * paragraph and leaves every other paragraph byte for byte alone. Only the
 * mention paragraphs right before the untrusted block (or the end) are
 * lifted — never anything inside or after an untrusted block, whose content
 * a third party controls.
 */
import en from "../../i18n/locales/en";
import zhCN from "../../i18n/locales/zh-CN";
import type { Attachment } from "./types";

const FILE_LINE = /^\[file attached: (.+)\]$/;
const IMAGE_LINE = /^\[image attached: (expert_work:\/\/image\/[^\]\n]*)\]$/;
const FILE_GLUED = "[file attached: ";
/** Plain marker (spotlighting off) and spotlight fence that open an untrusted block. */
const UNTRUSTED_OPENERS = ["[untrusted content]", "«UNTRUSTED nonce="];
const DOCS_NOTES = [zhCN.playground.uploaded_docs_note, en.playground.uploaded_docs_note];

export interface SplitInput {
  text: string;
  attachments: Attachment[];
}

type Found = Pick<Attachment, "kind" | "value">;

function markerOf(line: string): Found | null {
  const image = IMAGE_LINE.exec(line);
  if (image) return { kind: "image", value: image[1] };
  const file = FILE_LINE.exec(line);
  return file ? { kind: "document", value: file[1] } : null;
}

/** A paragraph made only of mention lines → its mentions; otherwise null. */
function mentionParagraph(paragraph: string): Found[] | null {
  const found = paragraph.split("\n").map(markerOf);
  return found.every((f) => f !== null) ? (found as Found[]) : null;
}

/** Vision layout: ``text[file attached: a]\n[file attached: b]``. Returns the
 *  text before the glued mention and the mentions, or null if not that shape. */
function gluedTail(paragraph: string): { text: string; found: Found[] } | null {
  const lines = paragraph.split("\n");
  let first = lines.length - 1;
  while (first > 0 && FILE_LINE.test(lines[first])) first -= 1;
  const line = lines[first];
  const at = line.lastIndexOf(FILE_GLUED);
  if (at <= 0 || !line.endsWith("]")) return null;
  const head = line.slice(0, at);
  const glued = markerOf(line.slice(at));
  if (glued === null) return null;
  const rest = lines.slice(first + 1).map(markerOf) as Found[];
  return { text: [...lines.slice(0, first), head].join("\n"), found: [glued, ...rest] };
}

function docsNote(paragraph: string): Found[] | null {
  for (const note of DOCS_NOTES) {
    const prefix = `${note}: `;
    if (paragraph.startsWith(prefix) && !paragraph.includes("\n")) {
      return paragraph
        .slice(prefix.length)
        .split(", ")
        .map((value) => ({ kind: "document", value }));
    }
  }
  return null;
}

export function splitAttachmentMarkers(input: string): SplitInput {
  const paragraphs = input.split("\n\n");
  const leading = paragraphs.length > 1 ? docsNote(paragraphs[0]) : null;
  const start = leading ? 1 : 0;
  let end = paragraphs.findIndex(
    (p, i) => i >= start && UNTRUSTED_OPENERS.some((o) => p.startsWith(o)),
  );
  if (end === -1) end = paragraphs.length;
  const untrusted = paragraphs.slice(end);

  const trailing: Found[][] = [];
  let cut = end;
  while (cut > start) {
    const found = mentionParagraph(paragraphs[cut - 1]);
    if (found === null) break;
    trailing.unshift(found);
    cut -= 1;
  }
  const body = paragraphs.slice(start, cut);
  if (body.length > 0) {
    const glued = gluedTail(body[body.length - 1]);
    if (glued) {
      body[body.length - 1] = glued.text;
      trailing.unshift(glued.found);
    }
  }

  const found = [...(leading ?? []), ...trailing.flat()];
  if (found.length === 0) return { text: input, attachments: [] };
  const attachments = found.map(({ kind, value }, i) => ({
    id: `history-attachment-${i}`,
    name: value.split("/").pop() || value,
    kind,
    value,
  }));
  return { text: [...body, ...untrusted].join("\n\n"), attachments };
}
