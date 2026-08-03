/**
 * Skill version metadata panel — Capability Uplift Sprint #3 PR C.
 *
 * Right-pane summary that sits above the editor: shows the version's
 * static descriptors plus the two cross-cutting badges from Mini-ADRs
 * U-15 (``lazy_load``) and U-24 (``high_risk``). Mutation lives in the
 * sibling editor; this panel never writes.
 */
import { useState } from "react";
import { Alert, AutoComplete, Button, Card, Space, Tag, Tooltip, Typography } from "antd";
import { ShieldAlert, Sparkles, Zap } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SkillRecord, SkillVersion } from "../../api/skills";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";

const { Text } = Typography;

interface MetadataPanelProps {
  skill: SkillRecord;
  version: SkillVersion;
  /** When set, the category row becomes an editable AutoComplete (platform
   *  library only — the tenant SDK has no category patch). */
  categoryOptions?: string[];
  onSaveCategory?: (category: string) => Promise<void>;
  /** Cross-tenant W4(D2)— 只读态(切入态 ∪ "*" 聚合深链外租户读),由
   *  SkillDetail 统一判定下传;改分类是写操作,置灰。 */
  readonly: boolean;
}

export function MetadataPanel({
  skill,
  version,
  categoryOptions,
  onSaveCategory,
  readonly,
}: MetadataPanelProps) {
  const { t } = useTranslation();
  const editableCategory = onSaveCategory !== undefined;
  const [categoryDraft, setCategoryDraft] = useState(skill.category ?? "");
  const [savingCategory, setSavingCategory] = useState(false);
  const categoryDirty = categoryDraft.trim() !== (skill.category ?? "").trim();

  const saveCategory = async () => {
    if (!onSaveCategory || !categoryDirty) return;
    setSavingCategory(true);
    try {
      await onSaveCategory(categoryDraft.trim());
    } finally {
      setSavingCategory(false);
    }
  };

  return (
    <Card
      title={t("skills.metadata_title")}
      size="small"
      style={{ marginBottom: 16 }}
      data-testid="skill-metadata-panel"
    >
      {version.high_risk && (
        <Alert
          type="warning"
          showIcon
          icon={<ShieldAlert size={14} strokeWidth={1.75} />}
          message={t("skills.detail_high_risk_warning")}
          style={{ marginBottom: 12 }}
          data-testid="skill-high-risk-warning"
        />
      )}

      <dl
        style={{
          display: "grid",
          gridTemplateColumns: "160px 1fr",
          rowGap: 8,
          columnGap: 16,
          margin: 0,
          fontSize: 13,
        }}
      >
        <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("skills.col_category")}</dt>
        <dd style={{ margin: 0 }}>
          {editableCategory ? (
            <Space.Compact style={{ width: "100%", maxWidth: 360 }}>
              <AutoComplete
                value={categoryDraft}
                onChange={setCategoryDraft}
                options={(categoryOptions ?? []).map((c) => ({ value: c }))}
                placeholder={t("platform_skills.detail_category_placeholder")}
                style={{ width: "100%" }}
                filterOption={(input, option) =>
                  (option?.value ?? "")
                    .toLowerCase()
                    .includes(input.toLowerCase())
                }
                aria-label={t("platform_skills.detail_category_label")}
                data-testid="skill-category-input"
              />
              <ReadonlyTooltip on={readonly}>
                <Button
                  type="primary"
                  loading={savingCategory}
                  disabled={!categoryDirty || savingCategory || readonly}
                  onClick={() => void saveCategory()}
                  data-testid="skill-category-save"
                >
                  {t("platform_skills.detail_category_save")}
                </Button>
              </ReadonlyTooltip>
            </Space.Compact>
          ) : skill.category ? (
            skill.category
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          )}
        </dd>

        <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("skills.col_description")}</dt>
        <dd style={{ margin: 0 }}>{skill.description}</dd>

        <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("skills.col_tools")}</dt>
        <dd style={{ margin: 0 }}>
          {version.tool_names.length === 0 ? (
            <Text type="secondary" style={{ fontSize: 12 }}>
              —
            </Text>
          ) : (
            <Space size={4} wrap>
              {version.tool_names.map((tool) => (
                <Tag key={tool} bordered={false}>
                  {tool}
                </Tag>
              ))}
            </Space>
          )}
        </dd>

        <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("skills.col_authored_by")}</dt>
        <dd style={{ margin: 0 }}>{version.authored_by}</dd>

        <dt style={{ color: "var(--ew-text-tertiary)" }}>
          {t("skills.detail_version_picker_label")}
        </dt>
        <dd style={{ margin: 0 }}>
          <Space size={6}>
            <Tag bordered={false}>v{version.version}</Tag>
            {/* Lazy badge — Mini-ADR U-15 */}
            <Tooltip
              title={
                version.lazy_load
                  ? t("skills.detail_lazy_tooltip")
                  : t("skills.detail_eager_tooltip")
              }
            >
              <Tag
                bordered={false}
                color={version.lazy_load ? "blue" : "default"}
                icon={
                  version.lazy_load ? (
                    <Sparkles size={11} strokeWidth={1.75} style={{ marginRight: 4 }} />
                  ) : (
                    <Zap size={11} strokeWidth={1.75} style={{ marginRight: 4 }} />
                  )
                }
                data-testid={
                  version.lazy_load ? "skill-lazy-badge" : "skill-eager-badge"
                }
              >
                {version.lazy_load ? t("skills.detail_lazy_badge") : "Eager"}
              </Tag>
            </Tooltip>
            {/* High-risk badge — Mini-ADR U-24 */}
            {version.high_risk && (
              <Tooltip title={t("skills.detail_high_risk_tooltip")}>
                <Tag
                  bordered={false}
                  color="error"
                  icon={
                    <ShieldAlert size={11} strokeWidth={1.75} style={{ marginRight: 4 }} />
                  }
                  data-testid="skill-high-risk-badge"
                >
                  🔒 {t("skills.detail_high_risk_badge")}
                </Tag>
              </Tooltip>
            )}
          </Space>
        </dd>

        <dt style={{ color: "var(--ew-text-tertiary)" }}>{t("skills.col_created")}</dt>
        <dd style={{ margin: 0 }}>{new Date(version.created_at).toLocaleString()}</dd>
      </dl>
    </Card>
  );
}
