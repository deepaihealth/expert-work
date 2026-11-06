/**
 * PurgeAgentModal — B-278「彻底删除」a deleted agent.
 *
 * Opens on ``GET .../purge-preview`` so the admin sees, before anything is
 * removed, every version that goes, how many rows of each kind go with it, and
 * what is kept (usage / billing / audit). Anything in ``blocking`` (a version
 * still live, or another agent using it as a subagent) disarms the button
 * outright; otherwise the admin types the name verbatim to arm it — same
 * type-to-confirm gate as ``DeleteAgentModal``.
 *
 * The purge runs synchronously and step-wise on the server. A partial run
 * (``409 AGENT_PURGE_INCOMPLETE`` with the summary in ``error.details``; a 200
 * with ``ok=false`` is still accepted as a fallback) leaves the name
 * ``purging`` and clicking again finishes it, so the modal stays open, lists
 * the failed steps and re-arms as 「再点一次继续清理」.
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, App, Input, Modal, Skeleton, Space, Tag, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { ApiError, errMessage } from "../api/client";
import {
  AGENT_PURGE_BLOCKED,
  AGENT_BUSY,
  AGENT_PURGE_INCOMPLETE,
  getAgentPurgePreview,
  purgeAgent,
  type AgentPurgeBlocking,
  type AgentPurgePreview,
} from "../api/agents";

const { Paragraph, Text } = Typography;

/** Count keys with a translated label, in display order. Unknown keys from a
 *  newer backend still show, under their raw name. */
const COUNT_ORDER = [
  "conversations",
  "runs",
  "memories",
  "artifacts",
  "artifact_versions",
  "approvals",
  "feedback",
  "retrieval_logs",
  "memory_jobs",
  "memory_dlq",
  "triggers",
  "trigger_runs",
  "webhooks",
  "webhook_deliveries",
  "agent_instances",
  "quality_scores",
  "drift_alerts",
  "eval_datasets",
  "curation_candidates",
  "uploads",
  "images",
  "secret_allowlist",
  "revisions",
  "nas_users",
] as const;

function nonZeroCounts(counts: Record<string, number>): Array<[string, number]> {
  const known: string[] = [...COUNT_ORDER];
  const extra = Object.keys(counts).filter((k) => !known.includes(k));
  return [...known, ...extra]
    .filter((k) => (counts[k] ?? 0) > 0)
    .map((k) => [k, counts[k]] as [string, number]);
}

function isBlocked(blocking: AgentPurgeBlocking): boolean {
  return blocking.active_versions.length > 0 || blocking.referenced_by.length > 0;
}

function readBlocking(err: ApiError): AgentPurgeBlocking | null {
  const active = err.details?.active_versions;
  const refs = err.details?.referenced_by;
  if (!Array.isArray(active) || !Array.isArray(refs)) return null;
  return { active_versions: active.map(String), referenced_by: refs.map(String) };
}

/** NAS outcome counters on a purge summary, in display order. The backend
 *  contract (#2026): show each to the admin when non-zero. */
const NAS_FIELDS = [
  "nas_quarantined",
  "nas_symlinks_unlinked",
  "nas_symlinks_skipped",
  "nas_failed_users",
] as const;

/** The non-zero NAS counters of a purge summary (200 body or the details of a
 *  409 ``AGENT_PURGE_INCOMPLETE``), as ``[field, count]``. */
function nasNotes(summary: Record<string, unknown> | undefined): Array<[string, number]> {
  if (summary === undefined) return [];
  return NAS_FIELDS.map((f) => [f, summary[f]] as [string, unknown])
    .filter((e): e is [string, number] => typeof e[1] === "number" && e[1] > 0);
}

/** ``AGENT_PURGE_INCOMPLETE`` carries the purge summary in ``error.details``;
 *  its ``failures`` is step → exception type. */
function readFailures(err: ApiError): Record<string, string> {
  const failures = err.details?.failures;
  if (typeof failures !== "object" || failures === null || Array.isArray(failures)) return {};
  return Object.fromEntries(
    Object.entries(failures as Record<string, unknown>).map(([step, type]) => [step, String(type)]),
  );
}

interface PurgeAgentModalProps {
  open: boolean;
  name: string;
  version: string;
  onClose: () => void;
  /** Fired after a fully clean purge (``ok=true``). */
  onPurged: () => void;
}

export function PurgeAgentModal({ open, name, version, onClose, onPurged }: PurgeAgentModalProps) {
  const { t } = useTranslation();
  const { message, modal } = App.useApp();
  const [preview, setPreview] = useState<AgentPurgePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState(false);
  const [failedSteps, setFailedSteps] = useState<Record<string, string> | null>(null);
  const [partialNas, setPartialNas] = useState<Array<[string, number]>>([]);
  const [purgeError, setPurgeError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setPreview(null);
    setPreviewError(null);
    setConfirmText("");
    setFailedSteps(null);
    setPartialNas([]);
    setPurgeError(null);
    getAgentPurgePreview(name, version).then(
      (p) => {
        if (alive) setPreview(p);
      },
      (err: unknown) => {
        if (alive) setPreviewError(errMessage(err));
      },
    );
    return () => {
      alive = false;
    };
  }, [open, name, version]);

  const blocked = preview !== null && isBlocked(preview.blocking);
  const armed = preview !== null && !blocked && confirmText === name && !busy;

  const close = useCallback(() => {
    if (!busy) onClose();
  }, [busy, onClose]);

  const onConfirm = useCallback(async () => {
    if (!armed) return;
    setBusy(true);
    setFailedSteps(null);
    setPartialNas([]);
    setPurgeError(null);
    try {
      const summary = await purgeAgent(name, version, confirmText);
      const nas = nasNotes(summary as unknown as Record<string, unknown>);
      if (summary.ok) {
        message.success(t("agent_lifecycle.purge_ok", { name }));
        if (nas.length > 0) {
          // Done, but the admin should know what happened on the NAS.
          modal.info({
            title: t("agent_lifecycle.purge_ok", { name }),
            content: <NasNotes notes={nas} />,
          });
        }
        onPurged();
        return;
      }
      // Fallback: an older backend answered a partial run with 200 + ok=false.
      setFailedSteps(summary.failures);
      setPartialNas(nas);
    } catch (err) {
      if (err instanceof ApiError && err.code === AGENT_PURGE_INCOMPLETE) {
        // Partial run: the summary rides in error.details; click again to finish.
        setFailedSteps(readFailures(err));
        setPartialNas(nasNotes(err.details));
        return;
      }
      if (err instanceof ApiError && err.code === AGENT_PURGE_BLOCKED) {
        // Something changed since the preview — show the server's reasons.
        const blocking = readBlocking(err);
        if (blocking !== null) {
          setPreview((p) => (p === null ? p : { ...p, blocking }));
          return;
        }
      }
      if (err instanceof ApiError && err.code === AGENT_BUSY) {
        // Another purge of this name is running (or a write on it collided);
        // nothing was deleted — retry later, the button stays usable.
        setPurgeError(t("agent_lifecycle.purge_busy"));
        return;
      }
      setPurgeError(t("agent_lifecycle.purge_failed", { error: errMessage(err) }));
    } finally {
      setBusy(false);
    }
  }, [armed, name, version, confirmText, message, modal, t, onPurged]);

  return (
    <Modal
      open={open}
      onCancel={close}
      onOk={() => void onConfirm()}
      okText={
        failedSteps !== null ? t("agent_lifecycle.purge_continue") : t("agent_lifecycle.purge")
      }
      cancelText={t("common.cancel")}
      okButtonProps={{
        danger: true,
        disabled: !armed,
        loading: busy,
        "data-testid": "purge-agent-confirm-ok",
      }}
      title={t("agent_lifecycle.purge_title", { name })}
      destroyOnHidden
    >
      <div data-testid="purge-agent-modal">
        {previewError !== null && (
          <Alert
            type="error"
            showIcon
            message={t("agent_lifecycle.purge_preview_failed", { error: previewError })}
            data-testid="purge-agent-preview-error"
          />
        )}
        {preview === null && previewError === null && (
          <div data-testid="purge-agent-loading">
            <Text type="secondary">{t("agent_lifecycle.purge_loading")}</Text>
            <Skeleton active paragraph={{ rows: 3 }} />
          </div>
        )}
        {preview !== null && (
          <PreviewBody
            preview={preview}
            blocked={blocked}
            name={name}
            confirmText={confirmText}
            busy={busy}
            onConfirmTextChange={setConfirmText}
            onPressEnter={() => void onConfirm()}
          />
        )}
        {failedSteps !== null && (
          <Alert
            type="warning"
            showIcon
            message={t("agent_lifecycle.purge_partial_title")}
            description={
              <>
                {Object.keys(failedSteps).length > 0 && (
                  <div>
                    {t("agent_lifecycle.purge_partial_body", {
                      steps: Object.entries(failedSteps)
                        .map(([step, type]) => `${step} (${type})`)
                        .join(", "),
                    })}
                  </div>
                )}
                <NasNotes notes={partialNas} />
                <div>{t("agent_lifecycle.purge_partial_retry")}</div>
              </>
            }
            style={{ marginTop: 12 }}
            data-testid="purge-agent-partial"
          />
        )}
        {purgeError !== null && (
          <Alert
            type="error"
            showIcon
            message={purgeError}
            style={{ marginTop: 12 }}
            data-testid="purge-agent-error"
          />
        )}
      </div>
    </Modal>
  );
}

/** One plain-language line per non-zero NAS counter. */
function NasNotes({ notes }: { notes: Array<[string, number]> }) {
  const { t } = useTranslation();
  if (notes.length === 0) return null;
  return (
    <ul style={{ margin: "4px 0", paddingLeft: 18 }} data-testid="purge-agent-nas-notes">
      {notes.map(([field, count]) => (
        <li key={field}>{t(`agent_lifecycle.nas.${field}`, { count })}</li>
      ))}
    </ul>
  );
}

interface PreviewBodyProps {
  preview: AgentPurgePreview;
  blocked: boolean;
  name: string;
  confirmText: string;
  busy: boolean;
  onConfirmTextChange: (value: string) => void;
  onPressEnter: () => void;
}

function PreviewBody({
  preview,
  blocked,
  name,
  confirmText,
  busy,
  onConfirmTextChange,
  onPressEnter,
}: PreviewBodyProps) {
  const { t } = useTranslation();
  const counts = nonZeroCounts(preview.counts);
  const { active_versions: active, referenced_by: refs } = preview.blocking;

  return (
    <>
      <Alert
        type="error"
        showIcon
        message={t("agent_lifecycle.purge_irreversible")}
        style={{ marginBottom: 12 }}
      />
      <Paragraph style={{ marginBottom: 4 }}>{t("agent_lifecycle.purge_versions")}</Paragraph>
      <Space size={[4, 4]} wrap style={{ marginBottom: 12 }} data-testid="purge-agent-versions">
        {preview.versions.map((v) => (
          <Tag key={v}>v{v}</Tag>
        ))}
      </Space>

      {preview.default_agent_cleared && (
        <Alert
          type="warning"
          showIcon
          message={t("agent_lifecycle.purge_default_agent")}
          style={{ marginBottom: 12 }}
          data-testid="purge-agent-default"
        />
      )}

      <Paragraph style={{ marginBottom: 4 }}>{t("agent_lifecycle.purge_counts_title")}</Paragraph>
      {counts.length === 0 ? (
        <Paragraph type="secondary" data-testid="purge-agent-counts-none">
          {t("agent_lifecycle.purge_counts_none")}
        </Paragraph>
      ) : (
        <>
          <dl
            data-testid="purge-agent-counts"
            style={{
              display: "grid",
              gridTemplateColumns: "1fr auto",
              rowGap: 4,
              columnGap: 16,
              margin: "0 0 4px",
              fontSize: 13,
            }}
          >
            {counts.map(([key, n]) => (
              <div key={key} style={{ display: "contents" }} data-testid={`purge-count-${key}`}>
                <dt>{t(`agent_lifecycle.counts.${key}`, { defaultValue: key })}</dt>
                <dd style={{ margin: 0, textAlign: "right" }} className="mono">
                  {n.toLocaleString()}
                </dd>
              </div>
            ))}
          </dl>
          <Paragraph type="secondary" style={{ fontSize: 12 }}>
            {t("agent_lifecycle.purge_counts_rest_zero")}
          </Paragraph>
        </>
      )}
      {preview.threads_capped && (
        <Paragraph type="warning" style={{ fontSize: 12 }} data-testid="purge-agent-capped">
          {t("agent_lifecycle.purge_threads_capped")}
        </Paragraph>
      )}
      <Paragraph type="secondary" style={{ fontSize: 12 }} data-testid="purge-agent-kept">
        {t("agent_lifecycle.purge_kept")}
      </Paragraph>

      {blocked ? (
        <Alert
          type="error"
          showIcon
          message={t("agent_lifecycle.purge_blocked_title")}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {active.length > 0 && (
                <li>
                  {t("agent_lifecycle.purge_blocked_active", {
                    versions: active.map((v) => `v${v}`).join(", "),
                  })}
                </li>
              )}
              {refs.length > 0 && (
                <li>{t("agent_lifecycle.purge_blocked_referenced", { refs: refs.join(", ") })}</li>
              )}
            </ul>
          }
          data-testid="purge-agent-blocked"
        />
      ) : (
        <>
          <Paragraph style={{ marginBottom: 6 }}>
            {t("agent_lifecycle.purge_type_to_confirm")} <Text code>{name}</Text>
          </Paragraph>
          <Input
            value={confirmText}
            onChange={(e) => onConfirmTextChange(e.target.value)}
            onPressEnter={onPressEnter}
            placeholder={name}
            disabled={busy}
            aria-label={t("agent_lifecycle.purge_type_to_confirm")}
            data-testid="purge-agent-confirm-input"
          />
          <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
            {t("agent_lifecycle.purge_timeout_hint")}
          </Paragraph>
        </>
      )}
    </>
  );
}
