import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { useTenantScope } from "./tenant/TenantScopeContext";
import { defaultPathForScope } from "./components/navModel";
import { AgentsList } from "./pages/AgentsList";
import { ApprovalsList } from "./pages/ApprovalsList";
import { AgentDetail } from "./pages/AgentDetail";
import { ConversationDetail } from "./pages/ConversationDetail";
import { ConversationsList } from "./pages/ConversationsList";
import { Curation } from "./pages/Curation";
import { EvalRunDetail } from "./pages/EvalRunDetail";
import { EvalRunsList } from "./pages/EvalRunsList";
import { KnowledgeAdmin } from "./pages/KnowledgeAdmin";
import { KnowledgeDetail } from "./pages/KnowledgeDetail";
import { MemoryAdmin } from "./pages/MemoryAdmin";
import { RunDetail } from "./pages/RunDetail";
import { SettingsApiKeys } from "./pages/SettingsApiKeys";
import { SettingsAudit } from "./pages/SettingsAudit";
import { SettingsEgressAudit } from "./pages/SettingsEgressAudit";
import { SettingsMembers } from "./pages/SettingsMembers";
import { SettingsPlatformConfig } from "./pages/SettingsPlatformConfig";
import { SettingsPlatformUsers } from "./pages/SettingsPlatformUsers";
import { SettingsRoleBindings } from "./pages/SettingsRoleBindings";
import { SettingsServiceAccounts } from "./pages/SettingsServiceAccounts";
import { SettingsTenantConfig } from "./pages/SettingsTenantConfig";
import { SettingsTenantCredentials } from "./pages/SettingsTenantCredentials";
import { SettingsTenantQuotas } from "./pages/SettingsTenantQuotas";
import { SettingsTenants } from "./pages/SettingsTenants";
import { SettingsMcpServers } from "./pages/SettingsMcpServers";
import { SettingsMcpCatalog } from "./pages/SettingsMcpCatalog";
import { McpCatalogDetail } from "./pages/McpCatalogDetail";
import { SettingsAgentTemplates } from "./pages/SettingsAgentTemplates";
import { AgentTemplateDetail } from "./pages/AgentTemplateDetail";
import { AgentTemplateMarketplace } from "./pages/AgentTemplateMarketplace";
import { SettingsMcpOAuth } from "./pages/SettingsMcpOAuth";
import { McpOAuthCallback } from "./pages/McpOAuthCallback";
import { SettingsPlatformSkills } from "./pages/SettingsPlatformSkills";
import { SettingsPlatformSkillDetail } from "./pages/SettingsPlatformSkillDetail";
import { SettingsRateCard } from "./pages/SettingsRateCard";
import { SettingsUsage } from "./pages/SettingsUsage";
import { SettingsBillingChargeback } from "./pages/SettingsBillingChargeback";
import { SettingsObservability } from "./pages/SettingsObservability";
import { SettingsKeycloak } from "./pages/SettingsKeycloak";
import { SettingsQuality } from "./pages/SettingsQuality";
import { SkillDetail } from "./pages/SkillDetail";
import { UserDetail } from "./pages/UserDetail";
import { Users } from "./pages/Users";
import { UserProfile } from "./pages/UserProfile";
import { SkillsList } from "./pages/SkillsList";
import { TriggersList } from "./pages/TriggersList";
import { WebhooksList } from "./pages/WebhooksList";
import { Handbook } from "./pages/Handbook";
import { ComingSoon } from "./pages/ComingSoon";

/** Scope-aware landing for ``/`` — a system_admin at the platform level
 *  (``"*"``, incl. the sessionStorage-restored scope on re-login) lands on
 *  the platform group's first menu entry, everyone else on /agents. Keeps
 *  the root redirect consistent with the switcher/promotion landing rule. */
function HomeRedirect() {
  const { scope } = useTenantScope();
  const isSystemAdmin = useAuth().identity?.isSystemAdmin ?? false;
  return <Navigate to={defaultPathForScope(scope, isSystemAdmin)} replace />;
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/" element={<HomeRedirect />} />
      <Route path="/agents" element={<AgentsList />} />
      <Route path="/agents/:name/:version" element={<AgentDetail />} />
      {/* User detail before the generic :tab route — three segments
          (users/:userId) never collide, listed here for clarity. */}
      <Route path="/agents/:name/:version/users/:userId" element={<UserDetail />} />
      <Route path="/agents/:name/:version/:tab" element={<AgentDetail />} />
      {/* Old flat runs list — permanently replaced by the conversation
          browser; redirect keeps bookmarks / deep links working. */}
      <Route path="/runs" element={<Navigate to="/conversations" replace />} />
      <Route path="/approvals" element={<ApprovalsList />} />
      <Route path="/runs/:threadId/:runId" element={<RunDetail />} />
      <Route path="/conversations" element={<ConversationsList />} />
      <Route path="/conversations/:threadId" element={<ConversationDetail />} />
      {/* Top-level user-dimension observability (admin-only; the page
          self-guards + backend 403s). ``/users/:userId`` is the
          agent-agnostic user profile (distinct from the agent-scoped
          ``/agents/:name/:version/users/:userId``). */}
      <Route path="/users" element={<Users />} />
      <Route path="/users/:userId" element={<UserProfile />} />
      <Route path="/curation" element={<Curation />} />
      <Route path="/eval-runs" element={<EvalRunsList />} />
      <Route path="/eval-runs/:runId" element={<EvalRunDetail />} />
      <Route path="/memory" element={<MemoryAdmin />} />
      <Route path="/knowledge" element={<KnowledgeAdmin />} />
      <Route path="/knowledge/:name" element={<KnowledgeDetail />} />
      <Route path="/knowledge/:name/:tab" element={<KnowledgeDetail />} />
      <Route path="/skills" element={<SkillsList />} />
      <Route path="/skills/:skillId" element={<SkillDetail />} />
      <Route path="/agent-template-marketplace" element={<AgentTemplateMarketplace />} />
      <Route path="/triggers" element={<TriggersList />} />
      <Route path="/webhooks" element={<WebhooksList />} />
      {/* In-app handbook (spec 2026-08-11): usage docs for everyone, ops
          docs for system_admin only (gated inside the page — see Handbook.tsx).
          SPA route is ``/handbook``, distinct from the public ``/docs/``
          static site served by nginx — the two must never collide. */}
      <Route path="/handbook" element={<Handbook />} />
      <Route path="/handbook/:slug" element={<Handbook />} />
      <Route path="/settings/api-keys" element={<SettingsApiKeys />} />
      <Route
        path="/settings/service-accounts"
        element={<SettingsServiceAccounts />}
      />
      <Route
        path="/settings/role-bindings"
        element={<SettingsRoleBindings />}
      />
      <Route path="/settings/members" element={<SettingsMembers />} />
      <Route
        path="/settings/tenant-quotas"
        element={<SettingsTenantQuotas />}
      />
      <Route
        path="/settings/tenant-config"
        element={<SettingsTenantConfig />}
      />
      <Route path="/settings/tenants" element={<SettingsTenants />} />
      <Route
        path="/settings/credentials"
        element={<SettingsTenantCredentials />}
      />
      <Route path="/settings/platform" element={<SettingsPlatformConfig />} />
      <Route
        path="/settings/platform-users"
        element={<SettingsPlatformUsers />}
      />
      <Route path="/settings/mcp-catalog" element={<SettingsMcpCatalog />} />
      <Route
        path="/settings/mcp-catalog/:catalogId"
        element={<McpCatalogDetail />}
      />
      <Route
        path="/settings/agent-templates"
        element={<SettingsAgentTemplates />}
      />
      <Route
        path="/settings/agent-templates/:name/:version"
        element={<AgentTemplateDetail />}
      />
      <Route
        path="/settings/platform-skills"
        element={<SettingsPlatformSkills />}
      />
      <Route
        path="/settings/platform-skills/:skillId"
        element={<SettingsPlatformSkillDetail />}
      />
      <Route path="/settings/audit" element={<SettingsAudit />} />
      <Route path="/settings/egress-audit" element={<SettingsEgressAudit />} />
      <Route path="/settings/mcp-servers" element={<SettingsMcpServers />} />
      <Route path="/settings/mcp-oauth" element={<SettingsMcpOAuth />} />
      <Route
        path="/settings/mcp-oauth/callback"
        element={<McpOAuthCallback />}
      />
      <Route path="/settings/usage" element={<SettingsUsage />} />
      <Route
        path="/settings/billing-chargeback"
        element={<SettingsBillingChargeback />}
      />
      <Route path="/settings/rate-card" element={<SettingsRateCard />} />
      <Route path="/settings/observability" element={<SettingsObservability />} />
      <Route path="/settings/keycloak" element={<SettingsKeycloak />} />
      <Route path="/settings/quality" element={<SettingsQuality />} />
      <Route path="/settings/*" element={<ComingSoon title="Settings" />} />
      <Route path="*" element={<ComingSoon title="404" />} />
    </Routes>
  );
}
