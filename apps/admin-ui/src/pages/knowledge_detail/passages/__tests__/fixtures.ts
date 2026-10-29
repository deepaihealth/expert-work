/**
 * Test data for the passage console (B-175 P3-PR3): the shared passage
 * fixture the backend test also reads, plus small builders.
 */
import { readFileSync } from "node:fs";

import type { KnowledgeDocument } from "../../../../api/knowledge";
import type {
  Passage,
  PassageRevision,
  PassagesPage,
} from "../../../../api/knowledge_maintenance";

/** ``apps/admin-ui/src/api/__fixtures__/knowledge_passages.json`` — two
 *  passages: an edited parent with two chunks, and a lone chunk switched off
 *  by inheritance. */
export function fixturePage(): PassagesPage {
  return JSON.parse(
    readFileSync("src/api/__fixtures__/knowledge_passages.json", "utf8"),
  ) as PassagesPage;
}

export function fixturePassages(): [Passage, Passage] {
  const [parent, inherited] = fixturePage().passages;
  return [parent, inherited];
}

export const BASE_NAME = "support-docs";

export function readyDocument(extra: Partial<KnowledgeDocument> = {}): KnowledgeDocument {
  return {
    id: "22222222-2222-2222-2222-222222222222",
    filename: "faq.pdf",
    status: "ready",
    error: null,
    chunk_count: 3,
    attempts: 1,
    created_at: "2026-10-27T00:00:00Z",
    updated_at: "2026-10-27T00:05:00Z",
    ...extra,
  };
}

export function revision(extra: Partial<PassageRevision> = {}): PassageRevision {
  return {
    passage_id: "8f0f3c2e-5b0a-4c39-9a35-0d3c7b6f1a01",
    revision: 1,
    content: "Sample paragraph one.\n\nSample paragraph two.",
    heading_path: ["Chapter 1", "Section 1.1"],
    edited_by: null,
    created_at: "2026-10-27T01:00:00Z",
    orphaned_at: null,
    switch_off_lost: false,
    ...extra,
  };
}
