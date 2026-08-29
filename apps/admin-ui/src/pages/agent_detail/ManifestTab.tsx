/**
 * Manifest tab — visual-first config (mirrors the create flow).
 *
 * Renders the same schema-driven :class:`ManifestEditor` the create modal
 * uses (Form tabs: basic / model / prompt / tools / mcp / knowledge /
 * skills / subagents / memory / governance, plus a raw YAML escape-hatch
 * tab). Edits accumulate in a buffer; ``Save`` writes through
 * ``PUT /v1/agents/{name}/{version}`` (the backend re-runs the full
 * :class:`ManifestLoader`, so server errors flow back through the
 * envelope); ``Reset`` re-derives the buffer from the latest server
 * snapshot by remounting the editor.
 *
 * Form-position persistence (#2): the editor is keyed by ``resetNonce``
 * only — a refreshed server snapshot flows in through ``initialYaml``
 * (the editor resyncs in place) instead of forcing a remount that reset
 * the whole form position on every save. The active config group also
 * persists in the URL (``?group=``) so it survives remounts and tab
 * switches.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Alert, Button, Card, Space, Typography } from "antd";
import { dump as yamlDump } from "js-yaml";
import { RotateCcw, Save, Trash2, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  discardAgentDraft,
  publishAgentDraft,
  saveAgentDraft,
  type AgentDetailResponse,
} from "../../api/agents";
import { ManifestEditor } from "../../components/manifest-editor";
import { CONFIG_GROUPS } from "../../components/manifest-editor/groups";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

const { Text } = Typography;

interface ManifestTabProps {
  detail: AgentDetailResponse;
  /** Called after a successful save — parent refetches so the SHA, the
   *  ``updated_at`` timestamp, and any server-side coercion show up. */
  onSaved: () => void;
}

export function ManifestTab({ detail, onSaved }: ManifestTabProps) {
  const { t } = useTranslation();
  const r = detail.record;
  // Track C W2 — 切入态只读:保存是写操作,切入目标租户后置灰。
  const isTenantSwitched = useIsTenantSwitched();

  /** 编辑器从**草稿**起步(有的话),否则从线上那一版。
   *
   *  「上次没改完的东西还在」是草稿存在的全部理由 —— 打开页面却看到线上版本,
   *  等于草稿白存。 */
  const editingSpec = (r.draft?.spec ?? r.spec) as Record<string, unknown>;
  const snapshotYaml = useMemo(
    () => yamlDump(editingSpec, { lineWidth: 120 }),
    [editingSpec],
  );

  /** ``If-Match`` 送的值:和编辑器里那一版对应(草稿优先),与后端
   *  ``_editing_sha`` 是同一条规矩。 */
  const editingSha = r.draft?.spec_sha256 ?? r.spec_sha256;

  const [buffer, setBuffer] = useState<string>(snapshotYaml);
  // Bumped on Reset to remount the editor and re-seed it from the snapshot.
  const [resetNonce, setResetNonce] = useState(0);
  const [busy, setBusy] = useState<null | "draft" | "publish" | "discard">(null);
  const [error, setError] = useState<string | null>(null);
  const [buildWarning, setBuildWarning] = useState<string | null>(null);
  const [buildError, setBuildError] = useState<string | null>(null);

  // #2 — the editor is no longer key-remounted when the server snapshot
  // changes; adopt the refreshed snapshot into the buffer instead. In
  // practice ``snapshotYaml`` only changes right after a successful save
  // (the parent's quiet refetch), so this is the explicit post-save resync,
  // not a mid-edit clobber.
  useEffect(() => {
    setBuffer(snapshotYaml);
  }, [snapshotYaml]);

  // #2 — active config group lives in the URL (``?group=``): it survives the
  // editor remounting (Reset), page-tab switches, even a full reload. An
  // unknown/absent value falls back to the editor's own default (basic).
  const [searchParams, setSearchParams] = useSearchParams();
  const groupParam = searchParams.get("group");
  const activeGroup =
    groupParam !== null && CONFIG_GROUPS.some((g) => g.id === groupParam)
      ? groupParam
      : undefined;
  const handleGroupChange = useCallback(
    (id: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("group", id);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const handleReset = useCallback(() => {
    setBuffer(snapshotYaml);
    setError(null);
    setResetNonce((n) => n + 1);
  }, [snapshotYaml]);

  const hasDraft = r.draft != null;
  const saving = busy !== null;

  /** 三个动作共用的错误处理 —— 409 是并发,给人话;其余照抛。 */
  const runAction = useCallback(
    async (kind: "draft" | "publish" | "discard", fn: () => Promise<AgentDetailResponse>) => {
      setBusy(kind);
      setError(null);
      setBuildWarning(null);
      setBuildError(null);
      try {
        const result = await fn();
        setBuildWarning(result.build_warning ?? null);
        setBuildError(result.build_error ?? null);
        onSaved();
      } catch (err) {
        // 409 = 别人在你编辑期间动过这一版。这不是「操作失败」那种技术错误,
        // 而是一条要人做决定的消息(去看对方改了什么,再决定怎么合)。
        if (err instanceof ApiError && err.code === "MANIFEST_STALE_WRITE") {
          setError(t("manifest_tab.stale_write"));
        } else {
          const message =
            err instanceof ApiError
              ? `${err.code}: ${err.message}`
              : err instanceof Error
                ? err.message
                : "unknown error";
          setError(message);
        }
      } finally {
        setBusy(null);
      }
    },
    [onSaved, t],
  );

  const handleSaveDraft = useCallback(
    () =>
      runAction("draft", () =>
        saveAgentDraft(r.name, r.version, { manifest_yaml: buffer }, editingSha),
      ),
    [runAction, r.name, r.version, buffer, editingSha],
  );

  const handlePublish = useCallback(
    // 发布匹配的是**线上**那一版(要替换掉的那个),不是草稿。
    () => runAction("publish", () => publishAgentDraft(r.name, r.version, r.spec_sha256)),
    [runAction, r.name, r.version, r.spec_sha256],
  );

  const handleDiscard = useCallback(
    () => runAction("discard", () => discardAgentDraft(r.name, r.version, editingSha)),
    [runAction, r.name, r.version, editingSha],
  );

  return (
    <Card data-testid="manifest-tab">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 12,
        }}
      >
        <Text type="secondary" style={{ fontSize: 12 }}>
          {t("manifest_tab.hint")}
        </Text>
        <Space size={8}>
          <Button
            size="small"
            icon={<RotateCcw size={14} strokeWidth={1.75} />}
            onClick={handleReset}
            disabled={saving}
            data-testid="manifest-reset-btn"
          >
            {t("manifest_tab.reset")}
          </Button>
          {hasDraft && (
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                size="small"
                icon={<Trash2 size={14} strokeWidth={1.75} />}
                onClick={handleDiscard}
                loading={busy === "discard"}
                disabled={saving || isTenantSwitched}
                data-testid="manifest-discard-btn"
              >
                {t("manifest_tab.discard_draft")}
              </Button>
            </ReadonlyTooltip>
          )}
          <ReadonlyTooltip on={isTenantSwitched}>
            <Button
              size="small"
              icon={<Save size={14} strokeWidth={1.75} />}
              onClick={handleSaveDraft}
              loading={busy === "draft"}
              disabled={saving || isTenantSwitched}
              data-testid="manifest-save-btn"
            >
              {t("manifest_tab.save_draft")}
            </Button>
          </ReadonlyTooltip>
          <ReadonlyTooltip on={isTenantSwitched}>
            <Button
              size="small"
              type="primary"
              icon={<Upload size={14} strokeWidth={1.75} />}
              onClick={handlePublish}
              loading={busy === "publish"}
              // 没有草稿就没有可发布的东西 —— 后端 409,这里不该让人点到那一步。
              disabled={saving || isTenantSwitched || !hasDraft}
              data-testid="manifest-publish-btn"
            >
              {t("manifest_tab.publish")}
            </Button>
          </ReadonlyTooltip>
        </Space>
      </div>

      {hasDraft && (
        <Alert
          type="info"
          showIcon
          message={t("manifest_tab.draft_pending")}
          description={t("manifest_tab.draft_pending_detail", {
            who: r.draft?.updated_by ?? "",
            when: r.draft ? new Date(r.draft.updated_at).toLocaleString() : "",
          })}
          style={{ marginBottom: 12 }}
          data-testid="manifest-draft-banner"
        />
      )}

      {buildError !== null && (
        <Alert
          type="warning"
          showIcon
          closable
          onClose={() => setBuildError(null)}
          message={t("manifest_tab.draft_saved_but_unpublishable")}
          description={buildError}
          style={{ marginBottom: 12 }}
          data-testid="manifest-build-error"
        />
      )}

      {buildWarning !== null && (
        <Alert
          type="warning"
          showIcon
          closable
          onClose={() => setBuildWarning(null)}
          message={t("manifest_tab.saved_but_not_runnable")}
          description={buildWarning}
          style={{ marginBottom: 12 }}
          data-testid="manifest-build-warning"
        />
      )}

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("manifest_tab.save_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="manifest-error"
        />
      )}

      <ManifestEditor
        key={resetNonce}
        mode="edit"
        initialYaml={snapshotYaml}
        onChange={setBuffer}
        activeGroup={activeGroup}
        onActiveGroupChange={handleGroupChange}
        // 委派增强层 3 — 已保存 Agent 才有「生成委派策略」(后端读已保存
        // manifest 起草);创建流/模板表单不传,按钮不渲染。
        agentRef={{ name: r.name, version: r.version }}
      />
    </Card>
  );
}
