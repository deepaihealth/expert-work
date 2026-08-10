/**
 * OneTimeCredentialPanel tests — member-password-provisioning Task 4.
 *
 * Pins the one-time-display contract: account, password and the "shown
 * only once" warning must all render, per-field copy must be available,
 * and "copy all" must build a clipboard payload containing both the
 * account and the password (mutation target for Step 5's second cut).
 *
 * Clipboard assertions read back through ``navigator.clipboard.readText()``
 * (``@testing-library/user-event``'s own in-memory clipboard stub, installed
 * by ``userEvent.setup()``) rather than a hand-rolled ``vi.fn()`` mock:
 * ``userEvent.setup()`` replaces ``navigator.clipboard`` with a getter-only
 * accessor backing a real stub, so a plain ``Object.assign(navigator, {
 * clipboard: { writeText } })`` either gets silently clobbered (if it runs
 * before ``setup()``) or throws ``Cannot set property clipboard of
 * #<Navigator> which has only a getter`` (if it runs after).
 *
 * The "copy all" button is targeted by ``data-testid`` rather than
 * ``getByRole("button", { name: /copy/i })``: each per-field
 * ``Typography.Text copyable`` also renders an icon-only button whose
 * accessible name is literally "Copy", so a ``/copy/i`` role query would
 * match multiple elements.
 */
import { describe, expect, it } from "vitest";
import { App } from "antd";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { OneTimeCredentialPanel } from "../OneTimeCredentialPanel";

function renderPanel() {
  render(
    <App>
      <OneTimeCredentialPanel account="a@b.com" password="wolf-mint-echo-1234" loginUrl="https://x" />
    </App>,
  );
}

describe("OneTimeCredentialPanel", () => {
  it("renders account, password, warning and copies all", async () => {
    const user = userEvent.setup();
    renderPanel();

    expect(screen.getByText("a@b.com")).toBeInTheDocument();
    expect(screen.getByText("wolf-mint-echo-1234")).toBeInTheDocument();
    expect(screen.getByText(/仅显示这一次|only shown once/i)).toBeInTheDocument();

    await user.click(screen.getByTestId("otc-copy-all"));
    const copied = await navigator.clipboard.readText();
    expect(copied).toContain("wolf-mint-echo-1234");
    expect(copied).toContain("a@b.com");
  });
});
