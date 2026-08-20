import type { ComponentType } from "react";
import type { Meta, StoryObj } from "@storybook/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ConversationDetail } from "./ConversationDetail";
import { apiClient } from "../api/client";
import "../i18n";

const THREAD_ID = "44444444-4444-4444-4444-444444444444";

const convo = {
  thread_id: THREAD_ID,
  tenant_id: "22222222-2222-2222-2222-222222222222",
  user_id: "88888888-8888-8888-8888-888888888888",
  agent_name: "code-reviewer",
  agent_version: "1.0.0",
  title: "refund question",
  status: "active",
  created_at: "2026-06-30T12:00:00Z",
  updated_at: "2026-06-30T12:05:00Z",
  run_count: 2,
  error_count: 1,
  pending_count: 0,
  last_run_at: "2026-06-30T12:05:00Z",
  tokens: {
    input_tokens: 1500,
    output_tokens: 300,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    total_tokens: 1800,
    llm_calls: 4,
    models: ["claude-sonnet-4-5"],
  },
  runs: [
    {
      run_id: "33333333-3333-3333-3333-333333333333",
      thread_id: THREAD_ID,
      user_id: "88888888-8888-8888-8888-888888888888",
      status: "success",
      is_resume: false,
      error: null,
      created_at: "2026-06-30T12:00:00Z",
      updated_at: "2026-06-30T12:01:00Z",
      finished_at: "2026-06-30T12:01:00Z",
      trace_id: "tr-1",
      tokens: null,
    },
    {
      run_id: "33333333-3333-3333-3333-333333333334",
      thread_id: THREAD_ID,
      user_id: "88888888-8888-8888-8888-888888888888",
      status: "error",
      is_resume: true,
      error: "tool call failed",
      created_at: "2026-06-30T12:05:00Z",
      updated_at: "2026-06-30T12:05:30Z",
      finished_at: "2026-06-30T12:05:30Z",
      trace_id: "tr-2",
      tokens: null,
    },
  ],
};

const transcript = [
  { role: "user", content: "I was charged twice for my last order — can I get a refund?" },
  {
    role: "assistant",
    content: "I can see the duplicate charge. I've opened refund case #4821; the second charge will be reversed within 3–5 business days.",
  },
  { role: "user", content: "Great, thanks. Can you email me the confirmation?" },
  { role: "assistant", content: "Done — the confirmation for case #4821 was sent to your registered email." },
];

/** All three endpoints are envelope-shaped — route by URL suffix: the
 *  transcript panel's ``GET /v1/sessions/{id}/messages`` gets ``transcript``,
 *  ``useHistoryTurns``' pairing fetch (``GET /v1/sessions/{id}/runs``) gets
 *  ``convo.runs`` (2 runs, matching ``transcript``'s 2 user turns, so the
 *  page's own count-pairing guard succeeds and renders the debug console's
 *  turn timeline instead of degrading to the flat message list), and
 *  everything else (``GET /v1/conversations/{id}``) gets ``convo`` itself. */
function withStub() {
  return (Story: ComponentType) => {
    apiClient.defaults.adapter = (config) =>
      Promise.resolve({
        data: config.url?.endsWith("/messages")
          ? { success: true, data: { messages: transcript }, error: null }
          : config.url?.endsWith("/runs")
            ? { success: true, data: { runs: convo.runs }, error: null }
            : { success: true, data: convo, error: null },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
        request: {},
      });
    return (
      <MemoryRouter initialEntries={[`/conversations/${THREAD_ID}`]}>
        <Routes>
          <Route path="/conversations/:threadId" element={<Story />} />
        </Routes>
      </MemoryRouter>
    );
  };
}

const meta: Meta<typeof ConversationDetail> = {
  title: "Pages/ConversationDetail",
  component: ConversationDetail,
};
export default meta;

type Story = StoryObj<typeof ConversationDetail>;

export const Default: Story = { decorators: [withStub()] };
