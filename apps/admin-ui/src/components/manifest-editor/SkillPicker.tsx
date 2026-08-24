/**
 * Skill picker — Tier 2 capability, its own form tab. Unlike a bare name
 * dropdown this surfaces the metadata an author needs to decide *whether* to
 * attach a skill: description, category, source (platform vs tenant) and the
 * plan-tier lock on platform skills the tenant is not entitled to. Tenant +
 * platform skills are merged (server-side name-shadowing already applied);
 * locked platform skills are shown but cannot be checked.
 *
 * Selected names that no longer resolve to a listed skill (e.g. hand-added in
 * raw YAML) are preserved as checked rows so a Form round-trip never drops
 * them. Emits the FULL merged manifest via the form_model writers.
 */
import {
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import {
  Checkbox,
  Empty,
  Input,
  Pagination,
  Select,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useTranslation } from "react-i18next";

import { listSkills, type SkillRecord } from "../../api/skills";
import { useTenantScope } from "../../tenant/TenantScopeContext";
import { FieldHelp } from "../FieldHelp";
import {
  readAutoAttachEvolvedSkills,
  readSkills,
  setAutoAttachEvolvedSkills,
  setSkills,
} from "./form_model";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };

// BUG-6:70+ 行全量平铺只靠内滚不可用 —— 客户端分页,同 SkillsList 的
// BUG-3 口径(20/页,单页自动隐藏)。
const PAGE_SIZE = 20;

function Heading({ children }: { children: ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

/** The fields the picker renders per skill — a synthetic option built from a
 *  ``SkillRecord`` or, for an unresolved selected name, a name-only stub. */
interface SkillOption {
  name: string;
  description?: string;
  category?: string;
  source?: "tenant" | "platform";
  /** ``false`` only for platform skills the tenant's tier cannot use. */
  locked: boolean;
  requiredTier?: string;
}

function toOption(rec: SkillRecord): SkillOption {
  return {
    name: rec.name,
    description: rec.description,
    category: rec.category,
    source: rec.source ?? "tenant",
    locked: rec.source === "platform" && rec.entitled === false,
    requiredTier: rec.required_tier,
  };
}

interface SkillPickerProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function SkillPicker({ formData, onChange }: SkillPickerProps) {
  const { t } = useTranslation();
  // Cross-tenant W3 — list the switched-in tenant's skills for the picker.
  const { apiTenantScope } = useTenantScope();
  const [skills, setSkillRecords] = useState<SkillRecord[]>([]);
  const [query, setQuery] = useState("");
  const [categoryFilter, setCategoryFilter] = useState<string | undefined>(
    undefined,
  );
  const [sourceFilter, setSourceFilter] = useState<
    "platform" | "tenant" | undefined
  >(undefined);
  const [page, setPage] = useState(1);

  useEffect(() => {
    let alive = true;
    listSkills({ tenantScope: apiTenantScope }).then(
      (s) => {
        if (!alive) return;
        setSkillRecords([...(s?.items ?? []), ...(s?.platform_items ?? [])]);
      },
      () => {},
    );
    return () => {
      alive = false;
    };
  }, [apiTenantScope]);

  const selected = readSkills(formData);

  // Merge listed skills with any selected name that didn't resolve to one, so
  // hand-authored refs survive and stay visibly checked.
  const options = useMemo<SkillOption[]>(() => {
    const byName = new Map<string, SkillOption>();
    for (const rec of skills) byName.set(rec.name, toOption(rec));
    for (const name of selected) {
      if (!byName.has(name)) byName.set(name, { name, locked: false });
    }
    return [...byName.values()];
  }, [skills, selected]);

  // Distinct categories from the loaded roster, feeding the category dropdown.
  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const o of options) if (o.category) set.add(o.category);
    return [...set].sort((a, b) => a.localeCompare(b));
  }, [options]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return options.filter((o) => {
      if (categoryFilter && o.category !== categoryFilter) return false;
      if (sourceFilter && (o.source ?? "tenant") !== sourceFilter) return false;
      if (
        q &&
        !o.name.toLowerCase().includes(q) &&
        !(o.description ?? "").toLowerCase().includes(q) &&
        !(o.category ?? "").toLowerCase().includes(q)
      ) {
        return false;
      }
      return true;
    });
  }, [options, query, categoryFilter, sourceFilter]);

  // Clamp rather than reset-on-change: a stale page > last page can only
  // happen when a filter shrank the list, and clamping keeps the "type a
  // query character" path from yanking the user off page 1 needlessly.
  const lastPage = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, lastPage);
  const paged = filtered.slice(
    (currentPage - 1) * PAGE_SIZE,
    currentPage * PAGE_SIZE,
  );

  const toggle = (name: string, on: boolean): void => {
    const next = on ? [...selected, name] : selected.filter((s) => s !== name);
    onChange(setSkills(formData, next));
  };

  return (
    <section data-testid="af-skills" style={SECTION}>
      <Heading>
        {t("agent_form.section_skills")}
        <FieldHelp
          text={t("agent_form.section_skills_help")}
          testId="af-skills"
        />
      </Heading>
      <Text type="secondary" style={{ display: "block", marginBottom: 8 }}>
        {t("agent_form.skills_hint")}
      </Text>

      {/* SE-16 (SE-A42) — evolution flywheel opt-in: build auto-attaches
          this agent's own ACTIVE distilled skills (lazy, summary only). */}
      <label
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 16,
        }}
        data-testid="af-auto-attach-evolved"
      >
        <Switch
          checked={readAutoAttachEvolvedSkills(formData)}
          onChange={(on) => onChange(setAutoAttachEvolvedSkills(formData, on))}
          aria-label={t("agent_form.auto_attach_evolved")}
          data-testid="af-auto-attach-evolved-switch"
        />
        <Text type="secondary">{t("agent_form.auto_attach_evolved")}</Text>
        <FieldHelp
          text={t("agent_form.auto_attach_evolved_help")}
          testId="af-auto-attach-evolved"
        />
      </label>

      {options.length > 6 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
            marginBottom: 12,
          }}
        >
          <Input
            allowClear
            style={{ flex: "1 1 200px", minWidth: 0 }}
            value={query}
            data-testid="af-skills-search"
            aria-label={t("agent_form.skills_search")}
            placeholder={t("agent_form.skills_search")}
            onChange={(e) => setQuery(e.target.value)}
          />
          <Select
            allowClear
            style={{ flex: "0 1 180px" }}
            value={categoryFilter}
            data-testid="af-skills-category"
            aria-label={t("agent_form.skills_filter_category")}
            placeholder={t("agent_form.skills_filter_category")}
            onChange={(v) => setCategoryFilter(v)}
            options={categories.map((c) => ({ value: c, label: c }))}
          />
          <Select
            allowClear
            style={{ flex: "0 1 150px" }}
            value={sourceFilter}
            data-testid="af-skills-source"
            aria-label={t("agent_form.skills_filter_source")}
            placeholder={t("agent_form.skills_filter_source")}
            onChange={(v) => setSourceFilter(v)}
            options={[
              {
                value: "platform",
                label: t("agent_form.skills_source_platform"),
              },
              { value: "tenant", label: t("agent_form.skills_source_tenant") },
            ]}
          />
        </div>
      )}

      <div
        data-testid="af-skills-scroll"
        // BUG-5:写死 480 让大屏在半屏留白下面透小窗看长列表 —— 上限跟随
        // 视口(减去页头/筛选行开销),480 仍是小屏地板。
        style={{
          maxHeight: "max(480px, calc(100vh - 420px))",
          overflowY: "auto",
        }}
      >
        <div data-testid="af-skills-list">
          {filtered.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={t("agent_form.skills_empty")}
            />
          ) : (
            paged.map((o) => {
              const checked = selected.includes(o.name);
              const row = (
                <div
                  key={o.name}
                  data-testid={`af-skill-row-${o.name}`}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: 10,
                    padding: "8px 0",
                    borderBottom:
                      "1px solid var(--ew-border, rgba(255,255,255,0.06))",
                    opacity: o.locked ? 0.55 : 1,
                  }}
                >
                  <Checkbox
                    checked={checked}
                    disabled={o.locked && !checked}
                    data-testid={`af-skill-check-${o.name}`}
                    aria-label={o.name}
                    onChange={(e) => toggle(o.name, e.target.checked)}
                    style={{ marginTop: 2 }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{ display: "flex", alignItems: "center", gap: 6 }}
                    >
                      <Text strong>{o.name}</Text>
                      <Tag
                        color={o.source === "platform" ? "purple" : "default"}
                        style={{ margin: 0 }}
                      >
                        {o.source === "platform"
                          ? t("agent_form.skills_source_platform")
                          : t("agent_form.skills_source_tenant")}
                      </Tag>
                      {o.category && (
                        <Tag color="cyan" style={{ margin: 0 }}>
                          {o.category}
                        </Tag>
                      )}
                      {o.locked && (
                        <Tag color="gold" style={{ margin: 0 }}>
                          {t("agent_form.skills_tier_locked", {
                            tier: o.requiredTier ?? "",
                          })}
                        </Tag>
                      )}
                    </div>
                    {o.description && (
                      <Text
                        type="secondary"
                        title={o.description}
                        // BUG-6:单行截断在窄列下几乎读不到内容 —— 两行
                        // clamp,悬停 title 看全文。
                        style={{
                          display: "-webkit-box",
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: "vertical",
                          fontSize: 13,
                          overflow: "hidden",
                        }}
                      >
                        {o.description}
                      </Text>
                    )}
                  </div>
                </div>
              );
              return o.locked ? (
                <Tooltip
                  key={o.name}
                  title={t("agent_form.skills_tier_locked_hint", {
                    tier: o.requiredTier ?? "",
                  })}
                >
                  {row}
                </Tooltip>
              ) : (
                row
              );
            })
          )}
        </div>
      </div>
      <Pagination
        size="small"
        current={currentPage}
        pageSize={PAGE_SIZE}
        total={filtered.length}
        onChange={setPage}
        showSizeChanger={false}
        hideOnSinglePage
        style={{ marginTop: 12, justifyContent: "flex-end", display: "flex" }}
        data-testid="af-skills-pagination"
      />
    </section>
  );
}
