/**
 * Run detail E2E — Stream CM-8 PR4, updated by 调试台重设计 PR-B Task 4.
 *
 * Closes the PR 7e debt (approval flow was never driven end-to-end) and
 * covers the console shell's ``PlanCard``. Spec-level ``page.route``
 * registrations stack on top of ``mockControlPlane`` (later routes win),
 * so each test shapes the run/plan payloads it needs and captures the
 * writes.
 *
 * The page now also drives ``useHistoryTurns`` (single-run trajectory —
 * ``TrajectoryView``) and fetches the conversation for the Schema tab's
 * agent/version key; ``mockControlPlane``'s default conversations-list glob
 * only matches ``GET /v1/conversations`` itself (a bare ``*`` never crosses
 * a ``/``), not the ``/{thread_id}`` sub-path this page reads — so
 * ``openRunDetail`` adds its own stubs for that sub-path + the thread's
 * messages/runs/events. This thread has no other history beyond the run
 * under test, so pairing degrades to the "no trajectory" empty state
 * (none of these tests assert on the trajectory area itself) — none of
 * that reaches the assertions below, but it keeps the page from making
 * real network calls CI can't reach.
 *
 * M-5 (closed by the PR-B follow-up wave) — the approval/plan fixtures
 * still go through the pairing-failed empty state (empty ``messages``/
 * ``runs``; none of those tests assert on the trajectory area), and the
 * dedicated real-pairing test at the bottom drives the full pipeline —
 * paired ``messages``+``runs`` → per-run SSE replay → ledger rows — end
 * to end through the browser. ``TrajectoryView``'s deeper axe/interaction
 * coverage still lives on the debug console side (component tests).
 */
import { test, expect, expectNoA11yViolations, SAMPLE_JWT } from "./fixtures";
import type { Page, Route } from "@playwright/test";

const THREAD = "55555555-5555-5555-5555-555555555555";
const RUN = "44444444-4444-4444-4444-444444444444";

const PLAN = {
  goal: "ship the feature",
  steps: [
    { id: "1", description: "write tests", status: "completed" },
    { id: "2", description: "implement", status: "in_progress" },
  ],
};

const APPROVAL = {
  request_id: "req-1",
  node: "deploy",
  reason_kind: "irreversible",
  action_summary: "Deploy build 42 to production",
  proposed_args: { target: "prod", build: 42 },
  requested_at: "2026-06-10T08:00:00Z",
  timeout_at: "2026-06-11T08:00:00Z",
};

function runDetail(status: string, withApproval: boolean) {
  return {
    run_id: RUN,
    thread_id: THREAD,
    status,
    trace_id: null,
    pending_approval: withApproval ? APPROVAL : null,
  };
}

/** A real paired history for the thread: wire-shaped ``/messages`` +
 *  ``/runs`` rows and the SSE replay body for this page's run. */
interface PairingFixture {
  messages: Array<{ role: string; content: string }>;
  runs: Array<Record<string, unknown>>;
  sse: string;
}

function sseBody(frames: Array<{ id: string; event: string; data: unknown }>): string {
  return (
    frames
      .map((f) => `id: ${f.id}\nevent: ${f.event}\ndata: ${JSON.stringify(f.data)}\n`)
      .join("\n") + "\n"
  );
}

async function openRunDetail(
  page: Page,
  {
    status,
    withApproval,
    plan,
    pairing,
  }: { status: string; withApproval: boolean; plan: object | null; pairing?: PairingFixture },
): Promise<{ resumes: unknown[]; planPuts: unknown[] }> {
  const resumes: unknown[] = [];
  const planPuts: unknown[] = [];
  await page.route(`**/v1/sessions/${THREAD}/runs/${RUN}`, async (route: Route) => {
    await route.fulfill({ json: runDetail(status, withApproval) });
  });
  await page.route(`**/v1/sessions/${THREAD}/runs/${RUN}/resume`, async (route: Route) => {
    resumes.push(route.request().postDataJSON());
    await route.fulfill({ json: runDetail("running", false) });
  });
  await page.route(`**/v1/sessions/${THREAD}/plan`, async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      planPuts.push(body);
      await route.fulfill({ json: body });
      return;
    }
    if (plan === null) {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    await route.fulfill({ json: plan });
  });
  // getConversation — the Schema tab's agent/version key. A sub-path, so
  // ``mockControlPlane``'s ``**/v1/conversations*`` (list only) doesn't
  // match it (a bare ``*`` never crosses a ``/``).
  await page.route(`**/v1/conversations/${THREAD}`, async (route: Route) => {
    await route.fulfill({
      json: {
        success: true,
        error: null,
        data: {
          thread_id: THREAD,
          tenant_id: "22222222-2222-2222-2222-222222222222",
          user_id: null,
          agent_name: "demo-agent",
          agent_version: "1.0.0",
          title: null,
          status: "active",
          created_at: "2026-06-10T08:00:00Z",
          updated_at: "2026-06-10T08:00:00Z",
          run_count: 1,
          error_count: 0,
          pending_count: 0,
          last_run_at: "2026-06-10T08:00:00Z",
          tokens: null,
          runs: [],
        },
      },
    });
  });
  // useHistoryTurns' pairing fetch. Without ``pairing`` an empty thread
  // (no runs beyond the one under test) degrades to the trajectory's "no
  // trajectory" empty state — the approval/plan tests don't assert on that
  // area. With ``pairing`` the real pipeline runs: paired messages/runs,
  // then the per-run SSE replay below.
  // URL-predicate matchers, not globs: these requests carry a
  // ``?tenant_id=`` query (M-8 threads the conversation's tenant through),
  // and a glob has to match the query string too — the old bare-glob
  // registrations silently never matched, and the page "worked" only
  // because the unmatched request failed into the pairing-failed empty
  // state. Exact-pathname predicates also can't swallow ``/runs/{id}`` or
  // ``/runs/{id}/events`` the way a trailing ``runs**`` glob would.
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/messages`,
    async (route: Route) => {
      await route.fulfill({
        json: { success: true, error: null, data: { messages: pairing?.messages ?? [] } },
      });
    },
  );
  await page.route(
    (url) => url.pathname === `/v1/sessions/${THREAD}/runs`,
    async (route: Route) => {
      await route.fulfill({
        json: { success: true, error: null, data: { runs: pairing?.runs ?? [] } },
      });
    },
  );
  if (pairing) {
    await page.route(
      (url) => url.pathname === `/v1/sessions/${THREAD}/runs/${RUN}/events`,
      async (route: Route) => {
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          body: pairing.sse,
        });
      },
    );
  }

  await page.goto("/login");
  // The login card's own render race (pre-existing — same fix as
  // usage.spec.ts/knowledge.spec.ts etc.): without this wait,
  // ``tokenField.isVisible()`` below can fire before React paints the
  // form at all, always reading false and hanging on a dev-toggle that
  // was never actually needed.
  await expect(page.getByTestId("login-card")).toBeVisible();
  // Local dev servers may have VITE_OIDC_* set — the token field then
  // hides behind the dev-login toggle (CI shows it directly).
  const tokenField = page.getByTestId("login-token");
  if (!(await tokenField.isVisible())) {
    await page.getByTestId("login-dev-toggle").click();
  }
  await tokenField.fill(SAMPLE_JWT);
  await page.getByTestId("login-submit").click();
  await expect(page).toHaveURL(/\/agents$/);
  await page.goto(`/runs/${THREAD}/${RUN}`);
  return { resumes, planPuts };
}

test("approval card renders and approve posts the verdict (PR 7e debt)", async ({ page }) => {
  const { resumes } = await openRunDetail(page, {
    status: "paused",
    withApproval: true,
    plan: PLAN,
  });
  await expect(page.getByTestId("approval-card")).toBeVisible();
  await expect(page.getByText("Deploy build 42 to production")).toBeVisible();
  await page.getByTestId("approval-approve").click();
  await expect.poll(() => resumes.length).toBe(1);
  expect(resumes[0]).toEqual({ decision: "approve" });
});

test("approval reject posts the verdict", async ({ page }) => {
  const { resumes } = await openRunDetail(page, {
    status: "paused",
    withApproval: true,
    plan: null,
  });
  await page.getByTestId("approval-reject").click();
  await expect.poll(() => resumes.length).toBe(1);
  expect(resumes[0]).toEqual({ decision: "reject" });
});

test("plan panel shows the goal and steps, and edits flow through PUT", async ({ page }) => {
  const { planPuts } = await openRunDetail(page, {
    status: "success",
    withApproval: false,
    plan: PLAN,
  });
  await expect(page.getByTestId("console-plan-card")).toBeVisible();
  await expect(page.getByText("ship the feature")).toBeVisible();
  await expect(page.getByText("write tests")).toBeVisible();

  await page.getByTestId("plan-edit").click();
  await page.getByTestId("plan-step-input-1").fill("implement + review");
  await page.getByTestId("plan-save").click();
  await expect.poll(() => planPuts.length).toBe(1);
  const put = planPuts[0] as { steps: Array<{ description: string }> };
  expect(put.steps[1].description).toBe("implement + review");
});

test("plan edit is locked while the run is live", async ({ page }) => {
  await openRunDetail(page, { status: "running", withApproval: false, plan: PLAN });
  await expect(page.getByTestId("console-plan-card")).toBeVisible();
  await expect(page.getByTestId("plan-edit")).toBeDisabled();
});

// D-1 (PR-B follow-up) — the real-pairing case the M-5 note used to defer:
// a thread whose /messages pair 1:1 with its /runs, whose run replays a
// real SSE body (metadata → tool call → tool result → answer → end), and
// whose ledger therefore renders actual rows instead of the pairing-failed
// empty state every other fixture in this file goes through.
test("real paired trajectory renders this run's ledger rows", async ({ page }) => {
  await openRunDetail(page, {
    status: "success",
    withApproval: false,
    plan: null,
    pairing: {
      messages: [
        { role: "user", content: "first question" },
        { role: "assistant", content: "run one's answer" },
      ],
      runs: [
        {
          run_id: RUN,
          status: "success",
          is_resume: false,
          created_at: "2026-06-10T08:00:00Z",
          tokens: null,
        },
      ],
      sse: sseBody([
        { id: "1", event: "metadata", data: { run_id: RUN } },
        {
          id: "2",
          event: "updates",
          data: {
            agent: {
              messages: [
                {
                  type: "ai",
                  content: "",
                  tool_calls: [
                    { id: "c1", name: "search", args: { q: "expert-work" }, type: "tool_call" },
                  ],
                },
              ],
            },
          },
        },
        {
          id: "3",
          event: "updates",
          data: {
            tools: {
              messages: [
                {
                  type: "tool",
                  tool_call_id: "c1",
                  name: "search",
                  content: "3 hits",
                  status: "success",
                },
              ],
            },
          },
        },
        {
          id: "4",
          event: "updates",
          data: { agent: { messages: [{ type: "ai", content: "run one's answer" }] } },
        },
        { id: "5", event: "end", data: {} },
      ]),
    },
  });

  const ledger = page.getByTestId("console-traj-ledger");
  await expect(ledger).toBeVisible();
  // The paired USER turn, the replayed tool call and the final answer all
  // materialise as ledger rows — none of which exist on the empty-state
  // path the other tests take.
  await expect(
    page.getByTestId("console-traj-row").filter({ hasText: "first question" }),
  ).toBeVisible();
  await expect(page.getByTestId("console-traj-row").filter({ hasText: "search" })).toBeVisible();
  await expect(
    page.getByTestId("console-traj-row").filter({ hasText: "run one's answer" }),
  ).toBeVisible();
  // And the pairing-failed empty state is absent.
  await expect(page.getByTestId("console-trajectory-empty")).not.toBeVisible();
});

test("run detail with approval + plan passes axe (serious + critical)", async ({ page }) => {
  await openRunDetail(page, { status: "paused", withApproval: true, plan: PLAN });
  await expect(page.getByTestId("approval-card")).toBeVisible();
  await expect(page.getByTestId("console-plan-card")).toBeVisible();
  await expectNoA11yViolations(page, `/runs/${THREAD}/${RUN}`);
});
