/**
 * A document's passages (「整块」), 20 per page — B-175 P3-PR3.
 *
 * Read with the read scope; only the latest read lands. A switch updates its
 * card in place; edits, additions and restores happen in the drawer's editor
 * and history, which bump ``version`` so the page is read again.
 */
import { useCallback, useEffect, useRef, useState, type ReactElement } from "react";
import { App, Button, Empty, Pagination, Skeleton } from "antd";
import { Plus } from "lucide-react";
import { useTranslation } from "react-i18next";

import { errMessage } from "../../../api/client";
import { listPassages, type Passage } from "../../../api/knowledge_maintenance";
import { PassageCard, WriteGate } from "./PassageCard";
import { usePassageSwitch } from "./usePassageSwitch";

const PAGE_SIZE = 20;

export interface PassageListProps {
  baseName: string;
  documentId: string;
  readScope: string | undefined;
  readonly: boolean;
  busy: boolean;
  /** Bumped by the drawer when passages changed elsewhere: read the page again. */
  version: number;
  onEdit: (passage: Passage) => void;
  onHistory: (passage: Passage) => void;
  onAdd: () => void;
  /** A passage was switched (the review count may have changed). */
  onSwitched: () => void;
}

export function PassageList({
  baseName,
  documentId,
  readScope,
  readonly,
  busy,
  version,
  onEdit,
  onHistory,
  onAdd,
  onSwitched,
}: PassageListProps): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [passages, setPassages] = useState<Passage[]>([]);
  const [figureIds, setFigureIds] = useState<ReadonlySet<string>>(new Set());
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const latestRead = useRef(0);

  const load = useCallback(async () => {
    const read = ++latestRead.current;
    setLoading(true);
    try {
      const result = await listPassages(
        baseName,
        documentId,
        { offset: (page - 1) * PAGE_SIZE, limit: PAGE_SIZE },
        readScope,
      );
      if (read !== latestRead.current) return;
      setPassages(result.passages);
      setFigureIds(new Set(result.figure_ids ?? []));
      setTotal(result.total);
    } catch (err) {
      if (read === latestRead.current) message.error(errMessage(err));
    } finally {
      if (read === latestRead.current) setLoading(false);
    }
  }, [baseName, documentId, page, readScope, message]);

  useEffect(() => {
    void load();
  }, [load, version]);

  const { switching, switchPassage } = usePassageSwitch(baseName, documentId, () => void load());

  const handleSwitch = async (passage: Passage, enabled: boolean) => {
    const switched = await switchPassage(passage, enabled);
    if (switched === null) return;
    setPassages((current) => current.map((p) => (p.id === switched.id ? switched : p)));
    onSwitched();
  };

  return (
    <div data-testid="kb-passage-list">
      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
        <WriteGate readonly={readonly} busy={busy}>
          <Button
            size="small"
            icon={<Plus size={14} strokeWidth={1.5} />}
            disabled={readonly || busy}
            onClick={onAdd}
            data-testid="kb-passage-add"
          >
            {t("kb_maintenance.add_passage")}
          </Button>
        </WriteGate>
      </div>
      {loading && passages.length === 0 ? (
        <Skeleton active paragraph={{ rows: 8 }} />
      ) : passages.length === 0 ? (
        <Empty description={t("kb_maintenance.passages_empty")} />
      ) : (
        <>
          {passages.map((passage) => (
            <PassageCard
              key={passage.id}
              baseName={baseName}
              documentId={documentId}
              readScope={readScope}
              passage={passage}
              figureIds={figureIds}
              readonly={readonly}
              busy={busy}
              switching={switching === passage.id}
              onSwitch={(p, enabled) => void handleSwitch(p, enabled)}
              onEdit={onEdit}
              onHistory={onHistory}
            />
          ))}
          {total > PAGE_SIZE && (
            <Pagination
              current={page}
              total={total}
              pageSize={PAGE_SIZE}
              showSizeChanger={false}
              onChange={setPage}
              style={{ marginTop: 8, textAlign: "right" }}
            />
          )}
        </>
      )}
    </div>
  );
}
