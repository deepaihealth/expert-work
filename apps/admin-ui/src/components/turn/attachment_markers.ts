/**
 * B-274 — the platform tells the model about a turn's uploads in the user
 * message itself, and the checkpoint keeps that message, so a history turn's
 * input carries the scaffolding; shown verbatim it reads as if the user had
 * typed it. This lifts it back out into attachments, the tags a live turn
 * shows. The shapes are exactly what the producers write:
 *
 * - control-plane ``_build_human_message``: ``text``, then a blank line,
 *   ``[image attached: expert_work://image/…]`` lines, a blank line,
 *   ``[file attached: uploads/<leaf>]`` lines, then the untrusted block. A
 *   document stored under a new name (B-262: same name, different content)
 *   also carries its original name, ``[file attached: uploads/<leaf>
 *   (original name: <leaf>)]`` (B-275); the chip shows that name. On a
 *   vision model the text and the trailer are separate content blocks that
 *   ``message_text`` joins with no separator, so the first file line (or the
 *   untrusted opener) arrives glued to the end of the text.
 * - the playground (``useRunEngine``) puts ``<uploaded_docs_note>: a, b`` as
 *   the first paragraph in front of the text.
 *
 * Values are anchored to what the producers can emit — a document is always
 * ``uploads/<leaf>`` (control-plane ``is_safe_document_upload_id``), an image
 * always an ``expert_work://image/`` ref — so a mention the user typed stays
 * text. Only mention lines at the end of the text, before the untrusted
 * block, are lifted; nothing at or after the untrusted opener is touched,
 * because a third party controls that content.
 */
import en from "../../i18n/locales/en";
import zhCN from "../../i18n/locales/zh-CN";
import type { Attachment } from "./types";

/** A leaf from ``_SAFE_STEM_RE``'s set (Python ``\w`` is Unicode): no space,
 *  slash, bracket or parenthesis, so the file line parses one way only. */
const LEAF = String.raw`[\p{L}\p{M}\p{N}_.\-]+`;
/** ``uploads/<leaf>``. */
const DOC_VALUE = `uploads/${LEAF}`;
const DOC_VALUE_RE = new RegExp(`^${DOC_VALUE}$`, "u");
/** control-plane ``file_attached_line``: the stored path, then (B-275) the
 *  original name when the upload was stored under a new one. */
const FILE_MARKER = String.raw`\[file attached: (${DOC_VALUE})(?: \(original name: (${LEAF})\))?\]`;
const FILE_LINE = new RegExp(`^${FILE_MARKER}$`, "u");
const FILE_GLUED = new RegExp(`${FILE_MARKER}$`, "u");
const IMAGE_LINE = /^\[image attached: (expert_work:\/\/image\/[^\]\s]+)\]$/;
/** Plain marker (spotlighting off) and spotlight fence that open an untrusted block. */
const UNTRUSTED_OPENERS = ["[untrusted content]\n", "«UNTRUSTED nonce="];
const DOCS_NOTES = [zhCN.playground.uploaded_docs_note, en.playground.uploaded_docs_note];

export interface SplitInput {
  text: string;
  attachments: Attachment[];
}

/** ``name`` = the original name a renamed upload's line carries (B-275). */
type Found = Pick<Attachment, "kind" | "value"> & { name?: string };

function fileFound(match: RegExpExecArray): Found {
  return { kind: "document", value: match[1], name: match[2] };
}

function markerOf(line: string): Found | null {
  const image = IMAGE_LINE.exec(line);
  if (image) return { kind: "image", value: image[1] };
  const file = FILE_LINE.exec(line);
  return file ? fileFound(file) : null;
}

/** The playground note paragraph at the very start → its documents. */
function leadingNote(region: string): { found: Found[]; rest: string } | null {
  for (const note of DOCS_NOTES) {
    const prefix = `${note}: `;
    const end = region.indexOf("\n\n");
    if (!region.startsWith(prefix) || end === -1) continue;
    const values = region.slice(prefix.length, end).split(", ");
    if (!values.every((v) => DOC_VALUE_RE.test(v))) return null;
    return { found: values.map((value) => ({ kind: "document", value })), rest: region.slice(end + 2) };
  }
  return null;
}

/** Mention lines (blank lines allowed between) at the end of ``region``, plus
 *  a file mention glued to the end of the last text line (vision layout). */
function trailingMentions(region: string): { text: string; found: Found[] } {
  const lines = region.split("\n");
  const found: Found[] = [];
  let last = lines.length - 1;
  for (; last >= 0; last -= 1) {
    if (lines[last] === "") continue;
    const marker = markerOf(lines[last]);
    if (marker === null) break;
    found.unshift(marker);
  }
  const kept = lines.slice(0, last + 1);
  if (last >= 0) {
    const glued = FILE_GLUED.exec(lines[last]);
    if (glued && glued.index > 0) {
      found.unshift(fileFound(glued));
      kept[last] = lines[last].slice(0, glued.index);
    }
  }
  if (found.length === 0) return { text: region, found };
  while (kept.length > 0 && kept[kept.length - 1] === "") kept.pop();
  return { text: kept.join("\n"), found };
}

export function splitAttachmentMarkers(input: string): SplitInput {
  const openers = UNTRUSTED_OPENERS.map((o) => input.indexOf(o)).filter((i) => i !== -1);
  const cut = openers.length > 0 ? Math.min(...openers) : input.length;
  const untrusted = input.slice(cut);

  const note = leadingNote(input.slice(0, cut));
  const body = note ? note.rest : input.slice(0, cut);
  const tail = trailingMentions(body);
  const found = [...(note?.found ?? []), ...tail.found];
  if (found.length === 0) return { text: input, attachments: [] };

  const attachments = found.map(({ kind, value, name }, i) => ({
    id: `history-attachment-${i}`,
    name: name ?? (value.split("/").pop() || value),
    kind,
    value,
  }));
  // Producers put a blank line before the untrusted block only after mention
  // lines; with none lifted the body is the user's text as stored, so re-join
  // it exactly (a cut inside the user's own text must not add a blank line).
  const sep = tail.found.length > 0 ? "\n\n" : "";
  const text = [tail.text, untrusted].filter((part) => part !== "").join(sep);
  return { text, attachments };
}
