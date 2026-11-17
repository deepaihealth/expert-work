/**
 * Platform Credentials page — per-tenant override drawer tests (Stream HX-8).
 *
 * Covers the override-count column, the drawer flow (pick tenant → load the
 * tenant-effective view), creating an override through the tenant API, and
 * deleting one back to fallback. SDK calls are spied directly (the page
 * imports them by name).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { App } from "antd";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { SettingsPlatformConfig } from "../SettingsPlatformConfig";
import * as sdk from "../../api/platform_config";
import * as tenantsSdk from "../../api/tenants";
import type {
  PlatformCredentialsView,
  PlatformProviderRow,
  TenantCredentialsView,
} from "../../api/platform_config";
import { AuthProvider } from "../../auth/AuthContext";
import { ApiError, setStoredToken } from "../../api/client";

const TENANT = "00000000-0000-0000-0000-00000000acce";

const VIEW: PlatformCredentialsView = {
  providers: [
    {
      provider: "anthropic",
      source: "db",
      secret_ref: "kms://platform/anthropic",
      enabled: true,
      keys: [
        {
          key_id: "default",
          secret_ref: "kms://platform/anthropic",
          enabled: true,
          priority: 100,
        },
      ],
      used_by_agents: 3,
      tenant_override_count: 1,
    },
  ],
  tools: [
    {
      tool: "web_search",
      source: "unset",
      secret_ref: null,
      enabled: false,
      used_by_agents: 0,
      tenant_override_count: 0,
    },
  ],
};

const TENANT_VIEW: TenantCredentialsView = {
  tenant_id: TENANT,
  providers: [
    {
      provider: "anthropic",
      override: {
        tenant_id: TENANT,
        provider: "anthropic",
        secret_ref: "kms://tenant/anthropic",
        enabled: true,
        created_at: "2026-06-12T10:00:00Z",
        updated_at: "2026-06-12T10:00:00Z",
        updated_by: "admin",
      },
      effective_source: "tenant",
      effective_ref: "kms://tenant/anthropic",
    },
  ],
  tools: [
    {
      tool: "web_search",
      override: null,
      effective_source: "unset",
      effective_ref: null,
    },
  ],
};

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function renderPage() {
  setStoredToken(
    makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["system_admin"] }),
  );
  vi.spyOn(sdk, "getPlatformCredentials").mockResolvedValue(VIEW);
  vi.spyOn(tenantsSdk, "listTenants").mockResolvedValue([
    {
      tenant_id: TENANT,
      display_name: "Acme",
      plan: "pro",
      created_at: "2026-06-01T00:00:00Z",
      status: "active",
    },
  ] as Awaited<ReturnType<typeof tenantsSdk.listTenants>>);
  vi.spyOn(sdk, "getTenantCredentials").mockResolvedValue(TENANT_VIEW);
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsPlatformConfig />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

async function openDrawerAndPickTenant(
  user: ReturnType<typeof userEvent.setup>,
) {
  await user.click(screen.getByTestId("pc-tenant-overrides-btn"));
  await screen.findByTestId("pc-tenant-drawer");
  await user.click(
    within(screen.getByTestId("pc-tenant-select")).getByRole("combobox"),
  );
  const opts = await screen.findAllByText(/Acme/);
  const visible =
    opts.find((el) =>
      el.className?.includes("ant-select-item-option-content"),
    ) ?? opts[0];
  await user.click(visible);
  await waitFor(() =>
    expect(sdk.getTenantCredentials).toHaveBeenCalledWith(TENANT),
  );
}

afterEach(() => vi.restoreAllMocks());

describe("SettingsPlatformConfig — tenant overrides (HX-8)", () => {
  it("renders the tenant override count column", async () => {
    renderPage();
    const table = await screen.findByTestId("pc-providers-table");
    await waitFor(() =>
      expect(within(table).getByText("1")).toBeInTheDocument(),
    );
  });

  it("drawer loads the tenant-effective view after picking a tenant", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("pc-providers-table");
    await openDrawerAndPickTenant(user);
    const providers = await screen.findByTestId("pc-tenant-providers-table");
    expect(
      within(providers).getByText("kms://tenant/anthropic"),
    ).toBeInTheDocument();
  });

  it("edits an override through the tenant API", async () => {
    const user = userEvent.setup();
    const upsert = vi
      .spyOn(sdk, "upsertTenantProviderOverride")
      .mockResolvedValue(TENANT_VIEW.providers[0].override!);
    renderPage();
    await screen.findByTestId("pc-providers-table");
    await openDrawerAndPickTenant(user);
    await screen.findByTestId("pc-tenant-providers-table");
    await user.click(screen.getByTestId("pc-tenant-edit-anthropic"));
    await screen.findByTestId("pc-edit-modal");
    await user.type(screen.getByTestId("pc-edit-value"), "sk-ant-REAL-KEY");
    await user.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(upsert).toHaveBeenCalledWith(
        TENANT,
        "anthropic",
        expect.objectContaining({ value: "sk-ant-REAL-KEY" }),
      ),
    );
  });

  it("does not render the tool credential tables (keyless web_search)", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByTestId("pc-providers-table");
    expect(screen.queryByTestId("pc-tools-table")).not.toBeInTheDocument();
    await openDrawerAndPickTenant(user);
    await screen.findByTestId("pc-tenant-providers-table");
    expect(
      screen.queryByTestId("pc-tenant-tools-table"),
    ).not.toBeInTheDocument();
  });

  it("deletes an override back to fallback", async () => {
    const user = userEvent.setup();
    const del = vi
      .spyOn(sdk, "deleteTenantProviderOverride")
      .mockResolvedValue(undefined);
    renderPage();
    await screen.findByTestId("pc-providers-table");
    await openDrawerAndPickTenant(user);
    await screen.findByTestId("pc-tenant-providers-table");
    await user.click(screen.getByTestId("pc-tenant-delete-anthropic"));
    // Popconfirm's confirm button shares the row button's "Delete" label;
    // the popover mounts last in the document body.
    const deleteButtons = await screen.findAllByRole("button", {
      name: "Delete",
    });
    await user.click(deleteButtons[deleteButtons.length - 1]);
    await waitFor(() => expect(del).toHaveBeenCalledWith(TENANT, "anthropic"));
  });
});

describe("SettingsPlatformConfig — per-provider multi-key (Y-MK)", () => {
  it("expands a provider to its key list", async () => {
    const user = userEvent.setup();
    renderPage();
    const table = await screen.findByTestId("pc-providers-table");
    // Row expander toggles the nested keys table.
    await user.click(
      within(table).getByRole("button", { name: /expand|展开/i }),
    );
    expect(
      await screen.findByTestId("pc-keys-table-anthropic"),
    ).toBeInTheDocument();
  });

  it("adds a new key through the key API", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockResolvedValue({
        key_id: "acct-b",
        secret_ref: "secret://x",
        enabled: true,
        priority: 10,
      });
    renderPage();
    await screen.findByTestId("pc-providers-table");
    await user.click(screen.getByTestId("pc-add-key-anthropic"));
    await screen.findByTestId("pc-edit-modal");
    await user.type(screen.getByTestId("pc-edit-key-id"), "acct-b");
    await user.type(screen.getByTestId("pc-edit-value"), "sk-ant-REAL");
    await user.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(upsertKey).toHaveBeenCalledWith(
        "anthropic",
        "acct-b",
        expect.objectContaining({ value: "sk-ant-REAL", priority: 100 }),
      ),
    );
  });
});

// ─── B-51 平台自身的依赖 + 警示 ────────────────────────────────────────

function providerRow(
  overrides: Partial<PlatformProviderRow> & { provider: string },
): PlatformProviderRow {
  return {
    source: "unset",
    secret_ref: null,
    enabled: false,
    keys: [],
    used_by_agents: 0,
    platform_uses: [],
    tenant_override_count: 0,
    ...overrides,
  };
}

function renderWithProviders(providers: PlatformProviderRow[]) {
  setStoredToken(
    makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["system_admin"] }),
  );
  vi.spyOn(sdk, "getPlatformCredentials").mockResolvedValue({
    providers,
    tools: [],
  });
  return render(
    <MemoryRouter>
      <AuthProvider>
        <App>
          <SettingsPlatformConfig />
        </App>
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("SettingsPlatformConfig — platform credential visibility (B-51)", () => {
  it("unset + an ENABLED platform use warns on the row and raises the banner", async () => {
    renderWithProviders([
      providerRow({
        provider: "anthropic",
        platform_uses: [
          {
            feature: "memory_consolidation",
            model: "claude-sonnet-4-6",
            enabled: true,
          },
        ],
      }),
    ]);
    const tag = await screen.findByTestId("pc-source-anthropic");
    expect(tag).toHaveTextContent(/needed by the platform/i);
    expect(tag.className).toContain("ant-tag-warning");
    expect(
      screen.getByTestId("pc-platform-uses-anthropic"),
    ).toHaveTextContent("⚠");
    const banner = screen.getByTestId("pc-platform-credentials-missing");
    expect(banner).toHaveTextContent(/anthropic/);
    expect(banner).toHaveTextContent(/Long-term memory consolidation/);
  });

  it("unset + a DISABLED-only platform use is tabled but never bannered", async () => {
    renderWithProviders([
      providerRow({
        provider: "anthropic",
        platform_uses: [
          {
            feature: "quality_judge",
            model: "claude-haiku-4-5",
            enabled: false,
          },
        ],
      }),
    ]);
    const tag = await screen.findByTestId("pc-source-anthropic");
    expect(tag).not.toHaveTextContent(/needed by the platform/i);
    expect(tag.className).not.toContain("ant-tag-warning");
    // 表格里照旧标出来 —— 只是不吵。
    expect(screen.getByTestId("pc-platform-uses-anthropic")).toHaveTextContent(
      "1 platform features",
    );
    expect(
      screen.queryByTestId("pc-platform-credentials-missing"),
    ).toBeNull();
  });

  it("unset with NO platform dependency stays plain — that is the normal state", async () => {
    renderWithProviders([providerRow({ provider: "kimi" })]);
    const tag = await screen.findByTestId("pc-source-kimi");
    expect(tag).not.toHaveTextContent(/needed by the platform/i);
    expect(tag.className).not.toContain("ant-tag-warning");
    expect(screen.queryByTestId("pc-platform-uses-kimi")).toBeNull();
    expect(
      screen.queryByTestId("pc-platform-credentials-missing"),
    ).toBeNull();
  });

  it("a configured credential with platform uses never warns", async () => {
    renderWithProviders([
      providerRow({
        provider: "anthropic",
        source: "db",
        secret_ref: "kms://platform/anthropic",
        enabled: true,
        platform_uses: [
          {
            feature: "memory_consolidation",
            model: "claude-sonnet-4-6",
            enabled: true,
          },
        ],
      }),
    ]);
    const tag = await screen.findByTestId("pc-source-anthropic");
    expect(tag).not.toHaveTextContent(/needed by the platform/i);
    expect(
      screen.queryByTestId("pc-platform-credentials-missing"),
    ).toBeNull();
  });

  it("shows the agent count and the platform count side by side", async () => {
    renderWithProviders([
      providerRow({
        provider: "anthropic",
        used_by_agents: 0,
        platform_uses: [
          { feature: "memory_consolidation", model: "m", enabled: true },
          { feature: "quality_judge", model: "m2", enabled: false },
        ],
      }),
    ]);
    expect(await screen.findByTestId("pc-agent-uses-anthropic")).toHaveTextContent(
      "0 agents",
    );
    expect(screen.getByTestId("pc-platform-uses-anthropic")).toHaveTextContent(
      "2 platform features",
    );
  });
});

// ─── B-308 请求地址 ─────────────────────────────────────────────────────

const PROXY = "https://relay.example.com:8443/team-token/v1";

function b308View(): PlatformCredentialsView {
  return {
    providers: [
      providerRow({
        provider: "openai",
        source: "db",
        secret_ref: "secret://expert-work/platform/llm/openai",
        enabled: true,
        official_base_url: "https://api.openai.com/v1",
        keys: [
          {
            key_id: "default",
            secret_ref: "secret://expert-work/platform/llm/openai",
            enabled: true,
            priority: 100,
            base_url: PROXY,
            base_url_unverified: true,
          },
          {
            key_id: "acct-b",
            secret_ref: "kms://platform/openai-b",
            enabled: true,
            priority: 200,
            base_url: null,
            base_url_unverified: false,
          },
        ],
      }),
      providerRow({
        provider: "qwen",
        source: "db",
        secret_ref: "kms://platform/qwen",
        enabled: true,
        official_base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
        keys: [
          {
            key_id: "default",
            secret_ref: "kms://platform/qwen",
            enabled: true,
            priority: 100,
            base_url: null,
            base_url_unverified: false,
          },
        ],
      }),
      providerRow({
        provider: "azure",
        source: "db",
        secret_ref: "kms://platform/azure",
        enabled: true,
        official_base_url: null,
        keys: [
          {
            key_id: "default",
            secret_ref: "kms://platform/azure",
            enabled: true,
            priority: 100,
            base_url: null,
            base_url_unverified: false,
          },
        ],
      }),
    ],
    tools: [],
  };
}

const SAVED_KEY = {
  key_id: "default",
  secret_ref: "secret://x",
  enabled: true,
  priority: 100,
};

async function expandProvider(
  user: ReturnType<typeof userEvent.setup>,
  provider: string,
) {
  const table = await screen.findByTestId("pc-providers-table");
  const row = within(table)
    .getAllByRole("row")
    .find((r) => within(r).queryByText(provider, { exact: true }) !== null);
  await user.click(
    within(row!).getByRole("button", { name: /expand|展开/i }),
  );
  return screen.findByTestId(`pc-keys-table-${provider}`);
}

async function openKeyEdit(
  user: ReturnType<typeof userEvent.setup>,
  provider: string,
  keyId: string,
) {
  await expandProvider(user, provider);
  await user.click(screen.getByTestId(`pc-key-edit-${provider}-${keyId}`));
  await screen.findByTestId("pc-edit-modal");
}

describe("SettingsPlatformConfig — request address (B-308)", () => {
  it("shows 'Official' or only the host, with the check-failed marker", async () => {
    const user = userEvent.setup();
    renderWithProviders(b308View().providers);
    await expandProvider(user, "openai");
    const custom = screen.getByTestId("pc-key-base-url-openai-default");
    expect(custom).toHaveTextContent("relay.example.com:8443");
    expect(custom).toHaveTextContent("Check failed");
    // The path may carry a token: the list never shows it.
    expect(custom).not.toHaveTextContent("team-token");
    const official = screen.getByTestId("pc-key-base-url-openai-acct-b");
    expect(official).toHaveTextContent("Official");
    expect(official).not.toHaveTextContent("Check failed");
  });

  it("the enable toggle leaves the address out of the request", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockResolvedValue(SAVED_KEY);
    renderWithProviders(b308View().providers);
    await expandProvider(user, "openai");
    await user.click(screen.getByTestId("pc-key-toggle-openai-default"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(1));
    const body = upsertKey.mock.calls[0][2];
    expect(body).toEqual({
      secret_ref: "secret://expert-work/platform/llm/openai",
      enabled: false,
      priority: 100,
    });
    expect("base_url" in body).toBe(false);
  });

  it("prefills the address and leaves it out when unchanged", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockResolvedValue(SAVED_KEY);
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "default");
    expect(screen.getByTestId("pc-edit-base-url")).toHaveValue(PROXY);
    await user.type(screen.getByTestId("pc-edit-value"), "sk-relay");
    await user.click(screen.getByText("Save"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(1));
    const body = upsertKey.mock.calls[0][2];
    expect(body.value).toBe("sk-relay");
    expect("base_url" in body).toBe(false);
    expect("force_save" in body).toBe(false);
  });

  it("uses the provider's official root as the placeholder (qwen keeps /compatible-mode/v1)", async () => {
    const user = userEvent.setup();
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "qwen", "default");
    expect(screen.getByTestId("pc-edit-base-url")).toHaveAttribute(
      "placeholder",
      "https://dashscope.aliyuncs.com/compatible-mode/v1",
    );
  });

  it("hides the address for a provider that takes none", async () => {
    const user = userEvent.setup();
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "azure", "default");
    expect(screen.queryByTestId("pc-edit-base-url")).toBeNull();
    // The list shows a dash instead of "Official" for such a provider.
    expect(screen.queryByTestId("pc-key-base-url-azure-default")).toBeNull();
  });

  it("a changed address with the same reference is stopped before sending", async () => {
    const user = userEvent.setup();
    const upsertKey = vi.spyOn(sdk, "upsertPlatformProviderKey");
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "acct-b");
    await user.click(screen.getByText("Reference (URI)"));
    const address = screen.getByTestId("pc-edit-base-url");
    await user.type(address, "https://relay.example.com/v1");
    expect(await screen.findByTestId("pc-edit-key-hint")).toHaveTextContent(
      "To change the address, also enter this service's key.",
    );
    await user.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(
        screen.getAllByText(
          "To change the address, also enter this service's key.",
        ).length,
      ).toBeGreaterThan(1),
    );
    expect(upsertKey).not.toHaveBeenCalled();
  });

  it("a changed address with an empty key box says to enter the key", async () => {
    const user = userEvent.setup();
    const upsertKey = vi.spyOn(sdk, "upsertPlatformProviderKey");
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "acct-b");
    await user.type(
      screen.getByTestId("pc-edit-base-url"),
      "https://relay.example.com/v1",
    );
    await user.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(
        screen.getAllByText(
          "To change the address, also enter this service's key.",
        ).length,
      ).toBeGreaterThan(1),
    );
    expect(upsertKey).not.toHaveBeenCalled();
  });

  it("sends a changed address with the pasted key, and null when cleared", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockResolvedValue(SAVED_KEY);
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "acct-b");
    await user.type(
      screen.getByTestId("pc-edit-base-url"),
      "  https://relay.example.com/v1  ",
    );
    await user.type(screen.getByTestId("pc-edit-value"), "sk-relay");
    await user.click(screen.getByText("Save"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(1));
    expect(upsertKey.mock.calls[0][2]).toEqual(
      expect.objectContaining({
        value: "sk-relay",
        base_url: "https://relay.example.com/v1",
      }),
    );

    // Clearing back to official needs no new key — sent as null. (The row
    // is still expanded.)
    await user.click(screen.getByTestId("pc-key-edit-openai-default"));
    await screen.findByTestId("pc-edit-modal");
    await user.clear(screen.getByTestId("pc-edit-base-url"));
    await user.click(screen.getByText("Reference (URI)"));
    await user.click(screen.getByText("Save"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(2));
    expect(upsertKey.mock.calls[1][2]).toEqual(
      expect.objectContaining({
        secret_ref: "secret://expert-work/platform/llm/openai",
        base_url: null,
      }),
    );
  });

  it("clearing the address with an empty key box keeps the current key (no switch to Reference)", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockResolvedValue(SAVED_KEY);
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "default");
    // Default "Paste a key" mode, key box left empty.
    await user.clear(screen.getByTestId("pc-edit-base-url"));
    await user.click(screen.getByText("Save"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(1));
    const body = upsertKey.mock.calls[0][2];
    // The contract takes exactly one of value / secret_ref: the row's own ref
    // means "keep the current key".
    expect(body).toEqual({
      secret_ref: "secret://expert-work/platform/llm/openai",
      enabled: true,
      priority: 100,
      base_url: null,
    });
    expect("value" in body).toBe(false);
  });

  it("any other save with an empty key box still asks for a key", async () => {
    const user = userEvent.setup();
    const upsertKey = vi.spyOn(sdk, "upsertPlatformProviderKey");
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "default");
    // Address untouched, key box empty.
    await user.click(screen.getByText("Save"));
    expect(
      await screen.findByText("Paste a key, or switch to Reference."),
    ).toBeInTheDocument();
    expect(upsertKey).not.toHaveBeenCalled();
  });

  it("a failed check explains why and offers 'save anyway' (resent with force_save)", async () => {
    const user = userEvent.setup();
    const upsertKey = vi
      .spyOn(sdk, "upsertPlatformProviderKey")
      .mockRejectedValueOnce(
        new ApiError("check failed", "BASE_URL_CHECK_FAILED", 422, {
          code: "BASE_URL_CHECK_FAILED",
          reason: "vendor_error",
          status: 502,
          interface: "chat",
        }),
      )
      .mockResolvedValueOnce(SAVED_KEY);
    renderWithProviders(b308View().providers);
    await openKeyEdit(user, "openai", "acct-b");
    await user.type(
      screen.getByTestId("pc-edit-base-url"),
      "https://relay.example.com/v1",
    );
    await user.type(screen.getByTestId("pc-edit-value"), "sk-relay");
    await user.click(screen.getByText("Save"));
    const failed = await screen.findByTestId("pc-edit-check-failed");
    expect(failed).toHaveTextContent("provider error (502)");
    expect(failed).toHaveTextContent("chat");
    expect("force_save" in upsertKey.mock.calls[0][2]).toBe(false);

    await user.click(screen.getByTestId("pc-edit-force-save"));
    await user.click(screen.getByText("Save"));
    await waitFor(() => expect(upsertKey).toHaveBeenCalledTimes(2));
    expect(upsertKey.mock.calls[1][2]).toEqual(
      expect.objectContaining({
        force_save: true,
        base_url: "https://relay.example.com/v1",
      }),
    );
  });

  it.each([
    [
      "BASE_URL_FORMAT",
      {
        hint: "version_segment",
        expected_suffix: "/compatible-mode/v1",
        example: "https://your-host/compatible-mode/v1",
      },
      "must end with /compatible-mode/v1, for example https://your-host/compatible-mode/v1",
    ],
    [
      "BASE_URL_FORMAT",
      {
        hint: "includes_api_path",
        expected_suffix: "/v1",
        example: "https://your-host/v1",
      },
      "without the API path",
    ],
    ["BASE_URL_BLOCKED_HOST", {}, "only public https addresses"],
    ["BASE_URL_INVALID", {}, "must start with https"],
    ["BASE_URL_UNRESOLVABLE", {}, "could not be found"],
    ["BASE_URL_NEEDS_NEW_KEY", {}, "also enter this service's key"],
    ["SECRET_NOT_FOUND", {}, "not found in the secret store"],
    ["BASE_URL_CHECK_UNAVAILABLE", {}, "no available chat model"],
  ])(
    "%s is explained in plain words, without 'save anyway'",
    async (code, extra, text) => {
      const user = userEvent.setup();
      const upsertKey = vi
        .spyOn(sdk, "upsertPlatformProviderKey")
        .mockRejectedValue(
          new ApiError("refused", code, 422, { code, ...extra }),
        );
      renderWithProviders(b308View().providers);
      await openKeyEdit(user, "openai", "acct-b");
      await user.type(
        screen.getByTestId("pc-edit-base-url"),
        "https://relay.example.com/v1",
      );
      await user.type(screen.getByTestId("pc-edit-value"), "sk-relay");
      await user.click(screen.getByText("Save"));
      await waitFor(() => expect(upsertKey).toHaveBeenCalled());
      expect(await screen.findByTestId("pc-edit-error")).toHaveTextContent(
        text,
      );
      expect(screen.queryByTestId("pc-edit-force-save")).toBeNull();
    },
  );

  it("a new key with an address asks for this service's key", async () => {
    const user = userEvent.setup();
    renderWithProviders(b308View().providers);
    await screen.findByTestId("pc-providers-table");
    await user.click(screen.getByTestId("pc-add-key-openai"));
    await screen.findByTestId("pc-edit-modal");
    expect(screen.queryByTestId("pc-edit-key-hint")).toBeNull();
    await user.type(
      screen.getByTestId("pc-edit-base-url"),
      "https://relay.example.com/v1",
    );
    expect(await screen.findByTestId("pc-edit-key-hint")).toHaveTextContent(
      "Enter this service's key.",
    );
  });
});

describe("SettingsPlatformConfig — tenant override request address (B-308)", () => {
  function renderTenant(view: TenantCredentialsView) {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: TENANT, roles: ["system_admin"] }),
    );
    vi.spyOn(sdk, "getPlatformCredentials").mockResolvedValue(b308View());
    vi.spyOn(tenantsSdk, "listTenants").mockResolvedValue([
      {
        tenant_id: TENANT,
        display_name: "Acme",
        plan: "pro",
        created_at: "2026-06-01T00:00:00Z",
        status: "active",
      },
    ] as Awaited<ReturnType<typeof tenantsSdk.listTenants>>);
    vi.spyOn(sdk, "getTenantCredentials").mockResolvedValue(view);
    return render(
      <MemoryRouter>
        <AuthProvider>
          <App>
            <SettingsPlatformConfig />
          </App>
        </AuthProvider>
      </MemoryRouter>,
    );
  }

  const OVERRIDE = {
    tenant_id: TENANT,
    provider: "openai",
    secret_ref: "kms://tenant/openai",
    enabled: true,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    updated_by: "admin",
    base_url: "https://relay.example.com/secret-path/v1",
    base_url_unverified: false,
  };

  it("shows the override's host in its own column", async () => {
    const user = userEvent.setup();
    renderTenant({
      tenant_id: TENANT,
      providers: [
        {
          provider: "openai",
          override: OVERRIDE,
          effective_source: "tenant",
          effective_ref: "kms://tenant/openai",
          official_base_url: "https://api.openai.com/v1",
        },
      ],
      tools: [],
    });
    await screen.findByTestId("pc-providers-table");
    await openDrawerAndPickTenant(user);
    const cell = await screen.findByTestId("pc-tenant-base-url-openai");
    expect(cell).toHaveTextContent("relay.example.com");
    expect(cell).not.toHaveTextContent("secret-path");
  });

  it("a new override with an address hints for the key and sends the address", async () => {
    const user = userEvent.setup();
    const upsert = vi
      .spyOn(sdk, "upsertTenantProviderOverride")
      .mockResolvedValue(OVERRIDE);
    renderTenant({
      tenant_id: TENANT,
      providers: [
        {
          provider: "openai",
          override: null,
          effective_source: "db",
          effective_ref: "secret://expert-work/platform/llm/openai",
          official_base_url: "https://api.openai.com/v1",
        },
      ],
      tools: [],
    });
    await screen.findByTestId("pc-providers-table");
    await openDrawerAndPickTenant(user);
    await user.click(await screen.findByTestId("pc-tenant-edit-openai"));
    await screen.findByTestId("pc-edit-modal");
    await user.type(
      screen.getByTestId("pc-edit-base-url"),
      "https://relay.example.com/v1",
    );
    expect(await screen.findByTestId("pc-edit-key-hint")).toHaveTextContent(
      "Enter this service's key.",
    );
    await user.type(screen.getByTestId("pc-edit-value"), "sk-tenant-relay");
    await user.click(screen.getByText("Save"));
    await waitFor(() =>
      expect(upsert).toHaveBeenCalledWith(
        TENANT,
        "openai",
        expect.objectContaining({
          value: "sk-tenant-relay",
          base_url: "https://relay.example.com/v1",
        }),
      ),
    );
  });
});
