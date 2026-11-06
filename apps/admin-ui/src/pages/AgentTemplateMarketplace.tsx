/**
 * Agent Template Marketplace — Stream Agent-Templates (M1-6b, tenant-scoped).
 *
 * Browse-and-fork shelf for the platform-curated Agent template catalog. Reads
 * the tenant-facing ``GET /v1/agents/templates`` view (published + enabled, one
 * card per name, each annotated with ``can_fork`` per the tenant's plan tier)
 * and renders a card wall of those templates.
 *
 * Forking materializes a tenant-owned ``agent_spec`` (``extends`` pinned to the
 * template version — the tier① security floor re-applies at build) via
 * ``POST /v1/agents/fork``, then navigates to the new agent's overview. The new
 * agent's ``agent_code`` is the name the operator picks in the fork modal.
 *
 * Tier-locked rows (``can_fork === false``) show a lock and can't be forked.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, App, Badge, Button, Card, Empty, Form, Input, Modal, Spin, Tag, Typography } from "antd";
import { Lock, Store } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  forkTemplate,
  listTemplateMarket,
  templateCategoryLabelKey,
  type TemplateMarketEntry,
  type TemplateTier,
} from "../api/agent-templates";
import { duplicateDeletedTarget, type DuplicateDeletedTarget } from "../api/agents";
import { ApiError } from "../api/client";
import { DuplicateDeletedNotice } from "../components/DuplicateDeletedNotice";
import { PageHeader } from "../components/PageHeader";

const { Text, Paragraph } = Typography;

const TIER_COLOR: Record<TemplateTier, string> = {
  free: "default",
  pro: "blue",
  enterprise: "gold",
};

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.code}: ${err.message}`;
  if (err instanceof Error) return err.message;
  return "unknown error";
}

interface ForkFormValues {
  name: string;
}

export function AgentTemplateMarketplace() {
  const { t } = useTranslation();
  const { message } = App.useApp();
  const navigate = useNavigate();
  const [form] = Form.useForm<ForkFormValues>();

  const [items, setItems] = useState<TemplateMarketEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The template a fork modal is currently open for (null = closed).
  const [forking, setForking] = useState<TemplateMarketEntry | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // B-278 — the chosen name clashed with a deleted / mid-purge agent; shown in
  // the modal.
  const [duplicateDeleted, setDuplicateDeleted] = useState<DuplicateDeletedTarget | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await listTemplateMarket());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const openFork = useCallback(
    (entry: TemplateMarketEntry) => {
      form.setFieldsValue({ name: entry.name });
      setDuplicateDeleted(null);
      setForking(entry);
    },
    [form],
  );

  const submitFork = useCallback(
    async (values: ForkFormValues) => {
      if (!forking) return;
      setSubmitting(true);
      setDuplicateDeleted(null);
      try {
        const result = await forkTemplate({
          template_name: forking.name,
          template_version: forking.version,
          name: values.name.trim(),
        });
        const { name, version } = result.record;
        message.success(t("agent_template_marketplace.forked_ok", { name }));
        setForking(null);
        navigate(
          `/agents/${encodeURIComponent(name)}/${encodeURIComponent(version)}/overview`,
        );
      } catch (err) {
        const deleted = duplicateDeletedTarget(err);
        if (deleted !== null) setDuplicateDeleted(deleted);
        else message.error(errorMessage(err));
      } finally {
        setSubmitting(false);
      }
    },
    [forking, message, navigate, t],
  );

  const body = useMemo(() => {
    if (loading) {
      return (
        <div style={{ textAlign: "center", padding: "32px 0" }} data-testid="atm-loading">
          <Spin />
        </div>
      );
    }
    if (error !== null) {
      return (
        <Alert
          type="error"
          showIcon
          message={t("agent_template_marketplace.failed_to_load")}
          description={error}
          data-testid="atm-error"
        />
      );
    }
    if (items.length === 0) {
      return <Empty description={t("agent_template_marketplace.empty")} data-testid="atm-empty" />;
    }
    return (
      <div
        data-testid="atm-root"
        style={{ display: "flex", flexDirection: "column", gap: 12 }}
      >
        {items.map((entry) => {
          const tier = entry.required_tier;
          const locked = !entry.can_fork;
          const categoryKey = entry.category ? templateCategoryLabelKey(entry.category) : null;
          const card = (
            <Card
              key={entry.name}
              size="small"
              hoverable={!locked}
              data-testid={`atm-card-${entry.name}`}
              style={locked ? { opacity: 0.6 } : undefined}
              styles={{ body: { padding: 14 } }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                <div style={{ minWidth: 0 }}>
                  <Text strong>{entry.display_name}</Text>
                  {entry.category && (
                    <Tag style={{ marginLeft: 8 }}>
                      {categoryKey ? t(categoryKey) : entry.category}
                    </Tag>
                  )}
                  <Tag color={TIER_COLOR[tier]} style={{ marginLeft: 4 }}>
                    {t(`agent_template_marketplace.tier_${tier}`)}
                  </Tag>
                  <div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {entry.name}@{entry.version}
                    </Text>
                  </div>
                  {entry.description && (
                    <Paragraph
                      type="secondary"
                      style={{ marginTop: 6, marginBottom: 0, fontSize: 12 }}
                      ellipsis={{ rows: 2 }}
                    >
                      {entry.description}
                    </Paragraph>
                  )}
                </div>
                <div style={{ flexShrink: 0, alignSelf: "center" }}>
                  {locked ? (
                    <Button
                      size="small"
                      disabled
                      icon={<Lock size={13} strokeWidth={1.5} />}
                      data-testid={`atm-locked-${entry.name}`}
                    >
                      {t("agent_template_marketplace.requires_tier", {
                        tier: t(`agent_template_marketplace.tier_${tier}`),
                      })}
                    </Button>
                  ) : (
                    <Button
                      type="primary"
                      size="small"
                      data-testid={`atm-fork-${entry.name}`}
                      onClick={() => openFork(entry)}
                    >
                      {t("agent_template_marketplace.fork")}
                    </Button>
                  )}
                </div>
              </div>
            </Card>
          );
          return locked ? (
            <Badge.Ribbon
              key={entry.name}
              text={t("agent_template_marketplace.locked_ribbon")}
              color="gray"
            >
              {card}
            </Badge.Ribbon>
          ) : (
            card
          );
        })}
      </div>
    );
  }, [loading, error, items, t, openFork]);

  return (
    <div>
      <PageHeader
        title={t("agent_template_marketplace.page_title")}
        icon={<Store size={20} strokeWidth={1.5} />}
        subtitle={t("agent_template_marketplace.subtitle")}
      />
      {body}

      <Modal
        open={forking !== null}
        title={t("agent_template_marketplace.fork_title", { name: forking?.display_name ?? "" })}
        okText={t("agent_template_marketplace.fork")}
        cancelText={t("common.cancel")}
        confirmLoading={submitting}
        onCancel={() => setForking(null)}
        onOk={() => form.submit()}
        destroyOnHidden
      >
        {duplicateDeleted !== null && (
          <DuplicateDeletedNotice target={duplicateDeleted} onNavigate={() => setForking(null)} />
        )}
        <Form form={form} layout="vertical" onFinish={(v) => void submitFork(v)} requiredMark={false}>
          <Form.Item
            name="name"
            label={t("agent_template_marketplace.fork_name_label")}
            extra={t("agent_template_marketplace.fork_name_hint")}
            rules={[
              { required: true, message: t("agent_template_marketplace.fork_name_required") },
              {
                pattern: /^[a-z0-9][a-z0-9-]{0,127}$/,
                message: t("agent_template_marketplace.fork_name_invalid"),
              },
            ]}
          >
            <Input
              aria-label={t("agent_template_marketplace.fork_name_label")}
              data-testid="atm-fork-name"
              placeholder="my-support-bot"
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
