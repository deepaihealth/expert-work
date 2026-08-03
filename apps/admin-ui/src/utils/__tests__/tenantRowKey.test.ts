/**
 * tenantRowKey 单测 — Cross-tenant W4(review IMPORTANT-3)。
 *
 * 变异靶(MUT-7/8):把 key 构造退回裸 name/key(丢租户维度)或丢资源名,
 * 这里必须红。KnowledgeAdmin / SettingsUsage(三表)/ SettingsQuality 的
 * rowKey 都走这一个纯函数。
 */
import { describe, expect, it } from "vitest";

import { tenantRowKey } from "../tenantRowKey";

describe("tenantRowKey", () => {
  it("keys same-named resources from two tenants distinctly (裸 name 变异 → 红)", () => {
    const a = tenantRowKey("tenant-1", "support-docs");
    const b = tenantRowKey("tenant-2", "support-docs");
    expect(a).not.toBe(b);
    // 键必须真带租户维度,不是碰巧不同。
    expect(a).toBe("tenant-1:support-docs");
    expect(b).toBe("tenant-2:support-docs");
  });

  it("keys same-tenant resources by name (裸 tenant 变异 → 红)", () => {
    expect(tenantRowKey("tenant-1", "base-a")).not.toBe(tenantRowKey("tenant-1", "base-b"));
  });

  it("collapses null/undefined tenant to the fixed home bucket", () => {
    expect(tenantRowKey(null, "support-docs")).toBe("home:support-docs");
    expect(tenantRowKey(undefined, "support-docs")).toBe("home:support-docs");
    // home 桶与真实租户的键不撞。
    expect(tenantRowKey(undefined, "support-docs")).not.toBe(
      tenantRowKey("tenant-1", "support-docs"),
    );
  });

  it("supports multi-part keys", () => {
    expect(tenantRowKey("t1", "agent", "model")).toBe("t1:agent:model");
  });
});
