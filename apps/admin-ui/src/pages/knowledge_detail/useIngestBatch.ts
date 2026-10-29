/**
 * B-222 PR-2 — follows the documents an upload batch created on the server:
 * every upload is estimated first and then waits for a confirmation, so the
 * queue row needs the document's server status and estimate, not just "sent".
 *
 * The ids come from the queue's finished rows (``unchanged`` ones started
 * nothing). They are read with the list's ``document_id`` filter (at most 200
 * per request) every 5 s while any of them is still being estimated or
 * ingested, and on demand (``refresh``) after a confirm / withdraw.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { listDocuments, type KnowledgeDocument, type UploadedDocument } from "../../api/knowledge";
import type { UploadQueueItem } from "../../hooks/useUploadQueue";

const POLL_INTERVAL_MS = 5_000;
const IDS_PER_REQUEST = 200;
const MOVING = new Set(["estimating", "pending", "processing"]);

export function batchDocumentIds(items: UploadQueueItem<UploadedDocument>[]): string[] {
  const ids = items
    .filter((item) => item.status === "done" && item.result && !item.result.unchanged)
    .map((item) => item.result!.id);
  return Array.from(new Set(ids));
}

export function useIngestBatch(
  baseName: string,
  readScope: string | undefined,
  items: UploadQueueItem<UploadedDocument>[],
): { documents: Record<string, KnowledgeDocument>; refresh: () => Promise<void> } {
  const ids = useMemo(() => batchDocumentIds(items), [items]);
  const idsKey = ids.join(",");
  const [documents, setDocuments] = useState<Record<string, KnowledgeDocument>>({});
  /** The ids ``documents`` was last read for (absent from it: deleted). */
  const [readFor, setReadFor] = useState("");
  const seqRef = useRef(0);

  const refresh = useCallback(async () => {
    const wanted = idsKey ? idsKey.split(",") : [];
    const seq = ++seqRef.current;
    if (wanted.length === 0) {
      setDocuments({});
      setReadFor("");
      return;
    }
    const found: Record<string, KnowledgeDocument> = {};
    for (let start = 0; start < wanted.length; start += IDS_PER_REQUEST) {
      const page = await listDocuments(
        baseName,
        { limit: IDS_PER_REQUEST, offset: 0 },
        readScope,
        { documentIds: wanted.slice(start, start + IDS_PER_REQUEST) },
      );
      for (const document of page.documents) found[document.id] = document;
    }
    if (seq === seqRef.current) {
      setDocuments(found);
      setReadFor(idsKey);
    }
  }, [baseName, idsKey, readScope]);

  useEffect(() => {
    void refresh().catch(() => undefined);
  }, [refresh]);

  const moving = useMemo(
    () =>
      readFor !== idsKey ||
      ids.some((id) => documents[id] !== undefined && MOVING.has(documents[id].status)),
    [documents, ids, idsKey, readFor],
  );
  useEffect(() => {
    if (!moving) return undefined;
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      void refresh().catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [moving, refresh]);

  return { documents, refresh };
}
