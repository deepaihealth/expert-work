/**
 * Skill file editor — Capability Uplift Sprint #3 PR C, Mini-ADR U-20.
 *
 * Right pane. Three modes:
 *
 *   - ``SKILL.md`` selected → render the version's prompt fragment as
 *     a read-only Monaco buffer. Editing the prompt itself goes through
 *     ZIP import / the JSON-API ``POST .../versions`` flow; the operator
 *     UX never directly mutates the body from here.
 *   - Supporting file in **view** mode → read-only Monaco buffer with
 *     Edit / Rename / Delete action row.
 *   - Supporting file in **edit** mode → Monaco editor with Save /
 *     Cancel + an optional "show diff vs server" toggle that swaps the
 *     ``Editor`` for ``DiffEditor``.
 *
 * Save creates a new SkillVersion server-side; the parent receives the
 * new version through ``onSaved`` and updates the version picker.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Space,
  Switch,
  Typography,
} from "antd";
import Editor, { DiffEditor } from "@monaco-editor/react";
import {
  Edit3,
  GitCompare,
  PencilLine,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  decodeBase64Utf8,
  encodeUtf8Base64,
  type SkillVersion,
  type SupportingFileBody,
} from "../../api/skills";
import { type SkillApi } from "../../api/skillApi";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";
import { SKILL_MD_PATH } from "./FileTree";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

interface FileEditorProps {
  api: SkillApi;
  skillId: string;
  version: SkillVersion;
  /** Cross-tenant W4(D2)— 权威读口径(URL ``?tenant_id=`` 优先,"*" 折叠
   *  成 undefined),由 SkillDetail 统一下传。 */
  readScope: string | undefined;
  /** Path of the file currently selected in the tree, or ``null`` when
   *  nothing is selected (initial state). */
  selectedPath: string | null;
  /** Toggled to ``true`` by the editor when the user starts typing —
   *  parent reads this to gate the "are you sure?" dialog when the user
   *  picks another file. */
  onDirtyChange: (dirty: boolean) => void;
  /** Parent updates the version picker + file tree after each successful
   *  PUT. */
  onSaved: (newVersion: SkillVersion) => void;
  onRequestDelete: (path: string) => void;
  onRequestRename: (path: string) => void;
}

function languageFor(path: string): string {
  const ext = path.slice(path.lastIndexOf(".") + 1).toLowerCase();
  switch (ext) {
    case "md":
      return "markdown";
    case "py":
      return "python";
    case "js":
      return "javascript";
    case "ts":
      return "typescript";
    case "json":
      return "json";
    case "yaml":
    case "yml":
      return "yaml";
    case "html":
      return "html";
    case "css":
      return "css";
    case "sh":
      return "shell";
    case "sql":
      return "sql";
    case "toml":
      return "ini";
    default:
      return "plaintext";
  }
}

interface LoadedFile {
  path: string;
  body: SupportingFileBody;
  /** ``null`` when the file is binary (decode failed). */
  text: string | null;
}

export function FileEditor({
  api,
  skillId,
  version,
  readScope,
  selectedPath,
  onDirtyChange,
  onSaved,
  onRequestDelete,
  onRequestRename,
}: FileEditorProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — 切入态只读:编辑/重命名/删除/保存是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [loaded, setLoaded] = useState<LoadedFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [buffer, setBuffer] = useState<string>("");
  const [showDiff, setShowDiff] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Fetch when selection changes (and not SKILL.md — that one renders
  // from the version's prompt_fragment without a network round-trip).
  useEffect(() => {
    setMode("view");
    setShowDiff(false);
    setSaveError(null);
    setLoadError(null);
    if (selectedPath === null) {
      setLoaded(null);
      setBuffer("");
      onDirtyChange(false);
      return;
    }
    if (selectedPath === SKILL_MD_PATH) {
      setLoaded(null);
      setBuffer(version.prompt_fragment);
      onDirtyChange(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    void (async () => {
      try {
        const body = await api.getSupportingFile(
          skillId,
          version.version,
          selectedPath,
          readScope,
        );
        if (cancelled) return;
        const text = decodeBase64Utf8(body.content);
        setLoaded({ path: selectedPath, body, text });
        setBuffer(text ?? "");
        onDirtyChange(false);
      } catch (err) {
        if (cancelled) return;
        const msg =
          err instanceof ApiError
            ? `${err.code}: ${err.message}`
            : err instanceof Error
              ? err.message
              : "unknown error";
        setLoadError(msg);
        setLoaded(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, skillId, selectedPath, version.version, version.prompt_fragment, onDirtyChange, readScope]);

  const isSkillMd = selectedPath === SKILL_MD_PATH;
  const isBinary = loaded !== null && loaded.text === null;

  const dirty = useMemo(() => {
    if (mode !== "edit") return false;
    // SKILL.md edits the version's prompt_fragment directly (no ``loaded``
    // round-trip); everything else compares against the fetched file body.
    if (isSkillMd) return buffer !== version.prompt_fragment;
    if (loaded === null) return false;
    return buffer !== (loaded.text ?? "");
  }, [buffer, loaded, mode, isSkillMd, version.prompt_fragment]);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  const handleEdit = useCallback(() => {
    // SKILL.md is editable without a ``loaded`` body; other files need one.
    if (!isSkillMd && (loaded === null || isBinary)) return;
    setMode("edit");
    setSaveError(null);
  }, [isBinary, loaded, isSkillMd]);

  const handleCancel = useCallback(() => {
    setBuffer(isSkillMd ? version.prompt_fragment : (loaded?.text ?? ""));
    setMode("view");
    setShowDiff(false);
    setSaveError(null);
  }, [loaded, isSkillMd, version.prompt_fragment]);

  const handleSave = useCallback(async () => {
    if (selectedPath === null) return;
    setSaving(true);
    setSaveError(null);
    try {
      let newVersion: SkillVersion;
      if (isSkillMd) {
        // Edit the prompt body → new version (inherits supporting files).
        newVersion = await api.putPrompt(skillId, version.version, buffer);
      } else {
        if (loaded === null) return;
        const utf8 = new TextEncoder().encode(buffer);
        newVersion = await api.putSupportingFile(skillId, version.version, selectedPath, {
          content: encodeUtf8Base64(buffer),
          size: utf8.byteLength,
          mime: loaded.body.mime || "text/plain",
        });
      }
      message.success(t("skills.file_saved", { version: newVersion.version }));
      setMode("view");
      setShowDiff(false);
      onSaved(newVersion);
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setSaveError(msg);
    } finally {
      setSaving(false);
    }
  }, [
    api,
    buffer,
    loaded,
    message,
    onSaved,
    selectedPath,
    skillId,
    t,
    version.version,
    isSkillMd,
  ]);

  if (selectedPath === null) {
    return (
      <Card size="small" data-testid="skill-editor-empty">
        <div
          style={{
            minHeight: 320,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 8,
            color: "var(--ew-text-tertiary)",
            fontSize: 13,
          }}
        >
          <PencilLine size={20} strokeWidth={1.5} />
          <span>{t("skills.detail_no_file_selected")}</span>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("skills.detail_select_file_hint")}
          </Text>
        </div>
      </Card>
    );
  }

  return (
    <Card size="small" data-testid="skill-editor-pane">
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 8,
        }}
      >
        <Text style={{ fontFamily: "var(--ew-font-mono)", fontSize: 13 }} strong>
          {selectedPath}
        </Text>
        {loaded !== null && (
          <Text type="secondary" style={{ fontSize: 11 }}>
            {t("skills.file_size_label")}: {loaded.body.size} ·{" "}
            {t("skills.file_mime_label")}: {loaded.body.mime || "—"}
          </Text>
        )}
        <span style={{ flex: 1 }} />
        {mode === "edit" && !isBinary && (
          <Space size={6}>
            <Switch
              size="small"
              checked={showDiff}
              onChange={setShowDiff}
              checkedChildren={<GitCompare size={11} strokeWidth={1.75} />}
              unCheckedChildren={<GitCompare size={11} strokeWidth={1.75} />}
              data-testid="skill-editor-diff-toggle"
            />
            <Text type="secondary" style={{ fontSize: 11 }}>
              {t("skills.detail_diff_toggle")}
            </Text>
          </Space>
        )}
      </div>

      {isSkillMd && mode === "view" && (
        <Alert
          type="info"
          showIcon
          message={t("skills.detail_skill_md_edit_hint")}
          style={{ marginBottom: 12 }}
          data-testid="skill-md-edit-hint"
        />
      )}

      {loadError !== null && (
        <Alert
          type="error"
          showIcon
          message={t("skills.file_load_failed")}
          description={loadError}
          style={{ marginBottom: 12 }}
          data-testid="skill-editor-load-error"
        />
      )}

      {saveError !== null && (
        <Alert
          type="error"
          showIcon
          message={t("skills.file_save_failed")}
          description={saveError}
          style={{ marginBottom: 12 }}
          data-testid="skill-editor-save-error"
        />
      )}

      {isBinary && (
        <Alert
          type="warning"
          showIcon
          message={t("skills.file_binary_placeholder", {
            size: loaded?.body.size,
            mime: loaded?.body.mime,
          })}
          style={{ marginBottom: 12 }}
          data-testid="skill-editor-binary"
        />
      )}

      {!isBinary && (
        // ``@monaco-editor/react`` doesn't forward ``data-testid`` to a
        // queryable DOM node — wrap so Playwright + vitest both have a
        // stable handle on the editor container.
        <div data-testid="skill-editor-monaco">
          {mode === "edit" && showDiff && loaded !== null ? (
            <DiffEditor
              language={languageFor(selectedPath)}
              original={loaded.text ?? ""}
              modified={buffer}
              theme="vs-dark"
              height="calc(100vh - 480px)"
              options={{
                renderSideBySide: true,
                originalEditable: false,
                minimap: { enabled: false },
                fontFamily: "var(--ew-font-mono)",
                fontSize: 12,
                scrollBeyondLastLine: false,
              }}
            />
          ) : (
            <Editor
              language={languageFor(selectedPath)}
              value={buffer}
              onChange={(v) => setBuffer(v ?? "")}
              theme="vs-dark"
              height="calc(100vh - 480px)"
              options={{
                readOnly: mode === "view" || loading,
                minimap: { enabled: false },
                fontFamily: "var(--ew-font-mono)",
                fontSize: 12,
                tabSize: 2,
                scrollBeyondLastLine: false,
                renderWhitespace: "boundary",
                wordWrap: "on",
              }}
            />
          )}
        </div>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: 8,
          marginTop: 12,
        }}
      >
        {/* SKILL.md is editable (Phase D-2) but has no rename/delete — it's
            a required member of every skill. Saving edits the prompt body. */}
        {isSkillMd && mode === "view" && (
          <ReadonlyTooltip on={isTenantSwitched}>
            <Button
              size="small"
              type="primary"
              icon={<Edit3 size={13} strokeWidth={1.75} />}
              onClick={handleEdit}
              disabled={isTenantSwitched}
              data-testid="skill-editor-edit-btn"
            >
              {t("skills.file_action_edit")}
            </Button>
          </ReadonlyTooltip>
        )}
        {!isSkillMd && mode === "view" && !isBinary && (
          <>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                size="small"
                danger
                icon={<Trash2 size={13} strokeWidth={1.75} />}
                onClick={() => onRequestDelete(selectedPath)}
                disabled={isTenantSwitched}
                data-testid="skill-editor-delete-btn"
              >
                {t("skills.file_action_delete")}
              </Button>
            </ReadonlyTooltip>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                size="small"
                icon={<PencilLine size={13} strokeWidth={1.75} />}
                onClick={() => onRequestRename(selectedPath)}
                disabled={isTenantSwitched}
                data-testid="skill-editor-rename-btn"
              >
                {t("skills.file_action_rename")}
              </Button>
            </ReadonlyTooltip>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                size="small"
                type="primary"
                icon={<Edit3 size={13} strokeWidth={1.75} />}
                onClick={handleEdit}
                disabled={isTenantSwitched}
                data-testid="skill-editor-edit-btn"
              >
                {t("skills.file_action_edit")}
              </Button>
            </ReadonlyTooltip>
          </>
        )}
        {mode === "edit" && (
          <>
            <Button
              size="small"
              icon={<X size={13} strokeWidth={1.75} />}
              onClick={handleCancel}
              disabled={saving}
              data-testid="skill-editor-cancel-btn"
            >
              {t("skills.file_action_cancel")}
            </Button>
            <ReadonlyTooltip on={isTenantSwitched}>
              <Button
                size="small"
                type="primary"
                icon={<Save size={13} strokeWidth={1.75} />}
                onClick={handleSave}
                loading={saving}
                disabled={!dirty || isTenantSwitched}
                data-testid="skill-editor-save-btn"
              >
                {t("skills.file_action_save")}
              </Button>
            </ReadonlyTooltip>
          </>
        )}
      </div>
    </Card>
  );
}
