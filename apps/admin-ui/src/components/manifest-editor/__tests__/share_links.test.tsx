// B-326 — the "分享文件链接" checkbox in the agent config form.
//
// Backend rule: ``share_links`` absent = off, and the block is omitted from the
// stored manifest. So the form writes ``{enabled: true}`` when on and REMOVES
// the block when off (never ``enabled: false``); ``ttl_minutes`` only when the
// user set one; 5..1440 or it is not written / the form is flagged invalid.
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../../i18n";

import { FormErrorsContext } from "../form_errors";
import {
  isValidShareLinksTtl,
  readShareLinks,
  setShareLinksOn,
  setShareLinksTtl,
  type AgentManifest,
} from "../form_model";
import { searchGroups } from "../groups";
import { ShareLinksToggle } from "../widgets/ShareLinksToggle";
import { dumpYaml, parseYaml } from "../yaml";

const SEED: AgentManifest = {
  apiVersion: "expert_work.io/v1",
  kind: "Agent",
  metadata: { name: "bot" },
  spec: {
    model: { provider: "openai", name: "gpt-4o" },
    system_prompt: { template: "hi" },
  },
};

function withBlock(block: Record<string, unknown>): AgentManifest {
  return { ...SEED, spec: { ...SEED.spec, share_links: block } };
}

describe("share_links form model", () => {
  it("is off when the block is absent", () => {
    expect(readShareLinks(SEED)).toEqual({
      enabled: false,
      ttlMinutes: undefined,
      ttlInvalid: false,
    });
  });

  it("switching on writes exactly {enabled: true}", () => {
    const on = setShareLinksOn(SEED, true);
    expect(on.spec?.share_links).toEqual({ enabled: true });
    expect(readShareLinks(on)).toEqual({ enabled: true, ttlMinutes: undefined, ttlInvalid: false });
    // Siblings untouched.
    expect(on.spec?.model).toEqual(SEED.spec?.model);
  });

  it("switching on keeps a lifetime the user already set", () => {
    const on = setShareLinksOn(withBlock({ enabled: false, ttl_minutes: 30 }), true);
    expect(on.spec?.share_links).toEqual({ enabled: true, ttl_minutes: 30 });
  });

  it("switching off removes the whole block — no enabled:false, no leftover key", () => {
    const off = setShareLinksOn(withBlock({ enabled: true, ttl_minutes: 30 }), false);
    expect(off.spec?.share_links).toBeUndefined();
    const yaml = dumpYaml(off);
    expect(yaml).not.toContain("share_links");
    expect(yaml).not.toContain("enabled: false");
    const roundTripped = parseYaml(yaml) as AgentManifest;
    expect(roundTripped.spec).not.toHaveProperty("share_links");
    expect(roundTripped.spec?.model).toEqual(SEED.spec?.model);
    expect(readShareLinks(roundTripped).enabled).toBe(false);
  });

  it("on → YAML → parse keeps the block", () => {
    const on = setShareLinksTtl(setShareLinksOn(SEED, true), 45);
    const roundTripped = parseYaml(dumpYaml(on)) as AgentManifest;
    expect(roundTripped.spec?.share_links).toEqual({ enabled: true, ttl_minutes: 45 });
  });

  it("sets and clears the lifetime", () => {
    const on = setShareLinksOn(SEED, true);
    const set = setShareLinksTtl(on, 60);
    expect(set.spec?.share_links).toEqual({ enabled: true, ttl_minutes: 60 });
    const cleared = setShareLinksTtl(set, null);
    expect(cleared.spec?.share_links).toEqual({ enabled: true });
    expect(cleared.spec?.share_links).not.toHaveProperty("ttl_minutes");
    expect(setShareLinksTtl(set, undefined).spec?.share_links).toEqual({ enabled: true });
  });

  it("does not write a lifetime while the switch is off", () => {
    expect(setShareLinksTtl(SEED, 60).spec?.share_links).toBeUndefined();
  });

  it.each([4, 1441, 0, -5, 12.5])("refuses to write an out-of-range lifetime %s", (bad) => {
    const set = setShareLinksTtl(setShareLinksOn(SEED, true), 60);
    expect(isValidShareLinksTtl(bad)).toBe(false);
    expect(setShareLinksTtl(set, bad).spec?.share_links).toEqual({
      enabled: true,
      ttl_minutes: 60,
    });
  });

  it.each([5, 120, 1440])("accepts the in-range lifetime %s", (ok) => {
    expect(isValidShareLinksTtl(ok)).toBe(true);
    expect(setShareLinksTtl(setShareLinksOn(SEED, true), ok).spec?.share_links).toEqual({
      enabled: true,
      ttl_minutes: ok,
    });
  });

  it.each([
    ["off with an out-of-range lifetime", { enabled: false, ttl_minutes: 2000 }, true],
    ["enabled missing, out of range", { ttl_minutes: 3 }, true],
    ["on with a non-numeric lifetime", { enabled: true, ttl_minutes: "abc" }, true],
    ["off with a non-numeric lifetime", { enabled: false, ttl_minutes: "2h" }, true],
    ["on with a fractional lifetime", { enabled: true, ttl_minutes: 30.5 }, true],
    ["enabled missing, valid lifetime", { ttl_minutes: 30 }, false],
    ["on without a lifetime", { enabled: true }, false],
    ["explicit null lifetime", { enabled: true, ttl_minutes: null }, false],
  ])("flags the lifetime regardless of the switch: %s", (_label, block, invalid) => {
    expect(readShareLinks(withBlock(block)).ttlInvalid).toBe(invalid);
  });

  it("is findable from settings search", () => {
    const t = (key: string) => key;
    for (const q of ["分享", "链接", "share"]) {
      expect(searchGroups(q, t).map((g) => g.id)).toContain("capabilities");
    }
  });
});

describe("ShareLinksToggle", () => {
  function renderToggle(formData: AgentManifest, onChange = vi.fn(), report = vi.fn()) {
    render(
      <FormErrorsContext.Provider value={report}>
        <ShareLinksToggle formData={formData} onChange={onChange} />
      </FormErrorsContext.Provider>,
    );
    return { onChange, report };
  }

  it("hides the lifetime input while off and turns on with {enabled: true}", async () => {
    const user = userEvent.setup();
    const { onChange } = renderToggle(SEED);
    expect(screen.queryByTestId("af-share-links-ttl")).not.toBeInTheDocument();
    // The label is the checkbox's own accessible name (no separate aria-label).
    expect(screen.getByRole("checkbox", { name: /分享文件链接|Share file links/ })).toBe(
      screen.getByTestId("af-share-links-checkbox"),
    );
    await user.click(screen.getByTestId("af-share-links-checkbox"));
    const next = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(next.spec?.share_links).toEqual({ enabled: true });
  });

  it("turning off removes the block", async () => {
    const user = userEvent.setup();
    const { onChange } = renderToggle(withBlock({ enabled: true, ttl_minutes: 30 }));
    await user.click(screen.getByTestId("af-share-links-checkbox"));
    const next = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(next.spec?.share_links).toBeUndefined();
  });

  it("shows the lifetime input when on and writes a typed value", () => {
    const { onChange, report } = renderToggle(withBlock({ enabled: true }));
    const input = screen.getByTestId("af-share-links-ttl");
    fireEvent.change(input, { target: { value: "90" } });
    const next = onChange.mock.calls.at(-1)?.[0] as AgentManifest;
    expect(next.spec?.share_links).toEqual({ enabled: true, ttl_minutes: 90 });
    expect(report).toHaveBeenLastCalledWith("share_links", false);
  });

  it("flags a bad lifetime while the switch is off and blocks saving", () => {
    const { report } = renderToggle(withBlock({ enabled: false, ttl_minutes: "abc" }));
    expect(report).toHaveBeenCalledWith("share_links", true);
    expect(screen.getByTestId("af-share-links-ttl-error")).toBeInTheDocument();
    expect(screen.queryByTestId("af-share-links-ttl")).not.toBeInTheDocument();
  });

  it("does not show the off-state error for a valid lifetime", () => {
    const { report } = renderToggle(withBlock({ enabled: false, ttl_minutes: 30 }));
    expect(report).toHaveBeenLastCalledWith("share_links", false);
    expect(screen.queryByTestId("af-share-links-ttl-error")).not.toBeInTheDocument();
  });

  it("flags an out-of-range lifetime from hand-edited YAML and blocks saving", () => {
    const { report } = renderToggle(withBlock({ enabled: true, ttl_minutes: 2000 }));
    expect(report).toHaveBeenCalledWith("share_links", true);
    expect(screen.getByText(/5.*1440/)).toBeInTheDocument();
  });

  it("turning off and back on restores the lifetime set before", async () => {
    // Round 5 L10 — the block (and its ttl) goes when switched off; the same editing
    // session brings the lifetime back instead of silently resetting to the default.
    const user = userEvent.setup();
    function Stateful() {
      const [data, setData] = useState<unknown>(withBlock({ enabled: true, ttl_minutes: 45 }));
      latest = data as AgentManifest;
      return (
        <FormErrorsContext.Provider value={vi.fn()}>
          <ShareLinksToggle formData={data} onChange={setData} />
        </FormErrorsContext.Provider>
      );
    }
    let latest: AgentManifest = SEED;
    render(<Stateful />);
    await user.click(screen.getByTestId("af-share-links-checkbox"));
    expect(latest.spec?.share_links).toBeUndefined();
    await user.click(screen.getByTestId("af-share-links-checkbox"));
    expect(latest.spec?.share_links).toEqual({ enabled: true, ttl_minutes: 45 });
    expect(screen.getByTestId("af-share-links-ttl")).toHaveValue("45");
  });
});
