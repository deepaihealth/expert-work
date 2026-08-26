import { describe, expect, it } from "vitest";

import {
  artifactsFromTools,
  parseExecResult,
  parseToolCalls,
  skillNameOf,
  skillsFromTools,
} from "../tool_timeline";
import type { SseEvent } from "../sessions";

function evt(event: string, data: unknown): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt: "" };
}

function ev(event: string, data: unknown, receivedAt: string): SseEvent {
  return { id: null, event, data, rawData: "", receivedAt };
}

/** An ``updates`` frame for one node carrying message dicts. */
function updates(node: string, messages: unknown[]): SseEvent {
  return evt("updates", { [node]: { messages } });
}

function aiCall(id: string, name: string, args: Record<string, unknown>): unknown {
  return { type: "ai", content: "", tool_calls: [{ id, name, args, type: "tool_call" }] };
}

function aiCall2(id: string, name: string, args: Record<string, unknown>): unknown {
  return { type: "ai", content: "", tool_calls: [{ id, name, args, type: "tool_call" }] };
}

function toolResult(id: string, content: string, status = "success"): unknown {
  return { type: "tool", tool_call_id: id, name: null, content, status };
}

/** A ToolMessage carrying LangChain's ``artifact`` field — the wire shape a
 *  ``manage_task`` create result takes (builder.py:2827's
 *  ``ToolMessage(..., artifact=...)``, sourced from ``ToolResult.meta``). */
function toolResultWithArtifact(id: string, content: string, artifact: unknown): unknown {
  return { type: "tool", tool_call_id: id, name: null, content, status: "success", artifact };
}

describe("parseToolCalls", () => {
  it("links a call to its result and parses an MCP server from the name", () => {
    const events = [
      updates("agent", [aiCall("c1", "mcp__amap-maps__maps_direction_driving", { origin: "a" })]),
      updates("tools", [
        toolResult("c1", "«UNTRUSTED nonce=x»\n{\"distance\":\"1001\"}\n«/UNTRUSTED nonce=x»"),
      ]),
    ];
    const [entry, ...rest] = parseToolCalls(events);
    expect(rest).toHaveLength(0);
    expect(entry.isMcp).toBe(true);
    expect(entry.server).toBe("amap-maps");
    expect(entry.toolName).toBe("maps_direction_driving");
    expect(entry.args).toEqual({ origin: "a" });
    expect(entry.status).toBe("success");
    // Spotlight fence stripped from the preview.
    expect(entry.resultPreview).toBe('{"distance":"1001"}');
  });

  it("treats a non-mcp name as a builtin tool", () => {
    const [entry] = parseToolCalls([updates("agent", [aiCall("c1", "web_search", { q: "hi" })])]);
    expect(entry.isMcp).toBe(false);
    expect(entry.server).toBeNull();
    expect(entry.toolName).toBe("web_search");
    expect(entry.status).toBe("pending"); // no result yet
  });

  it("marks a failed tool result as error", () => {
    const events = [
      updates("agent", [aiCall("c1", "exec_python", {})]),
      updates("tools", [toolResult("c1", "boom", "error")]),
    ];
    expect(parseToolCalls(events)[0].status).toBe("error");
  });

  it("renders a gate-blocked pending tool as pending_approval", () => {
    // The gate dispatches nothing — bash has a call but never a result.
    const events = [updates("agent", [aiCall("c1", "bash", { command: "pip install x" })])];
    // Live (not yet paused): the call reads as in-progress.
    expect(parseToolCalls(events)[0].status).toBe("pending");
    // Paused at the gate: the blocked call is awaiting approval, not stuck.
    expect(parseToolCalls(events, true)[0].status).toBe("pending_approval");
  });

  it("does not downgrade a resolved tool to pending_approval", () => {
    const events = [
      updates("agent", [aiCall("c1", "web_search", {})]),
      updates("tools", [toolResult("c1", "ok")]),
    ];
    // A completed call stays success even if a later call in the turn gated.
    expect(parseToolCalls(events, true)[0].status).toBe("success");
  });

  it("preserves call order across frames and handles multiple calls", () => {
    const events = [
      updates("agent", [aiCall("c1", "web_search", {})]),
      updates("agent", [aiCall("c2", "mcp__amap-maps__geocode", {})]),
      updates("tools", [toolResult("c2", "ok"), toolResult("c1", "ok")]),
    ];
    const out = parseToolCalls(events);
    expect(out.map((e) => e.id)).toEqual(["c1", "c2"]);
    expect(out.every((e) => e.status === "success")).toBe(true);
  });

  it("ignores non-updates frames (metadata/end)", () => {
    const events = [evt("metadata", { run_id: "r" }), evt("end", "done")];
    expect(parseToolCalls(events)).toEqual([]);
  });

  it("tolerates a result without a captured call (truncated stream)", () => {
    const out = parseToolCalls([updates("tools", [toolResult("orphan", "late")])]);
    expect(out).toHaveLength(1);
    expect(out[0].id).toBe("orphan");
    expect(out[0].status).toBe("success");
  });

  it("uses the result-side name when the call frame was missed", () => {
    // Orchestrator now stamps name on the ToolMessage too.
    const named = {
      type: "tool",
      tool_call_id: "orphan",
      name: "mcp__amap-maps__geo",
      content: "{}",
      status: "success",
    };
    const [entry] = parseToolCalls([updates("tools", [named])]);
    expect(entry.isMcp).toBe(true);
    expect(entry.server).toBe("amap-maps");
    expect(entry.toolName).toBe("geo");
  });

  it("reads per-tool duration_ms from the tool result additional_kwargs", () => {
    const events = [
      ev("updates", { agent: { messages: [
        { type: "ai", content: "", tool_calls: [{ id: "c1", name: "exec_python", args: {} }] },
      ] } }, "t1"),
      ev("updates", { tools: { messages: [
        { type: "tool", tool_call_id: "c1", name: "exec_python", content: "ok", status: "success",
          additional_kwargs: { duration_ms: 840 } },
      ] } }, "t2"),
    ];
    const entries = parseToolCalls(events);
    expect(entries[0].durationMs).toBe(840);
  });

  it("leaves durationMs null when the tool result carries no duration", () => {
    const events = [
      ev("updates", { tools: { messages: [
        { type: "tool", tool_call_id: "c2", name: "web_search", content: "ok", status: "success" },
      ] } }, "t1"),
    ];
    expect(parseToolCalls(events)[0].durationMs).toBe(null);
  });
});

describe("parseToolCalls artifact.trigger_id (Spec 1 PR4 Task 4)", () => {
  it("reads triggerId from the ToolMessage artifact (manage_task create)", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create", name: "daily digest" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "Created task 'daily digest': ...", {
          trigger_id: "trig-abc-123",
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.triggerId).toBe("trig-abc-123");
  });

  it("leaves triggerId null when the result carries no artifact", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "list" })]),
      updates("tools", [toolResult("c1", "no tasks")]),
    ];
    expect(parseToolCalls(events)[0].triggerId).toBeNull();
  });

  it("leaves triggerId null when the artifact carries no trigger_id key", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "delete" })]),
      updates("tools", [toolResultWithArtifact("c1", "Deleted.", { some_other_key: 1 })]),
    ];
    expect(parseToolCalls(events)[0].triggerId).toBeNull();
  });

  it("ignores a non-string trigger_id", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create" })]),
      updates("tools", [toolResultWithArtifact("c1", "Created.", { trigger_id: 12345 })]),
    ];
    expect(parseToolCalls(events)[0].triggerId).toBeNull();
  });

  it("ignores an empty-string trigger_id", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create" })]),
      updates("tools", [toolResultWithArtifact("c1", "Created.", { trigger_id: "" })]),
    ];
    expect(parseToolCalls(events)[0].triggerId).toBeNull();
  });

  it("defaults triggerId to null for a call with no result yet", () => {
    const events = [updates("agent", [aiCall("c1", "manage_task", { action: "create" })])];
    expect(parseToolCalls(events)[0].triggerId).toBeNull();
  });
});

describe("parseToolCalls artifact.action (PR4 Task 4)", () => {
  it("reads action=create from the ToolMessage artifact", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create", name: "digest" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "Created task 'digest': ...", {
          trigger_id: "trig-abc-123",
          action: "create",
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.action).toBe("create");
  });

  it("reads action=update from the ToolMessage artifact", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "update", task_id: "t1" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "Updated task 'digest'.", {
          trigger_id: "trig-abc-123",
          action: "update",
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.action).toBe("update");
  });

  it("leaves action null when the result carries no artifact", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "list" })]),
      updates("tools", [toolResult("c1", "no tasks")]),
    ];
    expect(parseToolCalls(events)[0].action).toBeNull();
  });

  it("leaves action null when the artifact carries no action key", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create" })]),
      updates("tools", [toolResultWithArtifact("c1", "Created.", { trigger_id: "trig-1" })]),
    ];
    expect(parseToolCalls(events)[0].action).toBeNull();
  });

  it("ignores a non-string action", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create" })]),
      updates("tools", [toolResultWithArtifact("c1", "Created.", { action: 123 })]),
    ];
    expect(parseToolCalls(events)[0].action).toBeNull();
  });

  it("ignores an empty-string action", () => {
    const events = [
      updates("agent", [aiCall("c1", "manage_task", { action: "create" })]),
      updates("tools", [toolResultWithArtifact("c1", "Created.", { action: "" })]),
    ];
    expect(parseToolCalls(events)[0].action).toBeNull();
  });

  it("defaults action to null for a call with no result yet", () => {
    const events = [updates("agent", [aiCall("c1", "manage_task", { action: "create" })])];
    expect(parseToolCalls(events)[0].action).toBeNull();
  });
});

describe("artifactsFromTools", () => {
  it("returns a successfully saved artifact with its name + kind", () => {
    const events = [
      updates("agent", [aiCall("c1", "save_artifact", { name: "report.pdf", kind: "document" })]),
      updates("tools", [toolResult("c1", "Saved artifact 'report.pdf' …")]),
    ];
    expect(artifactsFromTools(events)).toEqual([{ name: "report.pdf", kind: "document" }]);
  });

  it("defaults kind to 'other' when the call omitted it", () => {
    const events = [
      updates("agent", [aiCall("c1", "save_artifact", { name: "out.bin" })]),
      updates("tools", [toolResult("c1", "Saved …")]),
    ];
    expect(artifactsFromTools(events)).toEqual([{ name: "out.bin", kind: "other" }]);
  });

  it("ignores a save still pending (no result yet)", () => {
    const events = [updates("agent", [aiCall("c1", "save_artifact", { name: "report.pdf" })])];
    expect(artifactsFromTools(events)).toEqual([]);
  });

  it("ignores a failed save", () => {
    const events = [
      updates("agent", [aiCall("c1", "save_artifact", { name: "report.pdf" })]),
      updates("tools", [toolResult("c1", "disk full", "error")]),
    ];
    expect(artifactsFromTools(events)).toEqual([]);
  });

  it("dedupes a re-saved name to one chip", () => {
    const events = [
      updates("agent", [aiCall("c1", "save_artifact", { name: "report.pdf", kind: "document" })]),
      updates("tools", [toolResult("c1", "v1")]),
      updates("agent", [aiCall("c2", "save_artifact", { name: "report.pdf", kind: "document" })]),
      updates("tools", [toolResult("c2", "v2")]),
    ];
    expect(artifactsFromTools(events)).toEqual([{ name: "report.pdf", kind: "document" }]);
  });

  it("ignores non-save_artifact tools", () => {
    const events = [
      updates("agent", [aiCall("c1", "web_search", { q: "hi" })]),
      updates("tools", [toolResult("c1", "results")]),
    ];
    expect(artifactsFromTools(events)).toEqual([]);
  });
});

describe("skillNameOf", () => {
  it("returns args.skill_name for a skill_view call", () => {
    const [entry] = parseToolCalls([
      updates("agent", [aiCall("c1", "skill_view", { skill_name: "pptx-generation", path: "SKILL.md" })]),
    ]);
    expect(skillNameOf(entry)).toBe("pptx-generation");
  });

  it("returns null for any other tool", () => {
    const [entry] = parseToolCalls([
      updates("agent", [aiCall("c1", "web_search", { skill_name: "not-a-skill" })]),
    ]);
    expect(skillNameOf(entry)).toBeNull();
  });

  it("returns null when skill_name is missing or blank", () => {
    const entries = parseToolCalls([
      updates("agent", [
        aiCall("c1", "skill_view", { path: "SKILL.md" }),
        { type: "ai", content: "", tool_calls: [{ id: "c2", name: "skill_view", args: { skill_name: "  " }, type: "tool_call" }] },
      ]),
    ]);
    expect(entries.map(skillNameOf)).toEqual([null, null]);
  });
});

describe("skillsFromTools", () => {
  it("folds multiple reads of one skill into a single entry with a read count", () => {
    const events = [
      updates("agent", [aiCall("c1", "skill_view", { skill_name: "seo", path: "SKILL.md" })]),
      updates("tools", [toolResult("c1", "# seo skill body")]),
      updates("agent", [aiCall("c2", "skill_view", { skill_name: "seo", path: "reference/checklist.md" })]),
      updates("tools", [toolResult("c2", "checklist")]),
    ];
    expect(skillsFromTools(events)).toEqual([{ name: "seo", reads: 2 }]);
  });

  it("keeps first-read order across distinct skills", () => {
    const events = [
      updates("agent", [aiCall("c1", "skill_view", { skill_name: "b-skill", path: "SKILL.md" })]),
      updates("tools", [toolResult("c1", "b")]),
      updates("agent", [aiCall("c2", "skill_view", { skill_name: "a-skill", path: "SKILL.md" })]),
      updates("tools", [toolResult("c2", "a")]),
    ];
    expect(skillsFromTools(events)).toEqual([
      { name: "b-skill", reads: 1 },
      { name: "a-skill", reads: 1 },
    ]);
  });

  it("excludes failed lookups and calls still pending", () => {
    const events = [
      updates("agent", [aiCall("c1", "skill_view", { skill_name: "gone", path: "SKILL.md" })]),
      updates("tools", [toolResult("c1", "skill not found", "error")]),
      updates("agent", [aiCall("c2", "skill_view", { skill_name: "loading", path: "SKILL.md" })]),
    ];
    expect(skillsFromTools(events)).toEqual([]);
  });
});

describe("parseExecResult", () => {
  it("splits stdout / stderr / exit_code from the rendered sandbox string", () => {
    const preview = "stdout:\nhello\nworld\n\nstderr:\noops\n\nexit_code: 0";
    expect(parseExecResult(preview)).toEqual({
      stdout: "hello\nworld",
      stderr: "oops",
      exitCode: 0,
    });
  });

  it("handles stdout-only output and a non-zero exit code", () => {
    expect(parseExecResult("stdout:\n42\n\nexit_code: 1")).toEqual({
      stdout: "42",
      stderr: "",
      exitCode: 1,
    });
  });

  it("handles the (no output) case", () => {
    expect(parseExecResult("(no output)\n\nexit_code: 0")).toEqual({
      stdout: "",
      stderr: "",
      exitCode: 0,
    });
  });

  it("returns null exitCode when the marker is absent", () => {
    expect(parseExecResult("stdout:\nx").exitCode).toBeNull();
  });

  it("sets timedOut when the body carries the [execution timed out marker", () => {
    // format_sandbox_outcome._render appends this line before exit_code when
    // outcome.timed_out (sandbox.py:488-495) — the finding-2 fix-wave path.
    const preview =
      "(no output)\n\n[execution timed out — if the command legitimately needs longer " +
      "(e.g. installing a package), re-run it with a larger timeout_s (max 300)]\n\nexit_code: -1";
    expect(parseExecResult(preview)).toEqual({
      stdout: "",
      stderr: "",
      exitCode: -1,
      timedOut: true,
    });
  });
});

describe("parseToolCalls exec attribution", () => {
  it("attaches execResult for a builtin exec_python call", () => {
    const events = [
      updates("agent", [aiCall("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [toolResult("c1", "stdout:\n1\n\nexit_code: 0")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "1", stderr: "", exitCode: 0 });
  });

  it("does not attach execResult for a non-sandbox tool", () => {
    const events = [
      updates("agent", [aiCall("c1", "web_search", { q: "x" })]),
      updates("tools", [toolResult("c1", "some result")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toBeUndefined();
  });

  it("prefers the artifact's structured exec fields over text parsing", () => {
    // Wire shape after PR-D: format_sandbox_outcome.meta carries the raw
    // (pre-datamark) streams; the content string arrives datamark-mangled.
    const events = [
      updates("agent", [aiCall2("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "«UNTRUSTED nonce=x»\nstdout:▁ 1▁ exit_code:▁ 0\n«/UNTRUSTED nonce=x»", {
          exit_code: 0,
          timed_out: false,
          truncated: false,
          stdout: "1\n",
          stderr: "",
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "1\n", stderr: "", exitCode: 0 });
  });

  it("leaves execResult unset on a datamark-mangled preview with no artifact", () => {
    // Legacy runs (pre-PR-D frames) have no exec artifact and a mangled
    // preview — the raw-preview fallback branch must stay reachable, so no
    // truthy-but-empty ExecResult may be attached.
    const events = [
      updates("agent", [aiCall2("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [toolResult("c1", "stdout:▁ 1▁ exit_code:▁ 0")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toBeUndefined();
    expect(entry.resultPreview).toBe("stdout:▁ 1▁ exit_code:▁ 0");
  });

  it("still parses a clean legacy preview without an artifact", () => {
    const events = [
      updates("agent", [aiCall2("c1", "bash", { command: "echo 1" })]),
      updates("tools", [toolResult("c1", "stdout:\n1\n\nexit_code: 0")]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "1", stderr: "", exitCode: 0 });
  });

  it("ignores a historical artifact carrying exit_code but no streams (pre-PR-D meta shape)", () => {
    // builder.py has piped meta → artifact since 2026-06-29; historical meta
    // (before this branch added stdout/stderr) carried only exit_code /
    // timed_out / truncated. Building a truthy-but-streamless execArtifact
    // from that shape would shadow the raw-preview fallback for every
    // historical run.
    const events = [
      updates("agent", [aiCall2("c1", "exec_python", { code: "print(1)" })]),
      updates("tools", [
        toolResultWithArtifact("c1", "stdout:▁ 1▁ exit_code:▁ 0", {
          exit_code: 0,
          timed_out: false,
          truncated: false,
        }),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toBeUndefined();
    expect(entry.resultPreview).toBe("stdout:▁ 1▁ exit_code:▁ 0");
  });

  it("sets timedOut on execResult when the artifact's timed_out is true", () => {
    const events = [
      updates("agent", [aiCall2("c1", "bash", { command: "sleep 999" })]),
      updates("tools", [
        toolResultWithArtifact(
          "c1",
          "«UNTRUSTED nonce=x»\n[execution timed out]▁ exit_code:▁ -1\n«/UNTRUSTED nonce=x»",
          { exit_code: -1, timed_out: true, truncated: false, stdout: "", stderr: "" },
        ),
      ]),
    ];
    const [entry] = parseToolCalls(events);
    expect(entry.execResult).toEqual({ stdout: "", stderr: "", exitCode: -1, timedOut: true });
  });
});
