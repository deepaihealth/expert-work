/**
 * B-278 — PurgeAgentModal: preview rendering, blockers disarm the button,
 * type-the-name gate, clean vs partial (``ok=false``) purge, server-side block.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "antd";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "../../i18n";

import { ApiError } from "../../api/client";
import {
  getAgentPurgePreview,
  purgeAgent,
  type AgentPurgePreview,
  type AgentPurgeSummary,
} from "../../api/agents";
import { PurgeAgentModal } from "../PurgeAgentModal";

vi.mock("../../api/agents", async () => {
  const actual = await vi.importActual<typeof import("../../api/agents")>("../../api/agents");
  return { ...actual, getAgentPurgePreview: vi.fn(), purgeAgent: vi.fn() };
});

function preview(overrides: Partial<AgentPurgePreview> = {}): AgentPurgePreview {
  return {
    name: "code-reviewer",
    agent_key: "code-reviewer-1a2b3c4d",
    versions: ["1.0.0", "2.0.0"],
    blocking: { active_versions: [], referenced_by: [] },
    default_agent_cleared: false,
    threads_capped: false,
    counts: { conversations: 3, runs: 9, memories: 0, artifacts: 2, nas_users: 1 },
    kept: ["token_usage", "audit_log"],
    ...overrides,
  };
}

function summary(overrides: Partial<AgentPurgeSummary> = {}): AgentPurgeSummary {
  return {
    name: "code-reviewer",
    versions: ["1.0.0", "2.0.0"],
    deleted: { thread_meta: 3 },
    failures: {},
    skipped: [],
    agent_spec_deleted: true,
    default_agent_cleared: false,
    ok: true,
    ...overrides,
  };
}

const onClose = vi.fn();
const onPurged = vi.fn();

function renderModal(): void {
  render(
    <App>
      <PurgeAgentModal
        open
        name="code-reviewer"
        version="1.0.0"
        onClose={onClose}
        onPurged={onPurged}
      />
    </App>,
  );
}

afterEach(() => vi.clearAllMocks());

describe("PurgeAgentModal", () => {
  it("lists every version, the non-zero counts in plain words, and what is kept", async () => {
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    renderModal();

    const counts = await screen.findByTestId("purge-agent-counts");
    expect(getAgentPurgePreview).toHaveBeenCalledWith("code-reviewer", "1.0.0");
    expect(screen.getByTestId("purge-agent-versions")).toHaveTextContent("v1.0.0");
    expect(screen.getByTestId("purge-agent-versions")).toHaveTextContent("v2.0.0");
    expect(screen.getByTestId("purge-count-conversations")).toHaveTextContent("Conversations3");
    expect(screen.getByTestId("purge-count-runs")).toHaveTextContent("Run records9");
    expect(screen.getByTestId("purge-count-nas_users")).toHaveTextContent(
      "Users with workspace files1",
    );
    // Zero rows are summarised, not listed one by one.
    expect(counts).not.toHaveTextContent("Memories");
    expect(screen.getByTestId("purge-agent-kept")).toHaveTextContent(
      "Kept: usage and billing records, audit logs.",
    );
    expect(screen.queryByTestId("purge-agent-default")).toBeNull();
  });

  it("warns when the agent is the tenant default", async () => {
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview({ default_agent_cleared: true }));
    renderModal();
    expect(await screen.findByTestId("purge-agent-default")).toHaveTextContent(
      "tenant's default agent",
    );
  });

  it("arms the red button only once the exact name is typed", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    renderModal();

    const input = await screen.findByTestId("purge-agent-confirm-input");
    const ok = screen.getByTestId("purge-agent-confirm-ok");
    expect(ok).toBeDisabled();
    await user.type(input, "Code-Reviewer");
    expect(ok).toBeDisabled();
    await user.clear(input);
    await user.type(input, "code-reviewer");
    expect(ok).toBeEnabled();
  });

  it("disables the purge and explains why when something blocks it", async () => {
    vi.mocked(getAgentPurgePreview).mockResolvedValue(
      preview({ blocking: { active_versions: ["2.0.0"], referenced_by: ["parent@1.0.0"] } }),
    );
    renderModal();

    const blocked = await screen.findByTestId("purge-agent-blocked");
    expect(blocked).toHaveTextContent("Versions not deleted yet: v2.0.0");
    expect(blocked).toHaveTextContent("Used as a subagent by: parent@1.0.0");
    expect(screen.queryByTestId("purge-agent-confirm-input")).toBeNull();
    expect(screen.getByTestId("purge-agent-confirm-ok")).toBeDisabled();
  });

  it("a clean purge reports success and hands back to the caller", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockResolvedValue(summary());
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
    expect(purgeAgent).toHaveBeenCalledWith("code-reviewer", "1.0.0", "code-reviewer");
    expect(await screen.findByText("Purged code-reviewer")).toBeInTheDocument();
  });

  it("409 AGENT_PURGE_INCOMPLETE: names the failed steps, offers 「再点一次继续清理」", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent)
      .mockRejectedValueOnce(
        new ApiError("some purge steps failed", "AGENT_PURGE_INCOMPLETE", 409, {
          ...summary({ ok: false, agent_spec_deleted: false }),
          failures: { nas: "PermissionError", artifacts: "OSError" },
          skipped: ["agent_spec", "nas_quarantined"],
        }),
      )
      .mockResolvedValueOnce(summary());
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    const partial = await screen.findByTestId("purge-agent-partial");
    expect(partial).toHaveTextContent("nas (PermissionError), artifacts (OSError)");
    expect(onPurged).not.toHaveBeenCalled();
    expect(screen.queryByTestId("purge-agent-error")).toBeNull();
    const again = screen.getByTestId("purge-agent-confirm-ok");
    expect(again).toHaveTextContent("Continue cleanup");
    expect(again).toBeEnabled();

    await user.click(again);
    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
    expect(purgeAgent).toHaveBeenCalledTimes(2);
  });

  it("409 AGENT_BUSY: another purge is running — says so, stays open, can retry", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent)
      .mockRejectedValueOnce(
        new ApiError("another purge of this agent is running; retry later", "AGENT_BUSY", 409),
      )
      .mockResolvedValueOnce(summary());
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    expect(await screen.findByTestId("purge-agent-error")).toHaveTextContent(
      "Another purge of this agent is in progress, so nothing was deleted this time.",
    );
    expect(screen.queryByTestId("purge-agent-partial")).toBeNull();
    expect(onPurged).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    const ok = screen.getByTestId("purge-agent-confirm-ok");
    expect(ok).toHaveTextContent("Purge");
    expect(ok).toBeEnabled();

    await user.click(ok);
    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
  });

  it("409 AGENT_PURGE_INCOMPLETE: shows the non-zero NAS outcomes from the summary", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockRejectedValue(
      new ApiError("some data could not be deleted", "AGENT_PURGE_INCOMPLETE", 409, {
        ...summary({ ok: false, agent_spec_deleted: false }),
        failures: {},
        nas_failed_users: 1,
        nas_quarantined: 0,
        nas_symlinks_unlinked: 0,
        nas_symlinks_skipped: 2,
      }),
    );
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    const notes = await screen.findByTestId("purge-agent-nas-notes");
    expect(notes).toHaveTextContent("1 user(s)' workspace folder could be neither deleted nor moved");
    expect(notes).toHaveTextContent("2 symlink(s) on the way were skipped");
    expect(notes).not.toHaveTextContent("holding area");
    expect(notes).not.toHaveTextContent("the link was removed");
    // No failed step, so no empty "These steps didn't finish:" line.
    expect(screen.getByTestId("purge-agent-partial")).not.toHaveTextContent("These steps");
    expect(screen.getByTestId("purge-agent-confirm-ok")).toHaveTextContent("Continue cleanup");
  });

  it("a clean purge with NAS outcomes tells the admin what happened on the NAS", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockResolvedValue({ ...summary(), nas_quarantined: 2 });
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
    expect(await screen.findByTestId("purge-agent-nas-notes")).toHaveTextContent(
      "2 workspace folder(s) couldn't be deleted and were moved to a holding area",
    );
  });

  it("a clean purge without NAS outcomes shows no NAS dialog", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockResolvedValue({
      ...summary(),
      nas_quarantined: 0,
      nas_symlinks_unlinked: 0,
      nas_symlinks_skipped: 0,
      nas_failed_users: 0,
    });
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId("purge-agent-nas-notes")).toBeNull();
  });

  it("fallback 200 ok=false: names the failed steps, stays open and lets the admin click again", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent)
      .mockResolvedValueOnce(
        summary({ ok: false, agent_spec_deleted: false, failures: { artifacts: "OSError" } }),
      )
      .mockResolvedValueOnce(summary());
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    const partial = await screen.findByTestId("purge-agent-partial");
    expect(partial).toHaveTextContent("artifacts (OSError)");
    expect(partial).toHaveTextContent("to finish");
    expect(onPurged).not.toHaveBeenCalled();
    expect(screen.getByTestId("purge-agent-confirm-ok")).toBeEnabled();

    await user.click(screen.getByTestId("purge-agent-confirm-ok"));
    await waitFor(() => expect(onPurged).toHaveBeenCalledTimes(1));
    expect(purgeAgent).toHaveBeenCalledTimes(2);
  });

  it("a server-side block (409) swaps in the server's reasons and disarms", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockRejectedValue(
      new ApiError("blocked", "AGENT_PURGE_BLOCKED", 409, {
        active_versions: [],
        referenced_by: ["parent@2.0.0"],
      }),
    );
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    expect(await screen.findByTestId("purge-agent-blocked")).toHaveTextContent("parent@2.0.0");
    expect(screen.getByTestId("purge-agent-confirm-ok")).toBeDisabled();
    expect(onPurged).not.toHaveBeenCalled();
  });

  it("any other failure (e.g. a timeout) says it can be retried", async () => {
    const user = userEvent.setup();
    vi.mocked(getAgentPurgePreview).mockResolvedValue(preview());
    vi.mocked(purgeAgent).mockRejectedValue(new ApiError("Gateway Timeout", "HTTP_504", 504));
    renderModal();

    await user.type(await screen.findByTestId("purge-agent-confirm-input"), "code-reviewer");
    await user.click(screen.getByTestId("purge-agent-confirm-ok"));

    expect(await screen.findByTestId("purge-agent-error")).toHaveTextContent("You can try again");
    expect(screen.getByTestId("purge-agent-confirm-ok")).toBeEnabled();
  });
});
