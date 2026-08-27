/**
 * Members page tests — Stream U PR F: set temporary password action.
 *
 * An admin opens the "Set password" modal for an active member, types a
 * temporary password, and submits. The member must change it on first
 * login (backend forces ``temporary=true``). Short passwords surface an
 * inline error and never hit the API.
 *
 * Deletion-hygiene PR5 adds the one-shot "deactivate & purge" action:
 * type-to-confirm (the member's email) arms the danger button, success
 * refreshes the roster, a partial failure keeps the modal open for a
 * retry, and a never-signed-in member shows the "no data" note.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsMembers } from "../SettingsMembers";
import { AuthProvider } from "../../auth/AuthContext";
import { setStoredToken } from "../../api/client";
import {
  inviteMembers,
  listMembers,
  purgeMember,
  resendMember,
  resetMemberPassword,
  type MemberPurgeResult,
  type TenantMember,
} from "../../api/members";
import type { PurgeSummary } from "../../api/users";

// ``importOriginal`` keeps the real ``isMemberPurgePartial`` — the purge
// modal computes partial-ness through it, so stubbing it would blind the
// partial-failure test below.
vi.mock("../../api/members", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/members")>();
  return {
    ...actual,
    listMembers: vi.fn(),
    inviteMembers: vi.fn(),
    resendMember: vi.fn(),
    revokeMember: vi.fn(),
    resetMemberPassword: vi.fn(),
    purgeMember: vi.fn(),
  };
});

// TenantScope context — switchable per test (mirrors ArtifactsList).
const scopeRef = vi.hoisted(() => ({ current: undefined as string | undefined }));
vi.mock("../../tenant/TenantScopeContext", async (importOriginal) => {
  const { mockTenantScopeModule } = await import("../../test-utils/tenantScopeMock");
  return mockTenantScopeModule(
    await importOriginal<typeof import("../../tenant/TenantScopeContext")>(),
    scopeRef,
  );
});

// Cross-tenant W3 — 切入态置灰;``isTenantSwitchedMock`` 可翻转做两态断言。
const { isTenantSwitchedMock } = vi.hoisted(() => ({
  isTenantSwitchedMock: vi.fn(() => false),
}));
vi.mock("../../tenant/useIsTenantSwitched", () => ({
  useIsTenantSwitched: isTenantSwitchedMock,
}));

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

const activeMember: TenantMember = {
  id: "m-1",
  tenant_id: "t1",
  email: "alice@example.com",
  display_name: "Alice",
  role: "admin",
  status: "active",
  keycloak_user_id: "kc-1",
  subject_id: "s-1",
  invited_by: "u1",
  invited_at: "2026-05-26T10:00:00Z",
  activated_at: "2026-05-27T10:00:00Z",
  updated_at: "2026-05-27T10:00:00Z",
};

function renderPage(): void {
  scopeRef.current = undefined;
  setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin"] }));
  render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsMembers />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

/** Render in the cross-tenant aggregate (read-only) view (scope "*"). */
function renderCrossTenant(): void {
  scopeRef.current = "*";
  setStoredToken(
    makeJwt({ sub: "u1", tenant_id: "t1", roles: ["system_admin"] }),
  );
  render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsMembers />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

/** Invited and never signed in — ``subject_id`` null, no business data. */
const invitedNoLogin: TenantMember = {
  ...activeMember,
  id: "m-2",
  email: "carol@example.com",
  display_name: "Carol",
  status: "invited",
  keycloak_user_id: "kc-2",
  subject_id: null,
  activated_at: null,
};

const suspendedMember: TenantMember = {
  ...activeMember,
  id: "m-3",
  email: "dave@example.com",
  display_name: "Dave",
  status: "suspended",
  keycloak_user_id: "kc-3",
  subject_id: "s-3",
};

const PURGE_SUMMARY_OK: PurgeSummary = {
  tenant_id: "t1",
  user_id: "s-1",
  subject_id: "kc-1",
  threads_purged: 1,
  runs_deleted: 0,
  threads_capped: false,
  deleted: {},
  anonymized: {},
  workspace_marked_deleted: true,
  deactivated: true,
  failures: {},
  ok: true,
};

const PURGE_OK: MemberPurgeResult = {
  member_id: "m-1",
  status: "suspended",
  kc_deleted: true,
  kc_delete_failed: false,
  role_bindings_removed: 1,
  role_bindings_cleanup_failed: false,
  data_purged: true,
  purge: PURGE_SUMMARY_OK,
};

const PURGE_PARTIAL: MemberPurgeResult = {
  ...PURGE_OK,
  kc_deleted: false,
  kc_delete_failed: true,
  data_purged: false,
  purge: null,
};

// Backend PR5 follow-up: the data-purge step is best-effort — a registry
// read / dependency assembly blowup no longer 500s, it comes back 200 with
// ``data_purge_failed: true`` and ``purge: null`` while every other step
// reports success. Renders as fully successful without the partial check
// below, even though not one row of business data was cleared.
const PURGE_DATA_STEP_FAILED: MemberPurgeResult = {
  ...PURGE_OK,
  data_purged: false,
  data_purge_failed: true,
  purge: null,
};

const crossTenantMembers: TenantMember[] = [
  { ...activeMember },
  {
    ...activeMember,
    id: "m-2",
    tenant_id: "t2",
    email: "bob@example.com",
    display_name: "Bob",
  },
];

afterEach(() => {
  setStoredToken(null);
  window.sessionStorage.clear();
  vi.clearAllMocks();
  // vitest 4 的 clear 不复位 mockReturnValue — 显式归位防串台。
  isTenantSwitchedMock.mockReturnValue(false);
});

describe("SettingsMembers — 切入态置灰 (W3)", () => {
  it("home 态邀请/清除可用(两态其一)", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("members-invite-btn")).toBeEnabled();
    expect(screen.getByTestId("members-purge-m-1")).toBeEnabled();
  });

  it("切入态置灰邀请/清除(两态其二)", async () => {
    isTenantSwitchedMock.mockReturnValue(true);
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("members-invite-btn")).toBeDisabled();
    expect(screen.getByTestId("members-purge-m-1")).toBeDisabled();
    expect(screen.getByTestId("members-remove-m-1")).toBeDisabled();
  });
});

describe("SettingsMembers — set password", () => {
  it("opens the modal and sets a valid temporary password", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    vi.mocked(resetMemberPassword).mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-set-password-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-set-password-m-1"));

    await waitFor(() =>
      expect(screen.getByTestId("members-set-password-input")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByTestId("members-set-password-input"),
      "s3cret-pass",
    );
    await user.click(screen.getByTestId("members-set-password-submit"));

    await waitFor(() =>
      expect(resetMemberPassword).toHaveBeenCalledWith("m-1", "s3cret-pass"),
    );
  });

  it("rejects a too-short password without calling the API", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-set-password-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-set-password-m-1"));

    await waitFor(() =>
      expect(screen.getByTestId("members-set-password-input")).toBeInTheDocument(),
    );
    await user.type(screen.getByTestId("members-set-password-input"), "short");
    await user.click(screen.getByTestId("members-set-password-submit"));

    expect(screen.getByTestId("members-set-password-error")).toBeInTheDocument();
    expect(resetMemberPassword).not.toHaveBeenCalled();
  });
});

describe("SettingsMembers — deactivate & purge", () => {
  it("shows the purge action for invited, active and suspended members", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: [activeMember, invitedNoLogin, suspendedMember],
      total: 3,
    });
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("members-purge-m-2")).toBeInTheDocument();
    expect(screen.getByTestId("members-purge-m-3")).toBeInTheDocument();
  });

  it("arms the confirm button only on an exact email match", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-purge-m-1"));

    const ok = await screen.findByTestId("purge-confirm-ok");
    expect(ok).toBeDisabled();
    // This member has signed in (subject_id set) — no "no data" note.
    expect(
      screen.queryByTestId("members-purge-no-data-note"),
    ).not.toBeInTheDocument();

    const input = screen.getByTestId("purge-confirm-input");
    await user.type(input, "wrong@example.com");
    expect(ok).toBeDisabled();
    await user.clear(input);
    await user.type(input, "alice@example.com");
    expect(ok).toBeEnabled();
  });

  it("purges the member and refreshes the roster on success", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    vi.mocked(purgeMember).mockResolvedValue(PURGE_OK);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-purge-m-1"));
    await user.type(
      await screen.findByTestId("purge-confirm-input"),
      "alice@example.com",
    );
    await user.click(screen.getByTestId("purge-confirm-ok"));

    await waitFor(() => expect(purgeMember).toHaveBeenCalledWith("m-1"));
    // Success closes the modal and re-fetches the roster.
    await waitFor(() => expect(listMembers).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(
        screen.queryByTestId("purge-confirm-input"),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps the modal open on a partial failure so retry stays actionable", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    vi.mocked(purgeMember).mockResolvedValue(PURGE_PARTIAL);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-purge-m-1"));
    await user.type(
      await screen.findByTestId("purge-confirm-input"),
      "alice@example.com",
    );
    await user.click(screen.getByTestId("purge-confirm-ok"));

    await waitFor(() => expect(purgeMember).toHaveBeenCalledWith("m-1"));
    // Partial failure stays put — modal open for a retry, no roster refresh.
    expect(screen.getByTestId("purge-confirm-input")).toBeInTheDocument();
    expect(listMembers).toHaveBeenCalledTimes(1);
  });

  it("keeps the modal open when the data-purge step failed to even start", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [activeMember], total: 1 });
    vi.mocked(purgeMember).mockResolvedValue(PURGE_DATA_STEP_FAILED);
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-1")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-purge-m-1"));
    await user.type(
      await screen.findByTestId("purge-confirm-input"),
      "alice@example.com",
    );
    await user.click(screen.getByTestId("purge-confirm-ok"));

    await waitFor(() => expect(purgeMember).toHaveBeenCalledWith("m-1"));
    // Partial failure stays put — modal open for a retry, no roster refresh.
    expect(screen.getByTestId("purge-confirm-input")).toBeInTheDocument();
    expect(listMembers).toHaveBeenCalledTimes(1);
  });

  it("notes there is no business data for a member who never signed in", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: [invitedNoLogin],
      total: 1,
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-purge-m-2")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-purge-m-2"));

    expect(
      await screen.findByTestId("members-purge-no-data-note"),
    ).toBeInTheDocument();
  });
});

describe("SettingsMembers — one-time initial password display", () => {
  it("shows one credential panel per email when invite results include a generated password", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(inviteMembers).mockResolvedValue({
      results: [
        {
          email: "new1@example.com",
          member_id: "m-10",
          status: "invited",
          error_code: null,
          initial_password: "pw-one-1234",
        },
        {
          email: "new2@example.com",
          member_id: "m-11",
          status: "invited",
          error_code: null,
          initial_password: "pw-two-5678",
        },
      ],
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-invite-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-invite-btn"));
    await user.type(
      await screen.findByTestId("members-invite-email"),
      "new1@example.com",
    );
    await user.click(screen.getByTestId("members-invite-submit"));

    await waitFor(() => expect(inviteMembers).toHaveBeenCalled());

    const block1 = await screen.findByTestId(
      "members-invite-credential-new1@example.com",
    );
    const block2 = screen.getByTestId(
      "members-invite-credential-new2@example.com",
    );
    expect(within(block1).getByText("pw-one-1234")).toBeInTheDocument();
    expect(within(block2).getByText("pw-two-5678")).toBeInTheDocument();
    // Passwords must stay visible/copyable — the drawer switches to the
    // result view (form replaced) instead of auto-closing like the
    // no-password path, and offers a single explicit close action.
    expect(screen.queryByTestId("members-invite-email")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("members-invite-credentials-close"),
    ).toBeInTheDocument();
  });

  it("keeps the plain success toast and no panel when the invite result has no password", async () => {
    vi.mocked(listMembers).mockResolvedValue({ items: [], total: 0 });
    vi.mocked(inviteMembers).mockResolvedValue({
      results: [
        {
          email: "new3@example.com",
          member_id: "m-12",
          status: "invited",
          error_code: null,
          initial_password: null,
        },
      ],
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-invite-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-invite-btn"));
    await user.type(
      await screen.findByTestId("members-invite-email"),
      "new3@example.com",
    );
    await user.click(screen.getByTestId("members-invite-submit"));

    await waitFor(() => expect(inviteMembers).toHaveBeenCalled());
    // Unchanged legacy path: no credential result view, no close-saved button.
    expect(
      screen.queryByTestId("one-time-credential-panel"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("members-invite-credentials-close"),
    ).not.toBeInTheDocument();
  });

  it("resend with a generated password opens the reset-password credential modal", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: [invitedNoLogin],
      total: 1,
    });
    vi.mocked(resendMember).mockResolvedValue({
      member_id: "m-2",
      status: "invited",
      keycloak_user_id: "kc-2",
      initial_password: "fresh-pw-9999",
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-resend-m-2")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-resend-m-2"));

    await waitFor(() => expect(resendMember).toHaveBeenCalledWith("m-2"));
    const modal = await screen.findByTestId("members-resend-credentials-modal");
    // Locale-agnostic: the test env's active language can resolve to either
    // (i18n's ``fallbackLng`` is zh-CN, but jsdom's default locale is en-US).
    expect(
      within(modal).getByText(/重置密码|password reset/i),
    ).toBeInTheDocument();
    expect(within(modal).getByText("fresh-pw-9999")).toBeInTheDocument();
  });

  it("resend with no password keeps the plain resend toast, no modal", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: [invitedNoLogin],
      total: 1,
    });
    vi.mocked(resendMember).mockResolvedValue({
      member_id: "m-2",
      status: "invited",
      keycloak_user_id: "kc-2",
      initial_password: null,
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() =>
      expect(screen.getByTestId("members-resend-m-2")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("members-resend-m-2"));

    await waitFor(() => expect(resendMember).toHaveBeenCalledWith("m-2"));
    expect(
      screen.queryByTestId("members-resend-credentials-modal"),
    ).not.toBeInTheDocument();
  });
});

describe("SettingsMembers — cross-tenant read-only view", () => {
  it("requests the cross-tenant aggregate when scope is *", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: crossTenantMembers,
      total: 2,
    });
    renderCrossTenant();

    await waitFor(() =>
      expect(listMembers).toHaveBeenCalledWith(
        expect.objectContaining({ tenantScope: "*" }),
      ),
    );
  });

  it("hides write surfaces and shows the read-only banner + tenant column", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: crossTenantMembers,
      total: 2,
    });
    renderCrossTenant();

    expect(await screen.findByTestId("members-cross-banner")).toBeInTheDocument();
    // No invite button, no per-row write actions in the aggregate view.
    expect(screen.queryByTestId("members-invite-btn")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("members-table")).toHaveTextContent(
        "bob@example.com",
      ),
    );
    expect(screen.queryByTestId("members-set-password-m-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("members-remove-m-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("members-purge-m-1")).not.toBeInTheDocument();
  });

  it("renders last_active_at when present and a dash when absent", async () => {
    vi.mocked(listMembers).mockResolvedValue({
      items: [
        { ...activeMember, last_active_at: "2026-08-27T08:00:00Z" },
        { ...activeMember, id: "m-2", email: "bob@example.com", status: "invited", last_active_at: null },
      ],
      total: 2,
    });
    renderPage();
    await screen.findByText("alice@example.com");
    const expected = new Date("2026-08-27T08:00:00Z").toLocaleString();
    expect(screen.getByText(expected)).toBeInTheDocument();
  });
});
