/**
 * CreateTenantDrawer tests — ported from SettingsCreateTenant.test.tsx.
 *
 * Pins the PR #370 tenant_id client-side UUID validation after folding the
 * standalone Create-Tenant page into a drawer: blank → omitted, valid UUID →
 * forwarded, slug → blocked before the POST. Also asserts ``onCreated`` fires
 * with the created record on success so the parent list can refresh.
 */
import { describe, expect, it, beforeEach, vi } from "vitest";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { CreateTenantDrawer } from "../CreateTenantDrawer";
import { apiClient } from "../../api/client";

interface PostCall {
  body: Record<string, unknown>;
}

let postCalls: PostCall[];

/** ``data`` payload merged into the create-tenant success response, per test. */
function installAdapter(dataOverride: Record<string, unknown> = {}): void {
  postCalls = [];
  apiClient.defaults.adapter = (config) => {
    const url = config.url ?? "";
    const method = (config.method ?? "get").toLowerCase();
    if (url === "/v1/tenants" && method === "post") {
      const body =
        typeof config.data === "string" ? JSON.parse(config.data) : (config.data ?? {});
      postCalls.push({ body });
      return Promise.resolve({
        data: {
          success: true,
          data: {
            tenant_id: "11111111-1111-1111-1111-111111111111",
            ...dataOverride,
          },
          error: null,
        },
        status: 201,
        statusText: "Created",
        headers: {},
        config,
        request: {},
      });
    }
    return Promise.resolve({
      data: {},
      status: 200,
      statusText: "OK",
      headers: {},
      config,
      request: {},
    });
  };
}

function renderDrawer(onCreated = vi.fn()): { onCreated: ReturnType<typeof vi.fn> } {
  render(
    <App>
      <CreateTenantDrawer open onClose={vi.fn()} onCreated={onCreated} />
    </App>,
  );
  return { onCreated };
}

beforeEach(() => {
  installAdapter();
});

describe("CreateTenantDrawer tenant_id validation", () => {
  it("blocks a non-UUID tenant_id and does not POST", async () => {
    const user = userEvent.setup();
    renderDrawer();

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.type(screen.getByTestId("ct-tenant-id"), "leyi-company");
    await user.click(screen.getByTestId("ct-submit"));

    expect(
      await screen.findByText(/Must be a valid UUID|合法 UUID/),
    ).toBeInTheDocument();
    expect(postCalls).toHaveLength(0);
  });

  it("omits tenant_id when left blank (server auto-generates)", async () => {
    const user = userEvent.setup();
    renderDrawer();

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.click(screen.getByTestId("ct-submit"));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(postCalls[0].body.display_name).toBe("乐毅大公司");
    expect(postCalls[0].body).not.toHaveProperty("tenant_id");
  });

  it("forwards a valid UUID tenant_id", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const uuid = "123e4567-e89b-12d3-a456-426614174000";

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.type(screen.getByTestId("ct-tenant-id"), uuid);
    await user.click(screen.getByTestId("ct-submit"));

    await waitFor(() => expect(postCalls).toHaveLength(1));
    expect(postCalls[0].body.tenant_id).toBe(uuid);
  });

  it("fires onCreated with the created record on success", async () => {
    const user = userEvent.setup();
    const { onCreated } = renderDrawer();

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.click(screen.getByTestId("ct-submit"));

    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1));
    expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ tenant_id: "11111111-1111-1111-1111-111111111111" }),
    );
  });
});

describe("CreateTenantDrawer one-time credential result", () => {
  it("switches to the credential panel when initial_password is non-empty", async () => {
    installAdapter({
      first_admin: {
        member_id: "m1",
        email: "admin@acme.example",
        status: "active",
        keycloak_user_id: "kc-1",
        initial_password: "wolf-mint-echo-1234",
      },
    });
    const user = userEvent.setup();
    renderDrawer();

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.type(screen.getByTestId("ct-first-admin-email"), "admin@acme.example");
    await user.click(screen.getByTestId("ct-submit"));

    expect(await screen.findByTestId("one-time-credential-panel")).toBeInTheDocument();
    expect(screen.getByText("wolf-mint-echo-1234")).toBeInTheDocument();
    // The result view replaces the form — it must not still be on screen.
    expect(screen.queryByTestId("ct-form")).not.toBeInTheDocument();
    expect(screen.getByTestId("ct-close-saved")).toBeInTheDocument();
  });

  it("keeps the existing success behavior when initial_password is null", async () => {
    installAdapter({
      first_admin: {
        member_id: "m1",
        email: "admin@acme.example",
        status: "pending_email",
        keycloak_user_id: "kc-1",
        initial_password: null,
      },
    });
    const user = userEvent.setup();
    renderDrawer();

    await user.type(screen.getByTestId("ct-display-name"), "乐毅大公司");
    await user.type(screen.getByTestId("ct-first-admin-email"), "admin@acme.example");
    await user.click(screen.getByTestId("ct-submit"));

    await screen.findByTestId("ct-created");
    expect(screen.queryByTestId("one-time-credential-panel")).not.toBeInTheDocument();
    // Existing behavior unchanged: form stays visible, no dedicated close button.
    expect(screen.getByTestId("ct-form")).toBeInTheDocument();
    expect(screen.queryByTestId("ct-close-saved")).not.toBeInTheDocument();
  });
});
