/**
 * Create / Edit MCP Server drawer — Stream V-F (+ M1 custom headers / config).
 *
 * Opened from ``/settings/mcp-servers``.  In create mode it submits
 * ``POST /v1/mcp-servers``; in edit mode it submits
 * ``PATCH /v1/mcp-servers/{name}`` (token omitted if blank to preserve the
 * existing secret).  A "Test connection" button validates the relevant form
 * fields then calls ``POST /v1/mcp-servers/test`` — nothing is persisted.
 *
 * Fields are split across three tabs (basic / headers / config). Custom header
 * values are write-only (like the token): in edit mode the configured header
 * names pre-fill with blank values, and submitting replaces the whole set only
 * when at least one complete row is entered (otherwise the existing headers are
 * kept). Clearing all headers is delete+recreate (parity with auth-type).
 */
import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Tabs,
  Tooltip,
} from "antd";
import { Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "./FieldHelp";

import {
  createMcpServer,
  testMcpConnection,
  updateMcpServer,
  type McpAuthType,
  type McpServer,
  type McpTransport,
} from "../api/mcp-servers";
import { ApiError } from "../api/client";
import { useIsTenantSwitched } from "../tenant/useIsTenantSwitched";

// ── Types ──────────────────────────────────────────────────────────────────

interface HeaderRow {
  key: string;
  value: string;
}

interface McpServerForm {
  name: string;
  transport: McpTransport;
  url: string;
  auth_type: McpAuthType;
  token?: string;
  timeout_s: number;
  headers?: HeaderRow[];
  sse_read_timeout_s?: number | null;
}

export interface CreateMcpServerDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Fires after a successful create/update so the parent can refresh. */
  onSaved: () => void;
  /** When supplied the drawer opens in edit mode and pre-fills the fields. */
  editing?: McpServer | null;
}

// ── Constants ──────────────────────────────────────────────────────────────

const TRANSPORT_OPTIONS: { value: McpTransport; label: string }[] = [
  { value: "sse", label: "SSE" },
  { value: "streamable_http", label: "Streamable HTTP" },
];

const AUTH_OPTIONS: { value: McpAuthType; label: string }[] = [
  { value: "none", label: "None" },
  { value: "bearer", label: "Bearer token" },
];

const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const URL_PATTERN = /^https?:\/\/.+/;
const HEADER_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]{0,127}$/;

// Collect complete {key, value} rows into a custom_headers object; returns
// undefined when no complete rows (so the caller can omit the field).
export function collectHeaders(
  rows: HeaderRow[] | undefined,
): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  for (const row of rows ?? []) {
    const k = row?.key?.trim();
    const v = row?.value ?? "";
    if (k && v.trim()) out[k] = v;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

// ── Component ──────────────────────────────────────────────────────────────

export function CreateMcpServerDrawer({
  open,
  onClose,
  onSaved,
  editing = null,
}: CreateMcpServerDrawerProps) {
  const { t } = useTranslation();
  const { message } = App.useApp();
  // Cross-tenant W3 — 切入态只读:新建/保存是写操作,置灰。
  const isTenantSwitched = useIsTenantSwitched();

  const [form] = Form.useForm<McpServerForm>();
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [activeTab, setActiveTab] = useState("basic");
  const [testResult, setTestResult] = useState<
    { ok: true; count: number } | { ok: false; msg: string } | null
  >(null);

  // Controlled auth_type so the token field can show/hide reactively.
  const [authType, setAuthType] = useState<McpAuthType>("none");

  // ── Reset ──────────────────────────────────────────────────────────────

  const reset = useCallback(() => {
    form.resetFields();
    setTestResult(null);
    setAuthType("none");
    setActiveTab("basic");
  }, [form]);

  useEffect(() => {
    if (!open) {
      reset();
      return;
    }
    if (editing) {
      form.setFieldsValue({
        name: editing.name,
        transport: editing.transport,
        url: editing.url,
        auth_type: editing.auth_type,
        timeout_s: editing.timeout_s,
        sse_read_timeout_s: editing.sse_read_timeout_s ?? null,
        // Header names pre-fill with blank values — values are write-only.
        headers: (editing.custom_header_names ?? []).map((key) => ({
          key,
          value: "",
        })),
        // token always blank in edit mode — user must re-enter to rotate.
      });
      setAuthType(editing.auth_type);
    } else {
      form.setFieldsValue({
        transport: "sse",
        auth_type: "none",
        timeout_s: 30,
      });
      setAuthType("none");
    }
  }, [open, editing, form, reset]);

  // ── Handlers ───────────────────────────────────────────────────────────

  const handleCancel = useCallback(() => {
    reset();
    onClose();
  }, [reset, onClose]);

  const handleTestConnection = useCallback(async () => {
    let values: McpServerForm;
    try {
      values = await form.validateFields([
        "transport",
        "url",
        "auth_type",
        "token",
        "timeout_s",
      ]);
    } catch {
      return;
    }
    const all = form.getFieldsValue(true) as McpServerForm;
    setTesting(true);
    setTestResult(null);
    try {
      const body: Parameters<typeof testMcpConnection>[0] = {
        transport: values.transport,
        url: values.url,
        auth_type: values.auth_type,
        timeout_s: values.timeout_s,
      };
      if (values.auth_type === "bearer" && values.token) {
        body.token = values.token;
      }
      const headers = collectHeaders(all.headers);
      if (headers) body.custom_headers = headers;
      if (all.sse_read_timeout_s != null)
        body.sse_read_timeout_s = all.sse_read_timeout_s;
      const result = await testMcpConnection(body);
      setTestResult({ ok: true, count: result.tool_count });
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      setTestResult({ ok: false, msg });
    } finally {
      setTesting(false);
    }
  }, [form]);

  const handleSubmit = useCallback(async () => {
    try {
      await form.validateFields();
    } catch {
      return;
    }
    const values = form.getFieldsValue(true) as McpServerForm;
    const headers = collectHeaders(values.headers);
    setSubmitting(true);
    try {
      if (editing) {
        const body: Parameters<typeof updateMcpServer>[1] = {
          url: values.url,
          timeout_s: values.timeout_s,
          enabled: editing.enabled,
        };
        const trimmedToken = values.token?.trim();
        if (trimmedToken) body.token = trimmedToken;
        if (headers) body.custom_headers = headers;
        if (values.sse_read_timeout_s != null)
          body.sse_read_timeout_s = values.sse_read_timeout_s;
        await updateMcpServer(editing.name, body);
      } else {
        const body: Parameters<typeof createMcpServer>[0] = {
          name: values.name,
          transport: values.transport,
          url: values.url,
          auth_type: values.auth_type,
          timeout_s: values.timeout_s,
        };
        if (values.auth_type === "bearer" && values.token)
          body.token = values.token;
        if (headers) body.custom_headers = headers;
        if (values.sse_read_timeout_s != null)
          body.sse_read_timeout_s = values.sse_read_timeout_s;
        await createMcpServer(body);
      }
      onSaved();
      reset();
      onClose();
    } catch (err) {
      if (err instanceof ApiError && err.code === "MCP_CUSTOM_DISABLED") {
        message.error(t("create_mcp_server.custom_disabled"));
        return;
      }
      const msg =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : err instanceof Error
            ? err.message
            : "unknown error";
      message.error(msg);
    } finally {
      setSubmitting(false);
    }
  }, [form, editing, message, onSaved, onClose, reset, t]);

  // ── Render ─────────────────────────────────────────────────────────────

  const isEditing = editing !== null && editing !== undefined;

  const basicTab = (
    <>
      <Form.Item
        name="name"
        label={
          <>
            {t("create_mcp_server.field_name")}
            <FieldHelp text={t("create_mcp_server.field_name_help")} testId="cms-name" />
          </>
        }
        rules={[
          { required: true, message: t("create_mcp_server.name_required") },
          {
            pattern: NAME_PATTERN,
            message: t("create_mcp_server.name_required"),
          },
        ]}
      >
        <Input
          data-testid="cms-name"
          aria-label={t("create_mcp_server.field_name")}
          disabled={isEditing}
          maxLength={64}
          placeholder="my-mcp-server"
        />
      </Form.Item>

      <Form.Item
        name="transport"
        label={
          <>
            {t("create_mcp_server.field_transport")}
            <FieldHelp text={t("create_mcp_server.field_transport_help")} testId="cms-transport" />
          </>
        }
      >
        <Select<McpTransport>
          data-testid="cms-transport"
          aria-label={t("create_mcp_server.field_transport")}
          options={TRANSPORT_OPTIONS}
          disabled={isEditing}
        />
      </Form.Item>

      <Form.Item
        name="url"
        label={
          <>
            {t("create_mcp_server.field_url")}
            <FieldHelp text={t("create_mcp_server.field_url_help")} testId="cms-url" />
          </>
        }
        rules={[
          { required: true, message: t("create_mcp_server.url_required") },
          {
            validator: (_rule, value: string | undefined) => {
              const v = (value ?? "").trim();
              if (v === "" || URL_PATTERN.test(v)) return Promise.resolve();
              return Promise.reject(
                new Error(t("create_mcp_server.url_invalid")),
              );
            },
          },
        ]}
      >
        <Input
          data-testid="cms-url"
          aria-label={t("create_mcp_server.field_url")}
          placeholder="https://mcp.example.com/mcp"
          maxLength={2048}
        />
      </Form.Item>

      <Form.Item
        name="auth_type"
        label={
          <>
            {t("create_mcp_server.field_auth")}
            <FieldHelp text={t("create_mcp_server.field_auth_help")} testId="cms-auth" />
          </>
        }
      >
        <Select<McpAuthType>
          data-testid="cms-auth"
          aria-label={t("create_mcp_server.field_auth")}
          options={AUTH_OPTIONS}
          onChange={(val) => {
            setAuthType(val);
            setTestResult(null);
          }}
        />
      </Form.Item>

      {authType === "bearer" && (
        <Form.Item
          name="token"
          label={
            <>
              {t("create_mcp_server.field_token")}
              <FieldHelp text={t("create_mcp_server.field_token_help")} testId="cms-token" />
            </>
          }
          extra={
            isEditing
              ? t("create_mcp_server.token_hint_edit")
              : t("create_mcp_server.token_hint_create")
          }
          rules={
            isEditing
              ? []
              : [
                  {
                    required: true,
                    message: t("create_mcp_server.token_required"),
                  },
                ]
          }
        >
          <Input.Password
            data-testid="cms-token"
            aria-label={t("create_mcp_server.field_token")}
            maxLength={4096}
          />
        </Form.Item>
      )}

      <Form.Item>
        <Space direction="vertical" style={{ width: "100%" }}>
          <Button
            onClick={handleTestConnection}
            disabled={testing || submitting}
            data-testid="cms-test"
            icon={testing ? <Spin size="small" /> : undefined}
          >
            {t("create_mcp_server.test_connection")}
          </Button>

          {testResult !== null && (
            <div data-testid="cms-test-result">
              {testResult.ok ? (
                <Alert
                  type="success"
                  showIcon
                  message={t("create_mcp_server.test_ok", {
                    count: testResult.count,
                  })}
                />
              ) : (
                <Alert
                  type="error"
                  showIcon
                  message={t("create_mcp_server.test_failed")}
                  description={testResult.msg}
                />
              )}
            </div>
          )}
        </Space>
      </Form.Item>
    </>
  );

  const headersTab = (
    <>
      <p style={{ color: "var(--ant-color-text-secondary)", marginTop: 0 }}>
        {isEditing
          ? t("create_mcp_server.headers_hint_edit")
          : t("create_mcp_server.headers_hint")}
      </p>
      <Form.List name="headers">
        {(fields, { add, remove }) => (
          <>
            {fields.map(({ key, name, ...rest }) => (
              <Space
                key={key}
                align="baseline"
                style={{ display: "flex", marginBottom: 8 }}
              >
                <Form.Item
                  {...rest}
                  name={[name, "key"]}
                  rules={[
                    {
                      pattern: HEADER_NAME_PATTERN,
                      message: t("create_mcp_server.header_name_invalid"),
                    },
                  ]}
                  style={{ marginBottom: 0 }}
                >
                  <Input
                    data-testid={`cms-header-key-${name}`}
                    aria-label={t("create_mcp_server.header_name")}
                    placeholder="X-API-Key"
                    maxLength={128}
                  />
                </Form.Item>
                <Form.Item
                  {...rest}
                  name={[name, "value"]}
                  style={{ marginBottom: 0 }}
                >
                  <Input.Password
                    data-testid={`cms-header-value-${name}`}
                    aria-label={t("create_mcp_server.header_value")}
                    placeholder={
                      isEditing
                        ? t("create_mcp_server.header_value_keep")
                        : t("create_mcp_server.header_value")
                    }
                    maxLength={4096}
                    autoComplete="off"
                  />
                </Form.Item>
                <Button
                  type="text"
                  danger
                  icon={<Trash2 size={16} />}
                  aria-label={t("common.delete")}
                  data-testid={`cms-header-remove-${name}`}
                  onClick={() => remove(name)}
                />
              </Space>
            ))}
            <Button
              type="dashed"
              onClick={() => add({ key: "", value: "" })}
              icon={<Plus size={16} />}
              data-testid="cms-header-add"
              block
            >
              {t("create_mcp_server.header_add")}
            </Button>
          </>
        )}
      </Form.List>
    </>
  );

  const configTab = (
    <>
      <Form.Item
        name="timeout_s"
        label={
          <>
            {t("create_mcp_server.field_timeout")}
            <FieldHelp text={t("create_mcp_server.field_timeout_help")} testId="cms-timeout" />
          </>
        }
      >
        <InputNumber
          data-testid="cms-timeout"
          aria-label={t("create_mcp_server.field_timeout")}
          min={1}
          max={300}
          step={1}
          style={{ width: "100%" }}
        />
      </Form.Item>
      <Form.Item
        name="sse_read_timeout_s"
        label={
          <>
            {t("create_mcp_server.field_sse_read_timeout")}
            <FieldHelp
              text={t("create_mcp_server.field_sse_read_timeout_help")}
              testId="cms-sse-timeout"
            />
          </>
        }
        extra={t("create_mcp_server.sse_read_timeout_hint")}
      >
        <InputNumber
          data-testid="cms-sse-read-timeout"
          aria-label={t("create_mcp_server.field_sse_read_timeout")}
          min={1}
          max={3600}
          step={1}
          style={{ width: "100%" }}
        />
      </Form.Item>
    </>
  );

  return (
    <Drawer
      open={open}
      onClose={handleCancel}
      title={
        isEditing
          ? t("create_mcp_server.edit_title")
          : t("create_mcp_server.add_title")
      }
      width={520}
      destroyOnHidden
      footer={
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button
            onClick={handleCancel}
            disabled={submitting}
            data-testid="cms-cancel"
          >
            {t("common.cancel")}
          </Button>
          <Tooltip title={isTenantSwitched ? t("common.tenant_switched_readonly") : undefined}>
            <Button
              type="primary"
              loading={submitting}
              disabled={isTenantSwitched}
              onClick={handleSubmit}
              data-testid="cms-submit"
            >
              {isEditing
                ? t("create_mcp_server.submit_save")
                : t("create_mcp_server.submit_add")}
            </Button>
          </Tooltip>
        </div>
      }
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{ transport: "sse", auth_type: "none", timeout_s: 30 }}
        data-testid="cms-form"
      >
        <Tabs
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            {
              key: "basic",
              label: t("create_mcp_server.tab_basic"),
              children: basicTab,
            },
            {
              key: "headers",
              label: t("create_mcp_server.tab_headers"),
              children: headersTab,
            },
            {
              key: "config",
              label: t("create_mcp_server.tab_config"),
              children: configTab,
            },
          ]}
        />
      </Form>
    </Drawer>
  );
}
