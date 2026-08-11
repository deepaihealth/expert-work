/**
 * navModel unit tests — regression for a review finding (2026-08-11,
 * Task 2 `/handbook` follow-up): adding the always-visible "global" group
 * (the handbook) to ``visibleGroups`` must NOT change
 * ``defaultPathForScope``'s landing pick. Before the fix,
 * ``visibleGroups("*", false)`` returned ``["global"]`` (previously
 * ``[]``), so ``defaultPathForScope("*", false)`` resolved to
 * ``/handbook`` instead of its ``/agents`` fallback — a state genuinely
 * reachable via a stale sessionStorage ``"*"`` scope left over from a
 * prior admin session on a since-demoted/non-admin identity
 * (``TenantScopeContext``'s stored-scope read seeds ``useState`` directly,
 * bypassing the ``setScope`` gate).
 */
import { describe, expect, it } from "vitest";

import {
  defaultPathForScope,
  pathAllowedForScope,
  TENANT_SETTINGS_ITEMS,
  visibleGroups,
} from "../navModel";

describe("defaultPathForScope — 'global' must not affect the [0] pick", () => {
  it("platform scope, non-admin (no scope-specific group visible) falls back to /agents", () => {
    expect(defaultPathForScope("*", false)).toBe("/agents");
  });

  it("platform scope, system_admin still lands on the platform group's first entry", () => {
    expect(defaultPathForScope("*", true)).toBe("/settings/tenants");
  });

  it("a concrete tenant scope still lands on /agents", () => {
    expect(defaultPathForScope("some-tenant-id", false)).toBe("/agents");
  });
});

describe("pathAllowedForScope — /handbook stays reachable everywhere", () => {
  it("is allowed at platform scope even for a non-admin", () => {
    expect(pathAllowedForScope("/handbook", "*", false)).toBe(true);
  });

  it("is allowed at platform scope for a system_admin", () => {
    expect(pathAllowedForScope("/handbook", "*", true)).toBe(true);
  });

  it("is allowed at a concrete tenant scope", () => {
    expect(pathAllowedForScope("/handbook", "some-tenant-id", false)).toBe(true);
  });
});

describe("visibleGroups — 'global' is still appended for Sidebar/pathAllowedForScope", () => {
  it("platform scope, non-admin sees only the global group", () => {
    expect(visibleGroups("*", false)).toEqual(["global"]);
  });

  it("platform scope, system_admin sees platform + global", () => {
    expect(visibleGroups("*", true)).toEqual(["platform", "global"]);
  });
});

describe("tenant-quotas nav entry — regression for the missing sidebar entry", () => {
  it("lives in tenant-settings and is gated to tenant admins", () => {
    const entry = TENANT_SETTINGS_ITEMS.find(
      (e) => e.path === "/settings/tenant-quotas",
    );
    expect(entry).toMatchObject({
      key: "settings-tenant-quotas",
      labelKey: "nav.tenant_quotas",
      adminOnly: true,
    });
  });
});
