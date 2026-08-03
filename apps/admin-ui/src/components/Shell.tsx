import { useEffect, useRef, type ReactNode } from "react";
import { Layout } from "antd";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { SCOPE_ALL, useTenantScope } from "../tenant/TenantScopeContext";
import {
  defaultPathForScope,
  groupForPath,
  isPlatformScope,
  pathAllowedForScope,
} from "./navModel";

const { Sider, Header, Content } = Layout;

/**
 * Enter the platform level when a system_admin deep-links a platform page
 * (§4, deep-link friendly). Minimal on purpose:
 *
 *   - platform route + system_admin not yet at platform level → switch up
 *     to ``"*"`` and stay on the page (bookmark / direct link just works,
 *     and the sidebar swaps to the platform group).
 *
 * Everything else is left to the pages themselves: non-admins on a platform
 * route get the page's own system-admin-only notice (no bounce); pages that
 * adapt to scope (e.g. cross-tenant Members at ``"*"``) keep whatever scope
 * the switcher set. No tenant-route force-switch — it would clobber those.
 */
function useScopeRedirect(): void {
  const { scope, setScope } = useTenantScope();
  const isSystemAdmin = useAuth().identity?.isSystemAdmin ?? false;
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (
      groupForPath(location.pathname) === "platform" &&
      isSystemAdmin &&
      !isPlatformScope(scope)
    ) {
      setScope(SCOPE_ALL);
    }
  }, [scope, isSystemAdmin, location.pathname, setScope]);

  // Scope changed underneath the current page — the context auto-promotes a
  // platform-homed system_admin to ``"*"`` after /v1/me (login lands on
  // /agents with a platform sidebar otherwise), and demotes a stale non-admin
  // scope. When the page is no longer part of the new scope's nav, land on
  // the new scope's first menu entry, same as a switcher change. A page that
  // IS part of it stays put (deep-link promotion above; ``"*"``-adaptive
  // pages like the cross-tenant Members overview). Transition-gated on
  // purpose: mounting straight onto e.g. /knowledge under ``"*"`` (aggregate
  // view via URL/palette) must not bounce.
  const prevScope = useRef(scope);
  useEffect(() => {
    if (prevScope.current === scope) return;
    prevScope.current = scope;
    if (!pathAllowedForScope(location.pathname, scope, isSystemAdmin)) {
      navigate(defaultPathForScope(scope, isSystemAdmin), { replace: true });
    }
  }, [scope, isSystemAdmin, location.pathname, navigate]);
}

export function Shell({ children }: { children: ReactNode }) {
  useScopeRedirect();
  return (
    <Layout style={{ minHeight: "100vh" }}>
      <Sider
        width={220}
        style={{
          borderRight: "1px solid var(--ew-border-subtle)",
        }}
      >
        <Sidebar />
      </Sider>
      <Layout>
        <Header
          style={{
            borderBottom: "1px solid var(--ew-border-subtle)",
            display: "flex",
            alignItems: "center",
            gap: 16,
            padding: "0 24px",
          }}
        >
          <Topbar />
        </Header>
        <Content style={{ padding: "24px 32px", overflow: "auto" }}>
          {children}
        </Content>
      </Layout>
    </Layout>
  );
}
