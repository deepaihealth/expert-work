/**
 * Composer — the input box + send/attach/stop controls, lifted verbatim out
 * of ``PlaygroundTab.tsx`` (调试台重设计 PR-A Task 7; see ``playground-input``
 * / ``playground-run`` / ``playground-attach`` / ``playground-attach-doc`` /
 * ``playground-stop`` testids there).
 *
 * NEW behaviour (ruling R5): Enter sends, Shift+Enter inserts a newline
 * (default textarea behaviour — left alone), and Enter during IME
 * composition (``nativeEvent.isComposing``) is ignored so committing a CJK
 * candidate with Enter doesn't fire a send.
 *
 * ``readOnly`` is a plain prop from the parent — this component does not
 * read ``useIsTenantSwitched`` itself.
 */
import type { JSX, KeyboardEvent } from "react";
import { Button, Input, Space, Tooltip, Typography } from "antd";
import { FileText, ImagePlus, Play, Send, Square } from "lucide-react";
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
      icon={
        running ? (
          <Play size={14} strokeWidth={1.75} />
        ) : (
          <Send size={14} strokeWidth={1.75} />
        )
      }
      onClick={onSend}
      loading={running}
      disabled={sendDisabled}
      data-testid="playground-run"
    >
      {running ? t("playground.running") : t("playground.run")}
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

  return (
    <>
      <TextArea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={t("playground.input_placeholder")}
        autoSize={{ minRows: 3, maxRows: 12 }}
        disabled={running || readOnly}
        maxLength={maxLength}
        showCount
        data-testid="playground-input"
      />
      <Text type="secondary" style={{ fontSize: 12 }}>
        {t("console.composer_hint")}
      </Text>
      <Space size={8}>
        <ReadonlyTooltip on={readOnly}>{sendButtonWithMissingTooltip}</ReadonlyTooltip>
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
        {running && (
          <Button
            danger
            icon={<Square size={14} strokeWidth={1.75} />}
            onClick={onStop}
            data-testid="playground-stop"
          >
            {t("playground.stop")}
          </Button>
        )}
      </Space>
    </>
  );
}
