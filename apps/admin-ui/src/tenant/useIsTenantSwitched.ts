/**
 * 切入态判定 — Track C W2(跨租户只读一期)。
 *
 * 系统管理员在租户切换器切入某个具体租户后,详情页进入「只读视角」:
 * 读走 ``?tenant_id=``,写操作一律置灰。本 hook 是那个统一判定。
 */
import { useAuth } from "../auth/AuthContext";
import { SCOPE_ALL, SCOPE_HOME, useTenantScope } from "./TenantScopeContext";

/** 切入态 = scope 为具体租户 UUID 且 ≠ 归属租户。"*" 聚合与 home 都不算。 */
export function useIsTenantSwitched(): boolean {
  const { scope } = useTenantScope();
  const { identity } = useAuth();
  return scope !== SCOPE_HOME && scope !== SCOPE_ALL && scope !== identity?.homeTenantId;
}
