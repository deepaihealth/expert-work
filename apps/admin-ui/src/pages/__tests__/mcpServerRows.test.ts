import { describe, expect, it } from "vitest";
import type { AvailableMcpServer, McpServer } from "../../api/mcp-servers";
import { buildUnifiedRows } from "../mcpServerRows";

const custom: McpServer = {
  id: "s1",
  name: "my-custom",
  transport: "sse",
  url: "https://x.example.com/sse",
  auth_type: "bearer",
  timeout_s: 30,
  enabled: true,
  created_at: "",
  updated_at: "",
};

describe("buildUnifiedRows", () => {
  it("maps enriched platform rows and puts them before custom rows", () => {
    const available: AvailableMcpServer[] = [
      {
        name: "amap-maps",
        source: "platform",
        display_name: "高德地图",
        auth_type: "oauth2",
        catalog_id: "c1",
      },
    ];
    const rows = buildUnifiedRows([custom], available);
    expect(rows).toHaveLength(2);
    const [p, c] = rows;
    expect(p.source).toBe("platform");
    if (p.source === "platform") {
      expect(p.displayName).toBe("高德地图");
      expect(p.authType).toBe("oauth2");
      expect(p.catalogId).toBe("c1");
      expect(p.key).toBe("platform:amap-maps");
    }
    expect(c.source).toBe("tenant");
    if (c.source === "tenant") {
      expect(c.server.name).toBe("my-custom");
      expect(c.key).toBe("tenant:home:my-custom");
    }
  });

  // Cross-tenant W4 — the "*" aggregate may carry a same-named server from
  // two tenants; the key must include the owning tenant so rows don't collide.
  it("keys same-named custom rows from two tenants distinctly", () => {
    const rows = buildUnifiedRows(
      [
        { ...custom, tenant_id: "t1" },
        { ...custom, id: "s2", tenant_id: "t2" },
      ],
      [],
    );
    expect(rows).toHaveLength(2);
    expect(rows[0].key).toBe("tenant:t1:my-custom");
    expect(rows[1].key).toBe("tenant:t2:my-custom");
    expect(new Set(rows.map((r) => r.key)).size).toBe(2);
  });

  it("degrades a platform row missing enrichment (stale allowlist)", () => {
    const rows = buildUnifiedRows(
      [],
      [{ name: "ghost", source: "platform" }],
    );
    expect(rows).toHaveLength(1);
    const [p] = rows;
    if (p.source === "platform") {
      expect(p.displayName).toBe("ghost");
      expect(p.authType).toBe("none");
      expect(p.catalogId).toBeNull();
    }
  });

  it("ignores source==='tenant' rows from available (servers is authoritative)", () => {
    const rows = buildUnifiedRows(
      [custom],
      [{ name: "my-custom", source: "tenant", enabled: true }],
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].source).toBe("tenant");
  });
});
