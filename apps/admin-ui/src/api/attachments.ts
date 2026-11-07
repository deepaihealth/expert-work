/**
 * B-295 —— the bytes behind one attachment chip in a conversation's user message
 * (``GET /v1/sessions/{thread_id}/attachment?ref=``). ``ref`` is the chip's ``value``
 * as the control plane wrote it: an ``expert_work://image/…`` ref or a document's
 * ``uploads/<leaf>``; the server resolves it inside that conversation.
 *
 * What the page may render is decided from the server's ``Content-Type`` (derived
 * from the stored row / stored name + sniffed bytes), never from the chip's name:
 * only the types in :data:`PREVIEW_TYPES` are previewed, everything else is a
 * download.
 */
import { ApiError, apiClient, withTenantScope, type TenantScope } from "./client";

export interface AttachmentBytes {
  blob: Blob;
  /** The response ``Content-Type`` (lower-cased, parameters dropped). */
  contentType: string;
}

/** 410 ``ATTACHMENT_GONE`` — retention deleted it, or it was deleted. */
export function isAttachmentGone(err: unknown): boolean {
  return err instanceof ApiError && err.status === 410;
}

export type PreviewKind = "image" | "pdf" | "text";

/** Server content types the page renders in place, and how. A blob is only ever
 *  handed to ``<img>`` / ``<iframe>`` re-typed as one of these keys. */
export const PREVIEW_TYPES: Readonly<Record<string, PreviewKind>> = {
  "image/png": "image",
  "image/jpeg": "image",
  "image/gif": "image",
  "image/webp": "image",
  "application/pdf": "pdf",
  "text/plain": "text",
  "text/csv": "text",
  "text/markdown": "text",
};

export function previewKindOf(contentType: string): PreviewKind | null {
  return Object.hasOwn(PREVIEW_TYPES, contentType) ? PREVIEW_TYPES[contentType] : null;
}

export async function fetchSessionAttachment(
  threadId: string,
  ref: string,
  tenantScope?: TenantScope,
): Promise<AttachmentBytes> {
  const response = await apiClient.get<Blob>(
    `/v1/sessions/${encodeURIComponent(threadId)}/attachment`,
    { params: withTenantScope({ ref }, tenantScope), responseType: "blob" },
  );
  const header = (response.headers as Record<string, string | undefined>)["content-type"];
  const contentType = (header ?? "").split(";")[0].trim().toLowerCase();
  return { blob: response.data, contentType };
}

/** Save ``blob`` under ``filename`` (an anchor can't carry the Bearer token, so the
 *  bytes were fetched first); the object URL is revoked right after the click. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename || "download";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}
