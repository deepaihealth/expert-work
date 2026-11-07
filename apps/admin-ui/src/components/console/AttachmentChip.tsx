/**
 * AttachmentChip — one attachment of a user message (B-295): click to preview in a
 * modal (images; PDF; txt / md / csv as text) with a download button, or — for types
 * a browser can't show safely (Office etc.) — click to download. A file retention has
 * already removed shows 「文件已过期或已删除」 instead of an error or a broken image.
 *
 * What is rendered follows the server's ``Content-Type`` (``api/attachments.ts``),
 * not the chip's name; the name only picks preview-vs-download for the click and the
 * chip's label. Object URLs are created for the image / PDF preview only and revoked
 * when the preview closes.
 */
import { useCallback, useEffect, useRef, useState, type JSX, type KeyboardEvent } from "react";
import { Alert, Button, Modal, Spin, Tag, Typography } from "antd";
import { Download, FileText, Image as ImageIcon } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  fetchSessionAttachment,
  isAttachmentGone,
  previewKindOf,
  saveBlob,
  type AttachmentBytes,
  type PreviewKind,
} from "../../api/attachments";
import { errMessage, type TenantScope } from "../../api/client";
import type { Attachment } from "../turn/types";

const { Text } = Typography;

/** Document extensions (of the stored ``uploads/<leaf>``) the browser can show. */
const PREVIEW_DOC_EXTS: ReadonlySet<string> = new Set([".pdf", ".txt", ".md", ".csv"]);
/** Text previews stop here; the full file is one click away. */
const TEXT_PREVIEW_MAX_BYTES = 200 * 1024;

/** 终审 F2 — 品牌深蓝底上的半透明白底白字,两主题都可读(原 UserBubble 标签样式)。 */
const CHIP_STYLE = {
  fontSize: 11,
  background: "rgba(255,255,255,0.22)",
  color: "#fff",
} as const;

type Loaded =
  | { state: "loading" }
  | { state: "gone" }
  | { state: "error"; message: string }
  | {
      state: "ready";
      blob: Blob;
      /** Save-as name from the server's ``Content-Disposition``. */
      filename: string;
      kind: PreviewKind | null;
      text?: string;
      truncated?: boolean;
    };

function opensPreview(attachment: Attachment): boolean {
  if (attachment.kind === "image") return true;
  const leaf = attachment.value.split("/").pop() ?? "";
  const dot = leaf.lastIndexOf(".");
  return dot > 0 && PREVIEW_DOC_EXTS.has(leaf.slice(dot).toLowerCase());
}

async function readPreview({ blob, contentType, filename }: AttachmentBytes): Promise<Loaded> {
  const kind = previewKindOf(contentType);
  if (kind !== "text") return { state: "ready", blob, filename, kind };
  const text = await blob.slice(0, TEXT_PREVIEW_MAX_BYTES).text();
  return {
    state: "ready",
    blob,
    filename,
    kind,
    text,
    truncated: blob.size > TEXT_PREVIEW_MAX_BYTES,
  };
}

/** Image / PDF bytes re-typed as the whitelisted server type, behind an object URL
 *  that is revoked as soon as the preview goes away. */
function useObjectUrl(loaded: Loaded | null, contentType: string | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const blob = loaded?.state === "ready" && loaded.kind !== "text" && loaded.kind !== null
    ? loaded.blob
    : null;
  useEffect(() => {
    if (blob === null || contentType === null) return undefined;
    const next = URL.createObjectURL(new Blob([blob], { type: contentType }));
    setUrl(next);
    return () => {
      URL.revokeObjectURL(next);
      setUrl(null);
    };
  }, [blob, contentType]);
  return url;
}

export interface AttachmentChipProps {
  attachment: Attachment;
  /** The conversation the message belongs to; ``null`` → a plain, inert label. */
  threadId: string | null;
  tenantScope?: TenantScope;
}

export function AttachmentChip({
  attachment,
  threadId,
  tenantScope,
}: AttachmentChipProps): JSX.Element {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [contentType, setContentType] = useState<string | null>(null);
  const request = useRef(0);
  const previewable = opensPreview(attachment);
  const url = useObjectUrl(loaded, contentType);

  const close = useCallback(() => {
    request.current += 1;
    setOpen(false);
    setLoaded(null);
    setContentType(null);
  }, []);

  const activate = useCallback(async () => {
    if (threadId === null) return;
    const mine = ++request.current;
    if (previewable) {
      setOpen(true);
      setLoaded({ state: "loading" });
    }
    let next: Loaded;
    try {
      const fetched = await fetchSessionAttachment(threadId, attachment.value, tenantScope);
      if (mine !== request.current) return;
      if (!previewable) {
        saveBlob(fetched.blob, fetched.filename);
        return;
      }
      setContentType(fetched.contentType);
      next = await readPreview(fetched);
    } catch (err) {
      next = isAttachmentGone(err)
        ? { state: "gone" }
        : { state: "error", message: errMessage(err) };
    }
    if (mine !== request.current) return;
    // A download-only chip that failed opens the same modal, so the failure is seen.
    setOpen(true);
    setLoaded(next);
  }, [threadId, attachment.value, tenantScope, previewable]);

  if (threadId === null) {
    return (
      <Tag bordered={false} style={CHIP_STYLE}>
        {attachment.name}
      </Tag>
    );
  }

  const hint = t(previewable ? "console.attachment_preview_hint" : "console.attachment_download_hint");
  const Icon = previewable ? (attachment.kind === "image" ? ImageIcon : FileText) : Download;
  const onKeyDown = (e: KeyboardEvent<HTMLSpanElement>) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      void activate();
    }
  };
  const ready = loaded?.state === "ready" ? loaded : null;

  return (
    <>
      <Tag
        bordered={false}
        role="button"
        tabIndex={0}
        aria-label={`${attachment.name} — ${hint}`}
        title={hint}
        data-testid="console-attachment-chip"
        onClick={() => void activate()}
        onKeyDown={onKeyDown}
        style={{ ...CHIP_STYLE, cursor: "pointer" }}
      >
        <Icon size={11} strokeWidth={1.75} style={{ marginRight: 4, verticalAlign: -1 }} />
        {attachment.name}
      </Tag>
      <Modal
        open={open}
        onCancel={close}
        title={attachment.name}
        width={ready?.kind === "pdf" ? 960 : 760}
        footer={
          <Button
            icon={<Download size={14} strokeWidth={1.75} />}
            disabled={ready === null}
            onClick={() => ready && saveBlob(ready.blob, ready.filename)}
            data-testid="console-attachment-download"
          >
            {t("console.attachment_download")}
          </Button>
        }
      >
        <div data-testid="console-attachment-preview">
          <PreviewBody loaded={loaded} url={url} name={attachment.name} />
        </div>
      </Modal>
    </>
  );
}

interface PreviewBodyProps {
  loaded: Loaded | null;
  url: string | null;
  name: string;
}

function PreviewBody({ loaded, url, name }: PreviewBodyProps): JSX.Element | null {
  const { t } = useTranslation();
  if (loaded === null) return null;
  if (loaded.state === "loading") {
    return (
      <div style={{ textAlign: "center", padding: 32 }}>
        <Spin />
        <div style={{ marginTop: 8 }}>
          <Text type="secondary">{t("console.attachment_loading")}</Text>
        </div>
      </div>
    );
  }
  if (loaded.state === "gone") {
    return (
      <div data-testid="console-attachment-expired" style={{ textAlign: "center", padding: 32 }}>
        <Text type="secondary">{t("console.attachment_expired")}</Text>
      </div>
    );
  }
  if (loaded.state === "error") {
    return (
      <Alert type="error" showIcon message={t("console.attachment_load_failed", { detail: loaded.message })} />
    );
  }
  if (loaded.kind === "text") {
    return (
      <>
        {loaded.truncated && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 8 }}
            message={t("console.attachment_text_truncated", { kb: TEXT_PREVIEW_MAX_BYTES / 1024 })}
          />
        )}
        <pre
          data-testid="console-attachment-text"
          style={{
            maxHeight: "60vh",
            overflow: "auto",
            margin: 0,
            padding: 12,
            fontFamily: "var(--ew-font-mono)",
            fontSize: 12,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
            background: "var(--ew-surface-subtle, rgba(0,0,0,0.03))",
            borderRadius: 6,
          }}
        >
          {loaded.text}
        </pre>
      </>
    );
  }
  if (loaded.kind === "image" && url !== null) {
    return (
      <div style={{ textAlign: "center" }}>
        <img
          data-testid="console-attachment-image"
          src={url}
          alt={name}
          style={{ maxWidth: "100%", maxHeight: "70vh", objectFit: "contain" }}
        />
      </div>
    );
  }
  if (loaded.kind === "pdf" && url !== null) {
    // Accepted risk (B-295 review L5): the PDF renders in a same-origin blob: iframe with
    // no ``sandbox`` — a sandboxed frame stops Chrome's built-in viewer from loading at
    // all. The browser's PDF viewer does not run page script in this origin, the blob is
    // re-typed to ``application/pdf`` (never HTML), and the server only labels bytes
    // that start with ``%PDF-`` as PDF.
    return (
      <iframe
        data-testid="console-attachment-pdf"
        src={url}
        title={name}
        style={{ width: "100%", height: "70vh", border: 0 }}
      />
    );
  }
  if (loaded.kind === null) {
    return <Text type="secondary">{t("console.attachment_no_preview")}</Text>;
  }
  return null;
}
