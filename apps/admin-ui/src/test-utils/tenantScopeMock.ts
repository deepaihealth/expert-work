/**
 * Shared tenant-scope mock factory — Cross-tenant W3 (Track F3).
 *
 * Nearly every page/component test repeats the same ``vi.mock`` boilerplate
 * for ``tenant/TenantScopeContext``: spread the real module (keeping
 * ``concreteTenantScope`` / ``SCOPE_ALL`` … authentic) and override
 * ``useTenantScope`` with a switchable scope. This module centralizes the
 * override construction; the ``vi.mock`` call itself stays per-file
 * (vitest hoists it), referencing only a ``vi.hoisted`` ref:
 *
 * ```ts
 * const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
 * vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
 *   const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
 *   return mockTenantScopeModule(
 *     await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
 *     scopeRef,
 *   );
 * });
 * ```
 *
 * ``ref.current === undefined`` models the home state (scope ``"home"``,
 * nothing on the wire); a UUID or ``"*"`` models switched/aggregate views.
 * Flip ``scopeRef.current`` per test; reset in ``beforeEach``/``afterEach``.
 */
import type * as TenantScopeModule from "../tenant/TenantScopeContext";

/** Mutable scope holder — create with ``vi.hoisted`` so the ``vi.mock``
 *  factory may legally reference it. */
export interface TenantScopeRef {
  current: string | undefined;
}

/** The ``useTenantScope()`` return value for the ref's current scope. */
export function tenantScopeValue(
  ref: TenantScopeRef,
): ReturnType<typeof TenantScopeModule.useTenantScope> {
  return {
    scope: ref.current ?? "home",
    setScope: () => {},
    apiTenantScope: ref.current,
  };
}

/** Build the mocked module: real exports spread + ``useTenantScope``
 *  reading the ref lazily (per render, so per-test flips take effect). */
export function mockTenantScopeModule(
  original: typeof TenantScopeModule,
  ref: TenantScopeRef,
): typeof TenantScopeModule {
  return {
    ...original,
    useTenantScope: () => tenantScopeValue(ref),
  };
}
