/**
 * B-209 PR-2 §5.9 — a chunk body in the segment preview, with the original
 * picture's thumbnail next to each picture description.
 *
 * A described picture sits in the text as a quote block whose first line is
 * ``> 〔图 <16 hex id>〕<description>``; the id names the original the platform
 * stored when it was described. The model never sees the picture — only this
 * preview fetches it (same permissions as the chunk list).
 */
import { useEffect, useState, type ReactElement } from "react";
import { Image, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { fetchFigureUrl } from "../../api/knowledge";

const { Paragraph, Text } = Typography;

/** The picture id on a description block's first line. */
const FIGURE_ID = /^> 〔图 ([0-9a-f]{16})〕/;

const THUMB_WIDTH = 96;

/** The body split into paragraphs (blank-line separated); a quote block is
 *  one paragraph (its inner blank lines are ``>``). */
export function chunkSegments(content: string): { text: string; figureId: string | null }[] {
  return content
    .split(/\n{2,}/)
    .filter((segment) => segment.trim() !== "")
    .map((segment) => ({ text: segment, figureId: FIGURE_ID.exec(segment)?.[1] ?? null }));
}

interface FigureThumbnailProps {
  baseName: string;
  documentId: string;
  figureId: string;
  readScope: string | undefined;
}

function FigureThumbnail({
  baseName,
  documentId,
  figureId,
  readScope,
}: FigureThumbnailProps): ReactElement {
  const { t } = useTranslation();
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    let created: string | null = null;
    fetchFigureUrl(baseName, documentId, figureId, readScope)
      .then((objectUrl) => {
        created = objectUrl;
        if (live) setUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
      if (created) URL.revokeObjectURL(created);
    };
  }, [baseName, documentId, figureId, readScope]);

  if (failed) {
    return (
      <Text type="secondary" style={{ fontSize: 12 }} data-testid={`kb-figure-missing-${figureId}`}>
        {t("knowledge_page.figure_unavailable")}
      </Text>
    );
  }
  return url ? (
    <span data-testid={`kb-figure-thumb-${figureId}`}>
      <Image src={url} width={THUMB_WIDTH} alt={t("knowledge_page.figure_thumb_alt")} />
    </span>
  ) : (
    <div style={{ width: THUMB_WIDTH }} />
  );
}

export interface ChunkContentProps {
  baseName: string;
  documentId: string;
  readScope: string | undefined;
  content: string;
  /** Picture ids the server confirmed for this document (security review
   *  S-L1): text that merely looks like a picture block gets no thumbnail. */
  figureIds: ReadonlySet<string>;
}

export function ChunkContent({
  baseName,
  documentId,
  readScope,
  content,
  figureIds,
}: ChunkContentProps): ReactElement {
  const segments = chunkSegments(content).map((segment) =>
    segment.figureId && figureIds.has(segment.figureId) ? segment : { ...segment, figureId: null },
  );
  if (!segments.some((segment) => segment.figureId)) {
    return (
      <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13 }}>{content}</Paragraph>
    );
  }
  return (
    <div>
      {segments.map((segment, index) =>
        segment.figureId ? (
          <div
            key={index}
            style={{ display: "flex", gap: 12, alignItems: "flex-start", marginBottom: 8 }}
            data-testid={`kb-figure-block-${segment.figureId}`}
          >
            <Paragraph style={{ whiteSpace: "pre-wrap", marginBottom: 0, fontSize: 13, flex: 1 }}>
              {segment.text}
            </Paragraph>
            <FigureThumbnail
              baseName={baseName}
              documentId={documentId}
              figureId={segment.figureId}
              readScope={readScope}
            />
          </div>
        ) : (
          <Paragraph key={index} style={{ whiteSpace: "pre-wrap", marginBottom: 8, fontSize: 13 }}>
            {segment.text}
          </Paragraph>
        ),
      )}
    </div>
  );
}
