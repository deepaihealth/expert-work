/**
 * Handbook page tests (``/handbook``, ``/handbook/:slug``) — role-gated
 * rendering, three-fold per the spec: ①a non-admin never sees the
 * platform-ops menu group and a direct ops-slug link 404s (not a
 * redirect); ②an admin sees both groups and the ops doc's content
 * actually renders.
 */
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { render, screen } from "@testing-library/react";
import "../../i18n";

import { Handbook } from "../Handbook";
import { AuthProvider } from "../../auth/AuthContext";
import { setStoredToken } from "../../api/client";

function makeJwt(payload: Record<string, unknown>): string {
  const header = btoa(JSON.stringify({ alg: "none", typ: "JWT" }));
  const body = btoa(JSON.stringify(payload));
  return `${header}.${body}.`;
}

function renderAt(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <AuthProvider>
        <Routes>
          <Route path="/handbook" element={<Handbook />} />
          <Route path="/handbook/:slug" element={<Handbook />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  setStoredToken(null);
});

describe("Handbook — role gating", () => {
  it("member: the bare page's menu never shows the platform-ops group", async () => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["member"] }));
    renderAt("/handbook");

    expect(await screen.findByTestId("handbook-menu")).toBeInTheDocument();
    expect(screen.queryByText("Platform ops")).not.toBeInTheDocument();
    // The usage-guide group is still there — it's visible to everyone.
    expect(screen.getByText("User guide")).toBeInTheDocument();
  });

  it("member: a direct link to an ops slug 404s instead of showing content or redirecting", async () => {
    setStoredToken(makeJwt({ sub: "u1", tenant_id: "t1", roles: ["member"] }));
    renderAt("/handbook/tenant-lifecycle");

    expect(await screen.findByTestId("handbook-not-found")).toBeInTheDocument();
    // The ops doc's own title never rendered — this is a 404, not a slow
    // load of the real content, and the route did not redirect elsewhere.
    expect(screen.queryByText("租户生命周期")).not.toBeInTheDocument();
  });

  it("admin: both groups render, and the ops doc's content shows its title", async () => {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    renderAt("/handbook/tenant-lifecycle");

    expect(await screen.findByTestId("handbook-content")).toBeInTheDocument();
    // The h2 in the content pane carries the doc's parsed front-matter title.
    expect(
      screen.getByRole("heading", { level: 2, name: "租户生命周期" }),
    ).toBeInTheDocument();
    // Both menu groups are present.
    expect(screen.getByText("User guide")).toBeInTheDocument();
    expect(screen.getByText("Platform ops")).toBeInTheDocument();
  });

  it("admin: the bare /handbook route defaults to the first tenant doc", async () => {
    setStoredToken(
      makeJwt({ sub: "u1", tenant_id: "t1", roles: ["admin", "system_admin"] }),
    );
    renderAt("/handbook");

    expect(
      await screen.findByRole("heading", { level: 2, name: "平台概览与登录" }),
    ).toBeInTheDocument();
  });
});
