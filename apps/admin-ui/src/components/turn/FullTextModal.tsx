/**
 * 「查看全文」 modal (#9/#11) — full-length reading view for a step's
 * reasoning / output and the turn answer. The text is already client-side
 * (parsed from the turn's own SSE frames), so opening it never fetches.
 * Follows TraceView's raw-content modal conventions: ``data-testid`` on the
 * inner content wrapper (antd forwards root-level testids to the modal root —
 * see TraceView.tsx's same note), pre-wrap + capped height inside.
 *
 * Sizing (UX feedback 2026-07-30 #14): near-full-viewport reading surface —
 * the default antd modal was far too small for chapter-length LLM output —
 * plus a one-click copy button in the title row.
 */
import { useState, type CSSProperties } from "react";
import { App, Button, Modal } from "antd";
import { Check, Copy } from "lucide-react";
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
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [copied, setCopied] = useState(false);

  const copy = async (): Promise<void> => {
    if (state === null) return;
    try {
      await navigator.clipboard.writeText(state.text);
      setCopied(true);
      message.success(t("playground.copy_done"));
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      message.error(t("playground.copy_failed"));
    }
  };

  return (
    <Modal
      open={state !== null}
      onCancel={onClose}
      footer={null}
      width="min(1080px, 92vw)"
      title={
        <div style={{ display: "flex", alignItems: "center", gap: 12, paddingRight: 24 }}>
          <span style={{ flex: 1 }}>{state?.title}</span>
          <Button
            size="small"
            icon={copied ? <Check size={13} /> : <Copy size={13} />}
            onClick={copy}
            data-testid="full-text-copy"
          >
            {t("playground.copy_all")}
          </Button>
        </div>
      }
      destroyOnHidden
    >
      <div data-testid="full-text-modal">
        <pre
          style={{
            margin: 0,
            fontFamily: "var(--ew-font-mono)",
            fontSize: 13,
            lineHeight: 1.6,
            color: "var(--ew-text-secondary)",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: "calc(100vh - 220px)",
            overflow: "auto",
          }}
        >
          {state?.text}
        </pre>
      </div>
    </Modal>
  );
}
