/**
 * form_errors — the "can't save yet" channel: a widget's report reaches the
 * provider, follows the value, and is cleared when the widget unmounts.
 */
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";

import { FormErrorsContext, useReportFormErrors } from "../form_errors";

function Reporter({ hasErrors }: { hasErrors: boolean }) {
  useReportFormErrors("w", hasErrors);
  return null;
}

describe("useReportFormErrors", () => {
  it("reports the value, follows changes, and reports false on unmount", () => {
    const report = vi.fn<(source: string, hasErrors: boolean) => void>();
    const ui = (hasErrors: boolean) => (
      <FormErrorsContext.Provider value={report}>
        <Reporter hasErrors={hasErrors} />
      </FormErrorsContext.Provider>
    );
    const { rerender, unmount } = render(ui(true));
    expect(report).toHaveBeenLastCalledWith("w", true);
    rerender(ui(false));
    expect(report).toHaveBeenLastCalledWith("w", false);
    rerender(ui(true));
    expect(report).toHaveBeenLastCalledWith("w", true);
    unmount();
    expect(report).toHaveBeenLastCalledWith("w", false);
  });

  it("is a no-op outside a provider", () => {
    expect(() => render(<Reporter hasErrors />).unmount()).not.toThrow();
  });
});
