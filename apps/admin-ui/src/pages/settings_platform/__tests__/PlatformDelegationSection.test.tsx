import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import type { ReactElement } from "react";
import "../../../i18n";
import * as sdk from "../../../api/platform_delegation_config";
import { PlatformDelegationSection } from "../PlatformDelegationSection";

// Wrap in antd <App> so the section's ``App.useApp()`` message API has context.
function renderSection(node: ReactElement) {
  return render(<App>{node}</App>);
}

beforeEach(() =>
  vi.spyOn(sdk, "getPlatformDelegationConfig").mockResolvedValue({
    configured: null,
    effective: { max_concurrent_delegations: 16 },
  }),
);
afterEach(() => vi.restoreAllMocks());

describe("PlatformDelegationSection", () => {
  it("shows the friendly explanation", async () => {
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");
    expect(screen.getByTestId("pdg-help")).toBeInTheDocument();
  });

  it("renders the input seeded from the effective capacity", async () => {
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");
    expect(screen.getByTestId("pdg-max-concurrent-delegations")).toHaveValue("16");
  });

  it("tags env default when no platform override is set", async () => {
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");
    expect(screen.getByTestId("pdg-env-default")).toBeInTheDocument();
  });

  it("does not tag env default when a platform override is configured", async () => {
    vi.spyOn(sdk, "getPlatformDelegationConfig").mockResolvedValueOnce({
      configured: { max_concurrent_delegations: 5 },
      effective: { max_concurrent_delegations: 5 },
    });
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");
    expect(screen.queryByTestId("pdg-env-default")).not.toBeInTheDocument();
  });

  it("disables save while the field is cleared (no silent coercion)", async () => {
    const user = userEvent.setup();
    const put = vi.spyOn(sdk, "putPlatformDelegationConfig");
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");

    await user.clear(screen.getByTestId("pdg-max-concurrent-delegations"));

    expect(screen.getByTestId("pdg-save")).toBeDisabled();
    await user.click(screen.getByTestId("pdg-save"));
    expect(put).not.toHaveBeenCalled();
  });

  it("PUTs the edited value when saved", async () => {
    const user = userEvent.setup();
    const put = vi.spyOn(sdk, "putPlatformDelegationConfig").mockResolvedValue({
      configured: { max_concurrent_delegations: 5 },
      effective: { max_concurrent_delegations: 5 },
    });
    renderSection(<PlatformDelegationSection />);
    await screen.findByTestId("pdg-root");

    const maxConcurrentDelegations = screen.getByTestId("pdg-max-concurrent-delegations");
    await user.clear(maxConcurrentDelegations);
    await user.type(maxConcurrentDelegations, "5");

    await user.click(screen.getByTestId("pdg-save"));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith({ max_concurrent_delegations: 5 }),
    );
  });
});
