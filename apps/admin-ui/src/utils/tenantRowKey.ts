/**
 * Shared table row-key for cross-tenant aggregate views — Cross-tenant W4.
 *
 * In the ``tenant_id=*`` aggregate two tenants may legally own a same-named
 * resource (base / usage key / agent …), so a bare-name row key collides.
 * The key therefore always leads with the owning tenant; ``null`` /
 * ``undefined`` (single-tenant view, or a pre-W4 backend that omits
 * ``tenant_id``) collapse to a fixed ``"home"`` bucket.
 *
 * Pure function (照 ``mcpServerRows.buildUnifiedRows`` 先例) so the key
 * construction is unit-testable — the mutation "退回裸 name/key" must turn
 * ``tenantRowKey.test.ts`` red.
 */
export function tenantRowKey(
  tenantId: string | null | undefined,
  ...parts: readonly string[]
): string {
  return [tenantId ?? "home", ...parts].join(":");
}
