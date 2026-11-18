/**
 * useMemberNames tests — B-316.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { ApiError, type TenantScope } from "../../api/client";
import * as membersSdk from "../../api/members";
import type { MemberList, TenantMember } from "../../api/members";
import { useAuth } from "../../auth/AuthContext";
import { MAX_MEMBER_PAGES, MEMBER_PAGE_SIZE, useMemberNames } from "../useMemberNames";

vi.mock("../../auth/AuthContext", () => ({ useAuth: vi.fn() }));

function signInAs(roles: string[], isSystemAdmin = false): void {
  vi.mocked(useAuth).mockReturnValue({
    identity: { roles, isSystemAdmin },
  } as unknown as ReturnType<typeof useAuth>);
}

function member(n: number, overrides: Partial<TenantMember> = {}): TenantMember {
  const hex = n.toString(16).padStart(4, "0");
  return {
    id: `00000000-0000-4000-8000-00000000${hex}`,
    tenant_id: "22222222-2222-2222-2222-222222222222",
    email: `m${n}@acme.com`,
    display_name: `成员${n}`,
    role: "operator",
    status: "active",
    keycloak_user_id: `11111111-0000-4000-8000-00000000${hex}`,
    subject_id: `33333333-0000-4000-8000-00000000${hex}`,
    invited_by: "seed",
    invited_at: null,
    activated_at: null,
    updated_at: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

const fullPage = (): MemberList => ({
  items: Array.from({ length: MEMBER_PAGE_SIZE }, (_, i) => member(i)),
  total: MEMBER_PAGE_SIZE,
});

beforeEach(() => signInAs(["admin"]));
afterEach(() => vi.restoreAllMocks());

describe("useMemberNames", () => {
  it("maps keycloak_user_id, subject_id and the member id to the display name", async () => {
    const m = member(1);
    vi.spyOn(membersSdk, "listMembers").mockResolvedValue({ items: [m], total: 1 });

    const { result } = renderHook(() => useMemberNames(undefined));

    await waitFor(() => expect(result.current(m.keycloak_user_id!)).toBe("成员1"));
    expect(result.current(m.subject_id!)).toBe("成员1");
    expect(result.current(m.id)).toBe("成员1");
  });

  it("falls back to the email when display_name is empty", async () => {
    const m = member(2, { display_name: null });
    vi.spyOn(membersSdk, "listMembers").mockResolvedValue({ items: [m], total: 1 });

    const { result } = renderHook(() => useMemberNames(undefined));

    await waitFor(() => expect(result.current(m.keycloak_user_id!)).toBe("m2@acme.com"));
  });

  it("returns unknown ids (service accounts, system actors) unchanged", async () => {
    const m = member(3);
    vi.spyOn(membersSdk, "listMembers").mockResolvedValue({ items: [m], total: 1 });

    const { result } = renderHook(() => useMemberNames(undefined));

    await waitFor(() => expect(result.current(m.id)).toBe("成员3"));
    expect(result.current("seed-canary")).toBe("seed-canary");
    // Not an Object.prototype lookup.
    expect(result.current("constructor")).toBe("constructor");
  });

  it("passes the tenant scope through and pages past the 200-member limit", async () => {
    const page1 = fullPage();
    const last = member(MEMBER_PAGE_SIZE);
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValueOnce(page1)
      .mockResolvedValueOnce({ items: [last], total: 1 });

    const { result } = renderHook(() => useMemberNames("*"));

    await waitFor(() =>
      expect(result.current(last.keycloak_user_id!)).toBe(`成员${MEMBER_PAGE_SIZE}`),
    );
    expect(result.current(page1.items[0].keycloak_user_id!)).toBe("成员0");
    expect(listMembers).toHaveBeenCalledTimes(2);
    expect(listMembers).toHaveBeenNthCalledWith(1, {
      tenantScope: "*",
      limit: MEMBER_PAGE_SIZE,
      offset: 0,
    });
    expect(listMembers).toHaveBeenNthCalledWith(2, {
      tenantScope: "*",
      limit: MEMBER_PAGE_SIZE,
      offset: MEMBER_PAGE_SIZE,
    });
  });

  it("stops after MAX_MEMBER_PAGES when every page comes back full", async () => {
    const listMembers = vi.spyOn(membersSdk, "listMembers").mockImplementation(async () => fullPage());

    const { result } = renderHook(() => useMemberNames(undefined));

    await waitFor(() => expect(result.current(member(0).id)).toBe("成员0"));
    expect(MAX_MEMBER_PAGES).toBe(50);
    expect(listMembers).toHaveBeenCalledTimes(MAX_MEMBER_PAGES);
  });

  it("fetches once per scope, not once per render", async () => {
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValue({ items: [member(5)], total: 1 });

    const { result, rerender } = renderHook(() => useMemberNames(undefined));
    await waitFor(() => expect(result.current(member(5).id)).toBe("成员5"));
    rerender();
    rerender();

    expect(listMembers).toHaveBeenCalledTimes(1);
  });

  it("refetches for a new scope", async () => {
    const tenantA = member(6);
    const tenantB = member(7);
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockImplementation(async ({ tenantScope } = {}) => ({
        items: [tenantScope === "tenant-b" ? tenantB : tenantA],
        total: 1,
      }));

    const { result, rerender } = renderHook(({ scope }) => useMemberNames(scope), {
      initialProps: { scope: "tenant-a" as TenantScope },
    });
    await waitFor(() => expect(result.current(tenantA.id)).toBe("成员6"));

    rerender({ scope: "tenant-b" });

    await waitFor(() => expect(result.current(tenantB.id)).toBe("成员7"));
    expect(listMembers).toHaveBeenLastCalledWith(
      expect.objectContaining({ tenantScope: "tenant-b", offset: 0 }),
    );
  });

  it("drops the previous scope's names when the new scope's fetch fails", async () => {
    const m = member(8);
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValueOnce({ items: [m], total: 1 })
      .mockRejectedValueOnce(new ApiError("nope", "FORBIDDEN", 403));

    const { result, rerender } = renderHook(({ scope }) => useMemberNames(scope), {
      initialProps: { scope: "tenant-a" as TenantScope },
    });
    await waitFor(() => expect(result.current(m.id)).toBe("成员8"));

    rerender({ scope: "tenant-b" });

    await waitFor(() => expect(listMembers).toHaveBeenCalledTimes(2));
    expect(result.current(m.id)).toBe(m.id);
    // Still raw once the rejection has settled — no stale name comes back.
    await new Promise((r) => setTimeout(r, 0));
    expect(result.current(m.id)).toBe(m.id);
  });

  it("ignores a stale fetch that lands after the scope changed", async () => {
    const stale = member(9);
    const firstPage = deferred<MemberList>();
    vi.spyOn(membersSdk, "listMembers")
      .mockReturnValueOnce(firstPage.promise)
      .mockResolvedValueOnce({ items: [], total: 0 });

    const { result, rerender } = renderHook(({ scope }) => useMemberNames(scope), {
      initialProps: { scope: "tenant-a" as TenantScope },
    });
    rerender({ scope: "tenant-b" });
    firstPage.resolve({ items: [stale], total: 1 });
    await new Promise((r) => setTimeout(r, 0));

    expect(result.current(stale.id)).toBe(stale.id);
  });

  it("stops paging once unmounted", async () => {
    const firstPage = deferred<MemberList>();
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockReturnValueOnce(firstPage.promise)
      .mockImplementation(async () => fullPage());

    const { unmount } = renderHook(() => useMemberNames(undefined));
    await waitFor(() => expect(listMembers).toHaveBeenCalledTimes(1));
    unmount();
    firstPage.resolve(fullPage());
    await new Promise((r) => setTimeout(r, 0));

    // A full first page would normally fetch page 2; cancelled, it does not.
    expect(listMembers).toHaveBeenCalledTimes(1);
  });

  it("does not call listMembers for an operator — ids stay raw", async () => {
    signInAs(["operator"]);
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValue({ items: [member(10)], total: 1 });

    const { result } = renderHook(() => useMemberNames(undefined));
    await new Promise((r) => setTimeout(r, 0));

    expect(listMembers).not.toHaveBeenCalled();
    expect(result.current(member(10).id)).toBe(member(10).id);
  });

  it("calls listMembers for an admin, and for a system admin without a tenant admin role", async () => {
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValue({ items: [member(11)], total: 1 });

    const admin = renderHook(() => useMemberNames(undefined));
    await waitFor(() => expect(admin.result.current(member(11).id)).toBe("成员11"));
    expect(listMembers).toHaveBeenCalledTimes(1);
    admin.unmount();

    signInAs(["viewer"], true);
    const sysAdmin = renderHook(() => useMemberNames(undefined));
    await waitFor(() => expect(sysAdmin.result.current(member(11).id)).toBe("成员11"));
    expect(listMembers).toHaveBeenCalledTimes(2);
  });
});
