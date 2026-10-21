import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";
import * as sdk from "../../../api/platform_recognition_config";
import type { PlatformRecognitionConfigView } from "../../../api/platform_recognition_config";
import { ApiError } from "../../../api/client";
import { PlatformRecognitionSection } from "../PlatformRecognitionSection";

const VIEW: PlatformRecognitionConfigView = {
  ocr: { provider: "glm", model: "glm-ocr" },
  fallback: { provider: "qwen", model: "qwen3.7-flash" },
  figure: { provider: "qwen", model: "qwen3.8-flash" },
  max_pages: 1000,
  is_default: true,
  available_ocr: [
    { provider: "glm", model: "glm-ocr" },
    { provider: "qwen", model: "qwen3.7-flash" },
    { provider: "qwen", model: "qwen3.8-flash" },
  ],
  available_figure: [
    { provider: "qwen", model: "qwen3.7-flash" },
    { provider: "qwen", model: "qwen3.8-flash" },
  ],
};

const SAVE_ALL = {
  ocr_provider: "glm",
  ocr_model: "glm-ocr",
  fallback_provider: "qwen",
  fallback_model: "qwen3.7-flash",
  figure_provider: "qwen",
  figure_model: "qwen3.8-flash",
  max_pages: 1000,
};

beforeEach(() => vi.spyOn(sdk, "getPlatformRecognitionConfig").mockResolvedValue(VIEW));
afterEach(() => vi.restoreAllMocks());

async function pick(
  user: ReturnType<typeof userEvent.setup>,
  wrapperTestId: string,
  optionText: string,
): Promise<void> {
  await user.click(within(screen.getByTestId(wrapperTestId)).getByRole("combobox"));
  const opts = await screen.findAllByText(optionText);
  const visible =
    opts.find((el) => el.className?.includes("ant-select-item-option-content")) ?? opts[0];
  await user.click(visible);
}

describe("PlatformRecognitionSection", () => {
  it("shows the effective models, the default marker and the explanation", async () => {
    render(<PlatformRecognitionSection />);
    const root = await screen.findByTestId("pr-root");
    expect(screen.getByTestId("pr-help")).toBeInTheDocument();
    expect(screen.getByTestId("pr-default")).toBeInTheDocument();
    expect(root).toHaveTextContent("glm / glm-ocr");
    expect(root).toHaveTextContent("qwen / qwen3.7-flash");
    expect(root).toHaveTextContent("qwen / qwen3.8-flash");
  });

  it("saves the selection as provider / model pairs", async () => {
    const user = userEvent.setup();
    const put = vi
      .spyOn(sdk, "putPlatformRecognitionConfig")
      .mockResolvedValue({ ...VIEW, fallback: { provider: "qwen", model: "qwen3.8-flash" } });
    render(<PlatformRecognitionSection />);
    await screen.findByTestId("pr-root");
    await pick(user, "pr-fallback", "qwen / qwen3.8-flash");
    await user.click(screen.getByTestId("pr-save"));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith({
        ...SAVE_ALL,
        fallback_provider: "qwen",
        fallback_model: "qwen3.8-flash",
      }),
    );
  });

  it("saves the page limit", async () => {
    const user = userEvent.setup();
    const put = vi
      .spyOn(sdk, "putPlatformRecognitionConfig")
      .mockResolvedValue({ ...VIEW, max_pages: 300 });
    render(<PlatformRecognitionSection />);
    await screen.findByTestId("pr-root");
    const input = screen.getByTestId("pr-max-pages"); // antd puts the test id on the input
    await user.clear(input);
    await user.type(input, "300");
    await user.click(screen.getByTestId("pr-save"));
    await waitFor(() => expect(put).toHaveBeenCalledWith({ ...SAVE_ALL, max_pages: 300 }));
  });

  it("a cleared slot is saved as switched off", async () => {
    vi.spyOn(sdk, "getPlatformRecognitionConfig").mockResolvedValueOnce({
      ...VIEW,
      fallback: null,
      is_default: false,
    });
    const user = userEvent.setup();
    const put = vi
      .spyOn(sdk, "putPlatformRecognitionConfig")
      .mockResolvedValue({ ...VIEW, fallback: null, is_default: false });
    render(<PlatformRecognitionSection />);
    await screen.findByTestId("pr-root");
    expect(screen.queryByTestId("pr-default")).toBeNull();
    await user.click(screen.getByTestId("pr-save"));
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith({
        ...SAVE_ALL,
        fallback_provider: null,
        fallback_model: null,
      }),
    );
  });

  it("surfaces a backend error code as a message", async () => {
    const user = userEvent.setup();
    vi.spyOn(sdk, "putPlatformRecognitionConfig").mockRejectedValue(
      new ApiError("key missing", "RECOGNITION_PROVIDER_KEY_MISSING", 422),
    );
    render(<PlatformRecognitionSection />);
    await screen.findByTestId("pr-root");
    await user.click(screen.getByTestId("pr-save"));
    expect(await screen.findByTestId("pr-error")).toHaveTextContent(
      "This provider has no configured key",
    );
  });

  it("shows a load error", async () => {
    vi.spyOn(sdk, "getPlatformRecognitionConfig").mockRejectedValueOnce(new Error("boom"));
    render(<PlatformRecognitionSection />);
    expect(await screen.findByTestId("pr-load-error")).toHaveTextContent("boom");
  });
});
