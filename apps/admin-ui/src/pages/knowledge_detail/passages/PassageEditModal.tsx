/**
 * Edit a passage's text, or add one at the end of the document — B-175
 * P3-PR3.
 *
 * An edit is sent against the revision the passage was read at. When
 * someone saved it in between (409 ``revision_conflict``) the editor stays
 * open with the server's copy and its revision: 「改用对方的版本」 loads it
 * into the editor; saving again (「用我的版本覆盖」) sends this text against
 * the server's revision. Only the chunks whose text changed are embedded
 * again (billed); the server refuses text over twice the passage cap.
 */
import { useState, type ReactElement } from "react";
import { Alert, App, Button, Form, Input, Modal, Select, Space, Typography } from "antd";
import { useTranslation } from "react-i18next";

import {
  addPassage,
  updatePassage,
  type Passage,
  type PassagePatch,
} from "../../../api/knowledge_maintenance";
import { HeadingPath } from "../HeadingPath";
import { usePassageLabel } from "./PassageCard";
import { passageWriteError } from "./passageErrors";

const { Paragraph } = Typography;

export type EditTarget = { mode: "edit"; passage: Passage } | { mode: "add" } | null;

export interface PassageEditModalProps {
  baseName: string;
  documentId: string;
  target: EditTarget;
  onClose: () => void;
  onSaved: (passage: Passage, mode: "edit" | "add") => void;
  /** The server's copy arrived with a conflict: show it in the list too. */
  onConflict?: (passage: Passage) => void;
  /** The passage is gone (a re-ingest replaced it): reload the list. */
  onGone: () => void;
}

interface EditorValues {
  content: string;
  heading_path: string[];
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

function EditorBody({
  baseName,
  documentId,
  editing,
  onClose,
  onSaved,
  onConflict,
  onGone,
}: Omit<PassageEditModalProps, "target"> & { editing: Passage | null }): ReactElement {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const [form] = Form.useForm<EditorValues>();
  /** The copy the edit is based on: as read, or the server's after a conflict. */
  const [current, setCurrent] = useState<Passage | null>(editing);
  const [conflict, setConflict] = useState<Passage | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async ({ content, heading_path }: EditorValues) => {
    const heading = heading_path ?? [];
    setSaving(true);
    try {
      if (current === null) {
        const added = await addPassage(baseName, documentId, { content, heading_path: heading });
        message.success(t("kb_maintenance.editor_added"));
        onSaved(added, "add");
        onClose();
        return;
      }
      const headingChanged = !sameList(heading, current.heading_path);
      if (content === current.content && !headingChanged) {
        onClose();
        return;
      }
      const body: PassagePatch = {
        content,
        revision: current.revision,
        ...(headingChanged ? { heading_path: heading } : {}),
      };
      const saved = await updatePassage(baseName, documentId, current.id, body);
      message.success(t("kb_maintenance.editor_saved", { revision: saved.revision }));
      onSaved(saved, "edit");
      onClose();
    } catch (err) {
      const failure = passageWriteError(t, err);
      if (failure.kind === "conflict") {
        setCurrent(failure.passage);
        setConflict(failure.passage);
        onConflict?.(failure.passage);
      } else if (failure.kind === "gone") {
        message.warning(t("kb_maintenance.error_gone"));
        onGone();
        onClose();
      } else {
        message.error(failure.text);
      }
    } finally {
      setSaving(false);
    }
  };

  const useTheirs = () => {
    if (conflict === null) return;
    form.setFieldsValue({ content: conflict.content, heading_path: conflict.heading_path });
    setConflict(null);
  };

  return (
    <div data-testid="kb-passage-editor">
      {conflict !== null && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          data-testid="kb-passage-editor-conflict"
          message={t("kb_maintenance.conflict_title", { revision: conflict.revision })}
          description={
            <>
              <Paragraph style={{ marginBottom: 8 }}>{t("kb_maintenance.conflict_body")}</Paragraph>
              <HeadingPath path={conflict.heading_path} />
              <Paragraph
                style={{ whiteSpace: "pre-wrap", maxHeight: 200, overflow: "auto", fontSize: 13 }}
              >
                {conflict.content}
              </Paragraph>
              <Button size="small" onClick={useTheirs} data-testid="kb-passage-editor-use-theirs">
                {t("kb_maintenance.conflict_use_theirs")}
              </Button>
            </>
          }
        />
      )}
      <Form<EditorValues>
        form={form}
        layout="vertical"
        onFinish={(values) => void save(values)}
        initialValues={{
          content: editing?.content ?? "",
          heading_path: editing?.heading_path ?? [],
        }}
      >
        <Form.Item
          name="heading_path"
          label={t("kb_maintenance.editor_heading")}
          extra={t("kb_maintenance.editor_heading_hint")}
        >
          <Select
            mode="tags"
            notFoundContent={null}
            aria-label={t("kb_maintenance.editor_heading")}
            data-testid="kb-passage-editor-heading"
          />
        </Form.Item>
        <Form.Item
          name="content"
          label={t("kb_maintenance.editor_content")}
          extra={t("kb_maintenance.editor_content_hint")}
          rules={[{ required: true, whitespace: true, message: t("kb_maintenance.error_empty") }]}
        >
          <Input.TextArea
            autoSize={{ minRows: 6, maxRows: 18 }}
            aria-label={t("kb_maintenance.editor_content")}
            data-testid="kb-passage-editor-content"
          />
        </Form.Item>
        <Space style={{ display: "flex", justifyContent: "flex-end" }}>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button
            type="primary"
            htmlType="submit"
            loading={saving}
            data-testid="kb-passage-editor-save"
          >
            {conflict !== null ? t("kb_maintenance.conflict_overwrite") : t("common.save")}
          </Button>
        </Space>
      </Form>
    </div>
  );
}

export function PassageEditModal({ target, ...props }: PassageEditModalProps): ReactElement {
  const { t } = useTranslation();
  const label = usePassageLabel();
  const editing = target?.mode === "edit" ? target.passage : null;
  return (
    <Modal
      open={target !== null}
      onCancel={props.onClose}
      footer={null}
      width={720}
      destroyOnHidden
      title={
        editing !== null
          ? t("kb_maintenance.editor_title_edit", { label: label(editing) })
          : t("kb_maintenance.editor_title_add")
      }
    >
      {target !== null && <EditorBody {...props} editing={editing} />}
    </Modal>
  );
}
