/**
 * 「查看全文」 modal (#9/#11) — full-length reading view for a step's
 * reasoning / output and the turn answer. The text is already client-side
 * (parsed from the turn's own SSE frames), so opening it never fetches.
 * Follows TraceView's raw-content modal conventions: ``data-testid`` on the
 * inner content wrapper (antd forwards root-level testids to the modal root —
 * see TraceView.tsx's same note), pre-wrap + capped height inside.
 */
import type { CSSProperties } from "react";
import { Modal } from "antd";
import { useTranslation } from "react-i18next";

/** ``null`` while closed — one modal instance serves several trigger sites. */
export interface FullTextState {
  title: string;
  text: string;
}

// Link-style trigger — mirrors TraceView's ACTION_LINK_STYLE.
const TRIGGER_STYLE: CSSProperties = {
  border: 0,
  background: "transparent",
  color: "var(--ew-text-info, #4c8dff)",
  cursor: "pointer",
  padding: 0,
  font: "inherit",
  fontSize: 11,
};

export function FullTextTrigger({
  onClick,
  style,
}: {
  onClick: () => void;
  style?: CSSProperties;
}) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      onClick={onClick}
      style={{ ...TRIGGER_STYLE, ...style }}
      data-testid="full-text-trigger"
    >
      {t("playground.view_full_text")}
    </button>
  );
}

export function FullTextModal({
  state,
  onClose,
}: {
  state: FullTextState | null;
  onClose: () => void;
}) {
  return (
    <Modal
      open={state !== null}
      onCancel={onClose}
      footer={null}
      title={state?.title}
      destroyOnHidden
    >
      <div data-testid="full-text-modal">
        <pre
          style={{
            margin: 0,
            fontFamily: "var(--ew-font-mono)",
            fontSize: 12,
            lineHeight: 1.55,
            color: "var(--ew-text-secondary)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: 420,
            overflow: "auto",
          }}
        >
          {state?.text}
        </pre>
      </div>
    </Modal>
  );
}
