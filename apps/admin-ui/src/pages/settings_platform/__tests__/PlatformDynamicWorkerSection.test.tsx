import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "antd";
import type { ReactElement } from "react";
import "../../../i18n";
import * as sdk from "../../../api/platform_dynamic_worker_config";
import { PlatformDynamicWorkerSection } from "../PlatformDynamicWorkerSection";

const EFFECTIVE = {
  max_concurrent: 3,
  max_per_run: 16,
  max_iterations: 32,
  cap_max_concurrent: 10,
  cap_max_per_run: 64,
  cap_max_iterations: 128,
};

// Wrap in antd <App> so the section's ``App.useApp()`` message API has context.
function renderSection(node: ReactElement) {
  return render(<App>{node}</App>);
}

beforeEach(() =>
  vi.spyOn(sdk, "getPlatformDynamicWorkerConfig").mockResolvedValue({
    configured: null,
    effective: { ...EFFECTIVE },
  }),
);
afterEach(() => vi.restoreAllMocks());

describe("PlatformDynamicWorkerSection", () => {
  it("shows the friendly explanation", async () => {
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");
    expect(screen.getByTestId("pdw-help")).toBeInTheDocument();
  });

  it("renders both tiers seeded from the effective limits", async () => {
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");
    expect(screen.getByTestId("pdw-max-concurrent")).toHaveValue("3");
    expect(screen.getByTestId("pdw-max-per-run")).toHaveValue("16");
    expect(screen.getByTestId("pdw-max-iterations")).toHaveValue("32");
    expect(screen.getByTestId("pdw-cap-max-concurrent")).toHaveValue("10");
    expect(screen.getByTestId("pdw-cap-max-per-run")).toHaveValue("64");
    expect(screen.getByTestId("pdw-cap-max-iterations")).toHaveValue("128");
  });

  it("tags env default when no platform override is set", async () => {
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");
    expect(screen.getByTestId("pdw-env-default")).toBeInTheDocument();
  });

  it("does not tag env default when a platform override is configured", async () => {
    vi.spyOn(sdk, "getPlatformDynamicWorkerConfig").mockResolvedValueOnce({
      configured: { ...EFFECTIVE, max_concurrent: 5 },
      effective: { ...EFFECTIVE, max_concurrent: 5 },
    });
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");
    expect(screen.queryByTestId("pdw-env-default")).not.toBeInTheDocument();
  });

  it("disables save while any field is cleared (no silent coercion)", async () => {
    const user = userEvent.setup();
    const put = vi.spyOn(sdk, "putPlatformDynamicWorkerConfig");
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");

    await user.clear(screen.getByTestId("pdw-cap-max-concurrent"));

    expect(screen.getByTestId("pdw-save")).toBeDisabled();
    await user.click(screen.getByTestId("pdw-save"));
    expect(put).not.toHaveBeenCalled();
  });

  it("disables save and warns when a default exceeds its cap", async () => {
    const user = userEvent.setup();
    const put = vi.spyOn(sdk, "putPlatformDynamicWorkerConfig");
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");

    const maxIterations = screen.getByTestId("pdw-max-iterations");
    await user.clear(maxIterations);
    await user.type(maxIterations, "200"); // cap stays 128

    expect(screen.getByTestId("pdw-default-above-cap")).toBeInTheDocument();
    expect(screen.getByTestId("pdw-save")).toBeDisabled();
    await user.click(screen.getByTestId("pdw-save"));
    expect(put).not.toHaveBeenCalled();
  });

  it("PUTs the edited six values when saved", async () => {
    const user = userEvent.setup();
    const put = vi.spyOn(sdk, "putPlatformDynamicWorkerConfig").mockResolvedValue({
      configured: { ...EFFECTIVE, max_concurrent: 5 },
      effective: { ...EFFECTIVE, max_concurrent: 5 },
    });
    renderSection(<PlatformDynamicWorkerSection />);
    await screen.findByTestId("pdw-root");

    const maxConcurrent = screen.getByTestId("pdw-max-concurrent");
    await user.clear(maxConcurrent);
    await user.type(maxConcurrent, "5");

    await user.click(screen.getByTestId("pdw-save"));

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith({ ...EFFECTIVE, max_concurrent: 5 }),
    );
  });
});
