/**
 * In-app handbook — ``/handbook`` and ``/handbook/:slug`` (spec
 * 2026-08-11-docs-site §4).
 *
 * Two doc groups bundled at build time (``docs/loader.ts``): usage docs
 * (``tenant/*.md``) visible to every signed-in member, and ops docs
 * (``ops/*.md``) visible only to ``system_admin``. Gated three ways, per
 * the spec (belt-and-suspenders, same ``isSystemAdmin`` source as every
 * other admin-only page — see ``navModel.ts`` / ``SettingsTenants.tsx``):
 *
 *   1. the sidebar entry (``navModel`` "global" group) is always visible —
 *      the role split lives *inside* this page, not in nav visibility;
 *   2. the ops menu group below only renders for a system_admin;
 *   3. an ops slug only *resolves* for a system_admin — a non-admin who
 *      deep-links one gets the same 404 empty state as a bogus slug
 *      (never a redirect, so the page can't be used to probe which ops
 *      slugs exist).
 */
import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Card, Empty, Menu } from "antd";
import { BookMarked, FileQuestion } from "lucide-react";
import { useTranslation } from "react-i18next";

import { PageHeader } from "../components/PageHeader";
import { MarkdownView } from "../components/MarkdownView";
import { useAuth } from "../auth/AuthContext";
import { loadDocs, type DocEntry } from "../docs/loader";

export function Handbook() {
  const { t } = useTranslation();
  const { slug } = useParams<{ slug?: string }>();
  const navigate = useNavigate();
  const isSystemAdmin = useAuth().identity?.isSystemAdmin ?? false;

  const docs = useMemo(() => loadDocs(), []);

  const active: DocEntry | undefined = useMemo(() => {
    if (!slug) return docs.tenant[0];
    const tenantMatch = docs.tenant.find((d) => d.slug === slug);
    if (tenantMatch) return tenantMatch;
    const opsMatch = docs.ops.find((d) => d.slug === slug);
    // An ops doc only resolves for a system_admin — a non-admin deep-linking
    // an ops slug falls through to the same "not found" outcome as a
    // genuinely unknown slug.
    return opsMatch && isSystemAdmin ? opsMatch : undefined;
  }, [slug, docs, isSystemAdmin]);

  if (slug && !active) {
    return (
      <div data-testid="handbook-root">
        <PageHeader
          icon={<BookMarked size={18} strokeWidth={1.5} />}
          title={t("nav.handbook")}
        />
        <Empty
          image={
            <FileQuestion
              size={48}
              strokeWidth={1.5}
              style={{ color: "var(--ew-text-tertiary)", margin: "0 auto" }}
            />
          }
          description={
            <>
              <div style={{ fontSize: 14, color: "var(--ew-text-primary)", marginBottom: 4 }}>
                {t("handbook.not_found_title")}
              </div>
              <div style={{ fontSize: 13, color: "var(--ew-text-tertiary)" }}>
                {t("handbook.not_found_body")}
              </div>
            </>
          }
          style={{ padding: "80px 24px" }}
          data-testid="handbook-not-found"
        />
      </div>
    );
  }

  const menuItems = [
    {
      key: "tenant-group",
      label: t("handbook.group_tenant"),
      type: "group" as const,
      children: docs.tenant.map((d) => ({ key: d.slug, label: d.title })),
    },
    ...(isSystemAdmin
      ? [
          {
            key: "ops-group",
            label: t("handbook.group_ops"),
            type: "group" as const,
            children: docs.ops.map((d) => ({ key: d.slug, label: d.title })),
          },
        ]
      : []),
  ];

  return (
    <div data-testid="handbook-root">
      <PageHeader
        icon={<BookMarked size={18} strokeWidth={1.5} />}
        title={t("nav.handbook")}
      />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "240px 1fr",
          gap: 16,
          alignItems: "start",
        }}
      >
        <Card size="small" styles={{ body: { padding: 0 } }} data-testid="handbook-menu">
          <Menu
            mode="inline"
            selectedKeys={active ? [active.slug] : []}
            items={menuItems}
            onClick={({ key }) => navigate(`/handbook/${key}`)}
            style={{ border: "none" }}
          />
        </Card>
        <Card data-testid="handbook-content">
          {active ? (
            <>
              <h2 style={{ marginTop: 0 }}>{active.title}</h2>
              <MarkdownView>{active.body}</MarkdownView>
            </>
          ) : (
            <Empty description={t("handbook.empty")} />
          )}
        </Card>
      </div>
    </div>
  );
}
