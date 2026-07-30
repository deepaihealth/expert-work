/**
 * Create Agent modal — Stream H.2 PR 2.
 *
 * Antd Modal hosting the curated ``ManifestEditor`` (tabs + Monaco) preloaded
 * with a capability-adaptive Agent manifest stub. On submit, POSTs to
 * ``/v1/agents`` and lets the backend's :class:`ManifestLoader` validate the
 * payload end-to-end — the same envelope errors that surface in ``ManifestTab``
 * apply here, so the UI only renders them. Uses a Modal (not a Drawer) to match
 * the platform Agent-template create flow.
 *
 * On success the modal closes and the parent decides what to do
 * (refresh list + optionally navigate to the new agent's detail page).
 */
import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Modal, Space, Typography } from "antd";
import { Plus, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { getPlatformEmbeddingStatus } from "../api/platform_embedding_config";
import { createAgent, type AgentDetailResponse } from "../api/agents";
import { ManifestEditor } from "./manifest-editor";
import { BASE_MANIFEST_YAML, buildDefaultManifest } from "./manifest-editor/defaults";
import { loadModelCatalog } from "./manifest-editor/catalog";
import { dumpYaml } from "./manifest-editor/yaml";

const { Text } = Typography;

/** Re-exported for back-compat; the drawer now seeds a capability-adaptive
 *  default at open time (falls back to this base). */
export const DEFAULT_AGENT_YAML = BASE_MANIFEST_YAML;

interface CreateAgentModalProps {
  open: boolean;
  onClose: () => void;
  /** Fires after a successful POST so the parent can refresh the list
   *  or navigate. Receives the created agent's record. */
  onCreated: (record: AgentDetailResponse) => void;
}

export function CreateAgentModal({ open, onClose, onCreated }: CreateAgentModalProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [buffer, setBuffer] = useState<string>(BASE_MANIFEST_YAML);
  const [initialYaml, setInitialYaml] = useState<string>(BASE_MANIFEST_YAML);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [embeddingConfigured, setEmbeddingConfigured] = useState<boolean | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setEmbeddingConfigured(null);
    getPlatformEmbeddingStatus().then(
      (s) => {
        if (alive) setEmbeddingConfigured(s.configured);
      },
      () => {
        if (alive) setEmbeddingConfigured(true);
      }, // fail-open: a transient status-fetch error must not block legitimate creation; the control-plane build-time embedder gate is the hard backstop
    );
    loadModelCatalog().then(
      (catalog) => {
        if (!alive) return;
        const seeded = dumpYaml(buildDefaultManifest(catalog));
        setInitialYaml(seeded);
        setBuffer(seeded);
      },
      () => {
        /* keep BASE_MANIFEST_YAML seed on failure */
      },
    );
    return () => {
      alive = false;
    };
  }, [open]);

  const reset = useCallback(() => {
    setBuffer(BASE_MANIFEST_YAML);
    setInitialYaml(BASE_MANIFEST_YAML);
    setError(null);
    setSubmitting(false);
  }, []);

  const handleCancel = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const created = await createAgent({ manifest_yaml: buffer });
      onCreated(created);
      reset();
    } catch (err) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }, [buffer, onCreated, reset]);

  const blocked = embeddingConfigured === false;

  return (
    <Modal
      open={open}
      onCancel={handleCancel}
      title={
        <Space size={8}>
          <Plus size={16} strokeWidth={1.75} />
          {t("create_agent.title")}
        </Space>
      }
      width={900}
      destroyOnHidden
      footer={
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button
            icon={<X size={14} strokeWidth={1.75} />}
            onClick={handleCancel}
            disabled={submitting}
            data-testid="create-agent-cancel"
          >
            {t("create_agent.cancel")}
          </Button>
          <Button
            type="primary"
            icon={<Plus size={14} strokeWidth={1.75} />}
            onClick={handleSubmit}
            loading={submitting}
            disabled={submitting || blocked}
            data-testid="create-agent-submit"
          >
            {t("create_agent.submit")}
          </Button>
        </div>
      }
    >
      {/* The testid lives on a content wrapper (not the <Modal> root): antd puts
          a forwarded data-testid on ``.ant-modal-root``, which Playwright treats
          as hidden even when open. This inner div is visible when the modal is
          open and unmounted (destroyOnHidden) when closed. */}
      <div data-testid="create-agent-modal">
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginBottom: 8 }}>
          {t("create_agent.hint")}
        </Text>

      {error !== null && (
        <Alert
          type="error"
          showIcon
          message={t("create_agent.create_failed")}
          description={error}
          style={{ marginBottom: 12 }}
          data-testid="create-agent-error"
        />
      )}

      {blocked ? (
        <div data-testid="create-agent-embedding-gate">
          <Alert
            type="warning"
            showIcon
            message={t("create_agent.embedding_required_title")}
            description={t("create_agent.embedding_required_desc")}
            style={{ marginBottom: 12 }}
          />
          <Button
            type="primary"
            data-testid="create-agent-embedding-cta"
            onClick={() => {
              onClose();
              navigate("/settings/platform");
            }}
          >
            {t("create_agent.embedding_required_cta")}
          </Button>
        </div>
      ) : (
        /* Key-remount the editor when the catalog-derived default seed arrives
          (ManifestEditor would also adopt the new initialYaml in place via its
          controlled resync — the remount just keeps the discard explicit). The
          brief pre-catalog window (one cached round-trip on a freshly-opened
          drawer) discards edits made before the seed lands — deliberate; the
          seed is the intended start. */
        <ManifestEditor
          key={initialYaml}
          mode="create"
          initialYaml={initialYaml}
          onChange={setBuffer}
        />
      )}
      </div>
    </Modal>
  );
}
