/**
 * text_summary — the console's shared one-line summary helpers. The middle
 * column's compact rows (``CompactRow.tsx``) and the right rail's trajectory
 * rows (``TrajectoryRows.tsx``) project the same underlying row, so they must
 * cut a multi-sentence note at the same place — before this module the two
 * carried private copies with different terminator sets and disagreed on the
 * very same plan row.
 */

/** 句子终止符:全角 。！？、半角 ! ?,外加换行。
 *  半角 `.` 不在其中 —— 它会把小数切断(`置信度 0.8 不够` → `置信度 0`)。 */
const SENTENCE_END = /[。!?！？\n]/;

/** The first sentence of a (possibly multi-sentence) note — the text before
 *  the first sentence terminator, trimmed; the whole (trimmed) string when
 *  there is none. */
export function firstSentence(text: string): string {
  const idx = text.search(SENTENCE_END);
  return (idx === -1 ? text : text.slice(0, idx)).trim();
}
