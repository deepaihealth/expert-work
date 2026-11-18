/**
 * useMemberNames tests — B-316.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

import { ApiError } from "../../api/client";
import * as membersSdk from "../../api/members";
import type { TenantMember } from "../../api/members";
import { MEMBER_PAGE_SIZE, useMemberNames } from "../useMemberNames";

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

  it("degrades to the raw id when the member list cannot be read", async () => {
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockRejectedValue(new ApiError("nope", "FORBIDDEN", 403));
    const id = member(4).keycloak_user_id!;

    const { result } = renderHook(() => useMemberNames(undefined));

    await waitFor(() => expect(listMembers).toHaveBeenCalledTimes(1));
    // Let the rejection settle before asserting nothing changed.
    await Promise.resolve();
    expect(result.current(id)).toBe(id);
  });

  it("passes the tenant scope through and pages past the 200-member limit", async () => {
    const firstPage = Array.from({ length: MEMBER_PAGE_SIZE }, (_, i) => member(i));
    const last = member(MEMBER_PAGE_SIZE);
    const listMembers = vi
      .spyOn(membersSdk, "listMembers")
      .mockResolvedValueOnce({ items: firstPage, total: MEMBER_PAGE_SIZE })
      .mockResolvedValueOnce({ items: [last], total: 1 });

    const { result } = renderHook(() => useMemberNames("*"));

    await waitFor(() => expect(result.current(last.keycloak_user_id!)).toBe(`成员${MEMBER_PAGE_SIZE}`));
    expect(result.current(firstPage[0].keycloak_user_id!)).toBe("成员0");
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
});
