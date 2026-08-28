/**
 * AnswerBubble — the console's agent-answer block: failure banner, channelled
 * answer segments (commentary de-emphasised, the final segment as markdown),
 * the streaming typewriter tail, the historical-turn fallback view, and the
 * per-turn artifact download row.
 *
 * JSX lifted from ``components/turn/TurnCard.tsx`` (the ``playground-turn-
 * answer`` block, TurnCard.tsx:769-865, plus its ``readOnly``/not-yet-loaded
 * early-return branch, TurnCard.tsx:658-730) — no rendering logic changed,
 * just consolidated into one component that reads ``ConsoleTurn`` instead of
 * ``Turn`` + a scattering of loose props. See
 * .superpowers/sdd/2026-08-18-debug-console-pr-a-console/task-10-brief.md.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Spin, Typography } from "antd";
import { Download } from "lucide-react";
import { useTranslation } from "react-i18next";

import { turnArtifacts } from "../../api/tool_timeline";
import type { TurnSummary } from "../../api/turn_summary";
import { MarkdownView } from "../MarkdownView";
import { CommentarySegmentLine } from "../turn/CommentarySegmentLine";
import {
  FullTextModal,
  FullTextTrigger,
  type FullTextState,
} from "../turn/FullTextModal";
import type { ConsoleTurn } from "./types";

const { Text } = Typography;

export interface AnswerBubbleProps {
  /** 用 turn.turn.status / error / events、loadState、fallbackLines。 */
  turn: ConsoleTurn;
  /** 父级 memo(``summarizeTurn(turn.turn.events)``)。 */
  summary: TurnSummary;
  /** 流式:当前未落地步的 content(打字机);settled 或历史轮 undefined。 */
  liveText?: string;
  onDownloadArtifact: (name: string) => Promise<void>;
}

export function AnswerBubble({ turn, summary, liveText, onDownloadArtifact }: AnswerBubbleProps) {
  const { t } = useTranslation();
  const status = turn.turn.status;
  const segments = summary.segments;
  const hasText = segments.length > 0;
  // Fix round 1 (Important 1) — the turn's first LLM message has no settled
  // segment yet (no `updates` frame has landed), so `liveText` alone must be
  // enough to show the scroll container instead of the generic "running"
  // placeholder; the placeholder is reserved for running-with-nothing-to-show.
  const isLiveStreaming = status === "running" && Boolean(liveText);
  const showAnswerBlock = hasText || isLiveStreaming;
  const answerFullText = segments.map((s) => s.text).join("\n\n");
  const historyNotLoaded = turn.loadState !== "done" && turn.turn.events.length === 0;

  const [fullText, setFullText] = useState<FullTextState | null>(null);

  // I2 — while streaming, keep the capped answer box pinned to the newest
  // text (same as TurnCard). Fix round 1 (Important 2) — `liveText` is in the
  // dep array too: a liveText-only update (new characters landing without a
  // new settled segment) must still re-pin the scroll.
  const answerScrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = answerScrollRef.current;
    if (status === "running" && node) {
      node.scrollTop = node.scrollHeight;
    }
  }, [segments, status, liveText]);

  // Worker-registered artifacts only appear in the end frame's snapshot —
  // ``turnArtifacts`` merges it over the tool-call derivation.
  const artifacts = useMemo(() => turnArtifacts(turn.turn.events), [turn.turn.events]);
  const [downloadingArtifact, setDownloadingArtifact] = useState<string | null>(null);
  const downloadArtifact = useCallback(
    async (name: string) => {
      setDownloadingArtifact(name);
      try {
        await onDownloadArtifact(name);
      } finally {
        setDownloadingArtifact(null);
      }
    },
    [onDownloadArtifact],
  );

  return (
    <div data-testid="playground-turn-answer">
      {status === "error" && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message={t("playground.turn_failed")}
          description={turn.turn.error || undefined}
          data-testid="playground-turn-error"
        />
      )}

      {showAnswerBlock ? (
        <>
          <div
            ref={answerScrollRef}
            style={{ maxHeight: 420, overflowY: "auto" }}
            data-testid="playground-turn-answer-scroll"
          >
            {segments.map((seg, i) => {
              const isLast = i === segments.length - 1;
              const asCommentary =
                status === "running" ? !isLast : seg.channel === "commentary";
              if (asCommentary) {
                return (
                  <CommentarySegmentLine
                    key={i}
                    text={seg.text}
                    label={t("playground.segment_commentary")}
                  />
                );
              }
              return status === "running" ? (
                <Text key={i} style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
                  {seg.text}
                </Text>
              ) : (
                <MarkdownView key={i}>{seg.text}</MarkdownView>
              );
            })}
            {isLiveStreaming && (
              <Text
                style={{ whiteSpace: "pre-wrap", fontSize: 13 }}
                data-testid="console-answer-live"
              >
                {liveText}
              </Text>
            )}
          </div>
          <FullTextTrigger
            onClick={() =>
              setFullText({ title: t("playground.view_full_text"), text: answerFullText })
            }
          />
        </>
      ) : status === "running" ? (
        <Text style={{ whiteSpace: "pre-wrap", fontSize: 13 }}>
          {t("playground.turn_running")}
        </Text>
      ) : historyNotLoaded ? (
        <>
          {turn.fallbackLines.length > 0 && (
            <>
              <div
                style={{
                  alignSelf: "flex-start",
                  maxWidth: "85%",
                  fontSize: 13,
                  opacity: 0.75,
                  maxHeight: 420,
                  overflowY: "auto",
                }}
              >
                {turn.fallbackLines.map((l, i) =>
                  l.channel === "commentary" ? (
                    <CommentarySegmentLine
                      key={i}
                      text={l.text}
                      label={t("playground.segment_commentary")}
                    />
                  ) : (
                    <MarkdownView key={i}>{l.text}</MarkdownView>
                  ),
                )}
              </div>
              <FullTextTrigger
                onClick={() =>
                  setFullText({
                    title: t("playground.view_full_text"),
                    text: turn.fallbackLines.map((l) => l.text).join("\n\n"),
                  })
                }
              />
            </>
          )}
          {turn.loadState !== "error" && (
            <div
              style={{ display: "flex", alignItems: "center", gap: 6, opacity: 0.6, fontSize: 12 }}
            >
              <Spin size="small" />
              <span>{t("playground.history_loading")}</span>
            </div>
          )}
        </>
      ) : status !== "error" ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("playground.turn_no_text")}
        </Text>
      ) : null}

      {artifacts.length > 0 && (
        <div
          style={{
            marginTop: 8,
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            alignItems: "center",
          }}
          data-testid="playground-turn-artifacts"
        >
          <Text type="secondary" style={{ fontSize: 11 }}>
            {t("playground.workspace_artifacts")}:
          </Text>
          {artifacts.map((a) => (
            <Button
              key={a.name}
              size="small"
              icon={<Download size={11} strokeWidth={1.75} />}
              loading={downloadingArtifact === a.name}
              onClick={() => void downloadArtifact(a.name)}
              aria-label={t("playground.artifact_download", { name: a.name })}
              data-testid="playground-turn-artifact-download"
            >
              {a.name}
            </Button>
          ))}
        </div>
      )}

      <FullTextModal state={fullText} onClose={() => setFullText(null)} />
    </div>
  );
}
