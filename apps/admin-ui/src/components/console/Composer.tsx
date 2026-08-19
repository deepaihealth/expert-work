/**
 * Composer — the input box + run/attach/stop controls, lifted verbatim out
 * of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see ``playground-input``
 * / ``playground-run`` / ``playground-attach`` / ``playground-attach-doc`` /
 * ``playground-stop`` testids there).
 *
 * NEW (ruling R5): Enter sends, Shift+Enter inserts a newline (default
 * textarea behaviour — left alone), and Enter during IME composition
 * (``nativeEvent.isComposing``) is ignored so committing a CJK candidate
 * with Enter doesn't fire a send.
 *
 * NEW (spec §八.5, PR-A.1 Task 4): the run button is replaced in place by a
 * danger stop button while running — same toolbar slot, never both at once
 * — and the char-count + hint text moves onto that toolbar row's right end.
 *
 * ``readOnly`` is a plain prop from the parent — this component does not
 * read ``useIsTenantSwitched`` itself.
 */
import type { JSX, KeyboardEvent } from "react";
import { Button, Input, Tooltip, Typography } from "antd";
import { FileText, ImagePlus, Send, Square } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ReadonlyTooltip } from "../ReadonlyTooltip";

const { TextArea } = Input;
const { Text } = Typography;

const DEFAULT_MAX_LENGTH = 65536;

export interface ComposerProps {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  running: boolean;
  uploading: boolean;
  readOnly: boolean;
  /** 非空 → 发送禁用 + tooltip `console.vars_required_missing`。 */
  missingVariables: readonly string[];
  onAttachImage: () => void;
  onAttachDocument: () => void;
  maxLength?: number;
}

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  running,
  uploading,
  readOnly,
  missingVariables,
  onAttachImage,
  onAttachDocument,
  maxLength = DEFAULT_MAX_LENGTH,
}: ComposerProps): JSX.Element {
  const { t } = useTranslation();

  const canSend = !running && value.trim() !== "" && missingVariables.length === 0;
  const sendDisabled =
    readOnly || (!running && (value.trim() === "" || missingVariables.length > 0));

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (canSend) onSend();
    }
  };

  const sendButton = (
    <Button
      type="primary"
      icon={<Send size={14} strokeWidth={1.75} />}
      onClick={onSend}
      disabled={sendDisabled}
      data-testid="playground-run"
    >
      {t("playground.run")}
    </Button>
  );

  const sendButtonWithMissingTooltip =
    missingVariables.length > 0 ? (
      <Tooltip
        title={t("console.vars_required_missing", {
          names: missingVariables.join(", "),
        })}
      >
        <span>{sendButton}</span>
      </Tooltip>
    ) : (
      sendButton
    );

  // §八.5 — same toolbar slot renders either the run button or, while
  // running, a danger stop button; never both.
  const runSlot = running ? (
    <Button
      danger
      icon={<Square size={14} strokeWidth={1.75} />}
      onClick={onStop}
      data-testid="playground-stop"
    >
      {t("playground.stop")}
    </Button>
  ) : (
    <ReadonlyTooltip on={readOnly}>{sendButtonWithMissingTooltip}</ReadonlyTooltip>
  );

  return (
    <>
      <TextArea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={t("playground.input_placeholder")}
        autoSize={{ minRows: 2, maxRows: 8 }}
        disabled={running || readOnly}
        maxLength={maxLength}
        data-testid="playground-input"
      />
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {runSlot}
        <ReadonlyTooltip on={readOnly}>
          <Button
            icon={<ImagePlus size={14} strokeWidth={1.75} />}
            onClick={onAttachImage}
            loading={uploading}
            disabled={running || readOnly}
            data-testid="playground-attach"
          >
            {uploading ? t("playground.uploading") : t("playground.attach_image")}
          </Button>
        </ReadonlyTooltip>
        <ReadonlyTooltip on={readOnly}>
          <Button
            icon={<FileText size={14} strokeWidth={1.75} />}
            onClick={onAttachDocument}
            loading={uploading}
            disabled={running || readOnly}
            data-testid="playground-attach-doc"
          >
            {uploading ? t("playground.uploading") : t("playground.attach_document")}
          </Button>
        </ReadonlyTooltip>
        <Text
          type="secondary"
          style={{ marginLeft: "auto", fontSize: 12 }}
          data-testid="console-composer-hint"
        >
          {value.length} / {maxLength} · {t("console.composer_hint")}
        </Text>
      </div>
    </>
  );
}
