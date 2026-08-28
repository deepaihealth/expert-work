"""``manage_task`` — conversational scheduled-task management (Spec 1 PR2).

A single tool with an ``action`` switch (create/list/update/cancel). It writes
per-user ``agent_trigger`` rows through an injected ``TriggerStore``; the
scheduler later fires them and (PR3) delivers results back into the originating
conversation. Guidance for the model lives entirely in this tool's schema
``description`` fields (D-7) — never in the system prompt. The tool name and
parameter schema are frozen (D-8) so a future instructional skill can reference
them by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from expert_work.persistence.trigger.base import TriggerStore
from expert_work.protocol import TriggerRecord
from orchestrator.tools.registry import (
    ToolBlockedError,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from orchestrator.tools.task_schedule import (
    ScheduleError,
    build_rrule,
    next_fire,
    summarize_schedule,
)

_SCHEDULE_KEYS = frozenset(
    {"frequency", "time", "by_day", "by_month_day", "start_date", "end_date", "count", "timezone"}
)

_DESCRIPTION = (
    "Create and manage the user's scheduled tasks — recurring or one-off jobs "
    "that run this agent automatically at a set time and deliver results back "
    "into this conversation. action=create sets one up (e.g. 'every day at 3pm, "
    "summarize AI news'); list shows the user's tasks; update changes one; "
    "cancel removes one. For create/update you MUST have a concrete time — if "
    "the user hasn't given one, ASK them first, don't guess. Pass update/cancel "
    "the task_id from a prior list."
)

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "list", "update", "cancel"],
            "description": "Which operation to perform.",
        },
        "task_id": {
            "type": "string",
            "description": "The task's id (from list). Required for update and cancel.",
        },
        "name": {
            "type": "string",
            "description": (
                "Optional short label for the task (create only). If omitted, "
                "one is derived from the instruction. Must be unique among the "
                "user's tasks for this agent."
            ),
        },
        "instruction": {
            "type": "string",
            "description": (
                "What the task should do each time it runs — becomes the agent's "
                "prompt for that run. Required on create."
            ),
        },
        "frequency": {
            "type": "string",
            "enum": ["once", "daily", "weekly", "monthly"],
            "description": "How often the task runs. Required on create.",
        },
        "time": {
            "type": "object",
            "properties": {
                "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "minute": {"type": "integer", "minimum": 0, "maximum": 59},
            },
            "required": ["hour", "minute"],
            "description": (
                "Time of day in 24h form. REQUIRED on create/update. If the user "
                "did not give a specific time, ask them before calling this tool."
            ),
        },
        "by_day": {
            "type": "array",
            "items": {"type": "string", "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]},
            "description": "Days of week, for frequency=weekly (e.g. ['WE']).",
        },
        "by_month_day": {
            "type": "integer",
            "minimum": 1,
            "maximum": 31,
            "description": "Day of month 1-31, for frequency=monthly.",
        },
        "start_date": {
            "type": "string",
            "description": "ISO date YYYY-MM-DD. Required for frequency=once (the day it runs).",
        },
        "end_date": {
            "type": "string",
            "description": "ISO date YYYY-MM-DD. Optional bound — the task stops after this day.",
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Optional — run this many times then stop (mutually exclusive with end_date)."
            ),
        },
        "timezone": {
            "type": "string",
            "description": "IANA timezone (e.g. America/New_York). Optional; defaults to UTC.",
        },
        "enabled": {
            "type": "boolean",
            "description": "update only — pause (false) or resume (true) a task.",
        },
    },
    "required": ["action"],
}


def _parse_uuid(raw: Any) -> UUID | None:
    if isinstance(raw, UUID):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return UUID(raw)
        except ValueError:
            return None
    return None


def _parse_date(raw: Any) -> date | None:
    if isinstance(raw, str) and raw:
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"bad date {raw!r} — use YYYY-MM-DD") from exc
    return None


def _time_fields(args: Mapping[str, Any]) -> tuple[int, int]:
    time = args.get("time")
    if not isinstance(time, Mapping) or time.get("hour") is None or time.get("minute") is None:
        raise ValueError(
            "I need a specific time (hour and minute) — ask the user what time, e.g. 3pm."
        )
    return int(time["hour"]), int(time["minute"])


def _compile(args: Mapping[str, Any]) -> tuple[str, str, str]:
    """(rrule, timezone, summary) from the schedule args. Raises ValueError on bad input."""
    frequency = args.get("frequency")
    if frequency is None:
        raise ValueError("a task needs a frequency (once/daily/weekly/monthly)")
    hour, minute = _time_fields(args)
    tz = str(args.get("timezone") or "UTC")
    by_day = args.get("by_day")
    by_month_day = args.get("by_month_day")
    start_date = _parse_date(args.get("start_date"))
    end_date = _parse_date(args.get("end_date"))
    count = args.get("count")
    try:
        rrule = build_rrule(
            frequency=frequency,
            hour=hour,
            minute=minute,
            by_day=by_day,
            by_month_day=by_month_day,
            start_date=start_date,
            end_date=end_date,
            count=count,
            timezone=tz,
        )
        summary = summarize_schedule(
            frequency=frequency,
            hour=hour,
            minute=minute,
            by_day=by_day,
            by_month_day=by_month_day,
            start_date=start_date,
            end_date=end_date,
            count=count,
            timezone=tz,
        )
    except ScheduleError as exc:
        raise ValueError(str(exc)) from exc
    return rrule, tz, summary


def _reject_past_once(args: Mapping[str, Any], *, timezone: str, now: datetime) -> None:
    """A one-off task must be in the future. Compare the FULL requested instant,
    not just the date — a same-day already-passed time (e.g. 'once today at 05:00'
    when it is already 06:41) would otherwise silently roll a year forward."""
    if args.get("frequency") != "once":
        return
    start_date = _parse_date(args.get("start_date"))
    if start_date is None:
        return
    hour, minute = _time_fields(args)
    requested = datetime.combine(start_date, time(hour, minute), tzinfo=ZoneInfo(timezone))
    if requested <= now:
        raise ValueError("that date/time is in the past — pick a future time")


@dataclass(frozen=True)
class ManageTaskTool:
    """``manage_task(action, ...)`` — conversational scheduled tasks."""

    store: TriggerStore
    agent_name: str
    agent_version: str

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="manage_task",
            description=_DESCRIPTION,
            parameters=_PARAMETERS,
            side_effect="reversible",
        )

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        if ctx.trigger_origin:
            raise ToolBlockedError("scheduled runs cannot manage tasks (self-scheduling guardrail)")
        action = args.get("action")
        if action == "create":
            return await self._create(args, ctx)
        if action == "list":
            return await self._list(ctx)
        if action == "update":
            return await self._update(args, ctx)
        if action == "cancel":
            return await self._cancel(args, ctx)
        raise ValueError(f"unknown action {action!r} (use create/list/update/cancel)")

    def _scope(self, ctx: ToolContext) -> tuple[UUID, UUID]:
        if ctx.tenant_id is None or ctx.user_id is None:
            raise ValueError("managing tasks requires a user-bound conversation")
        return ctx.tenant_id, ctx.user_id

    async def _create(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        tenant_id, user_id = self._scope(ctx)
        instruction = str(args.get("instruction", "")).strip()
        if not instruction:
            raise ValueError("a task needs an instruction (what to do each run)")
        now = datetime.now(UTC)

        rrule, tz, summary = _compile(args)
        _reject_past_once(args, timezone=tz, now=now)
        nxt = next_fire(rrule, timezone=tz, dtstart=now, after=now)
        if nxt is None:
            raise ValueError("that schedule has no upcoming run — check the dates")

        raw_name = str(args.get("name", "")).strip() or instruction
        name = raw_name[:60]  # TriggerRecord.name is max_length=64 — keep headroom

        # friendly pre-check (single-user conversational tool — TOCTOU race is
        # caught by the create() call below and surfaces as a generic error).
        existing = await self.store.list_by_user(
            tenant_id=tenant_id, user_id=user_id, agent_name=self.agent_name
        )
        if any(r.name == name for r in existing):
            raise ValueError(f"a task named {name!r} already exists — use a different name")

        record = TriggerRecord(
            id=uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            agent_name=self.agent_name,
            agent_version=self.agent_version,
            name=name,
            kind="cron",
            config={"rrule": rrule, "timezone": tz, "seed_input": instruction, "summary": summary},
            enabled=True,
            source="api",
            originating_thread_id=ctx.thread_id,
            context_mode="reuse_thread",
            created_at=now,
            updated_at=now,
        )
        try:
            await self.store.create(record)
        except ValueError as exc:
            msg = f"a task named {name!r} already exists — use a different name"
            raise ValueError(msg) from exc
        return ToolResult(
            content=(
                f"Created task {name!r}: {summary}. Next run "
                f"{nxt:%Y-%m-%d %H:%M UTC}. Results will appear here when it runs."
            ),
            meta={"trigger_id": str(record.id), "action": "create"},
        )

    async def _list(self, ctx: ToolContext) -> ToolResult:
        tenant_id, user_id = self._scope(ctx)
        rows = await self.store.list_by_user(
            tenant_id=tenant_id, user_id=user_id, agent_name=self.agent_name
        )
        if not rows:
            return ToolResult(content="You have no scheduled tasks.")
        now = datetime.now(UTC)
        lines: list[str] = []
        for r in rows:
            summary = str(r.config.get("summary") or "(schedule)")
            state = "" if r.enabled else " [paused]"
            nxt_s = ""
            if r.enabled:
                rrule = str(r.config.get("rrule") or "")
                tz = str(r.config.get("timezone") or "UTC")
                if rrule:
                    nxt = next_fire(rrule, timezone=tz, dtstart=r.created_at, after=now)
                    if nxt is not None:
                        nxt_s = f", next {nxt:%Y-%m-%d %H:%M UTC}"
            lines.append(f"- {r.name}{state}: {summary}{nxt_s} (id {r.id})")
        return ToolResult(content="Your scheduled tasks:\n" + "\n".join(lines))

    async def _get_owned(self, args: Mapping[str, Any], ctx: ToolContext) -> TriggerRecord:
        tenant_id, user_id = self._scope(ctx)
        tid = _parse_uuid(args.get("task_id"))
        if tid is None:
            raise ValueError("that action needs a task_id (from list)")
        rec = await self.store.get(trigger_id=tid, tenant_id=tenant_id)
        if rec is None or rec.user_id != user_id:
            # Do not distinguish "missing" from "someone else's" — no ownership leak.
            raise ValueError("no such task")
        return rec

    async def _update(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        rec = await self._get_owned(args, ctx)
        updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        config = dict(rec.config)

        if isinstance(args.get("enabled"), bool):
            updates["enabled"] = args["enabled"]

        if any(k in args for k in _SCHEDULE_KEYS):
            rrule, tz, summary = _compile(args)
            _reject_past_once(args, timezone=tz, now=datetime.now(UTC))
            config["rrule"] = rrule
            config["timezone"] = tz
            config["summary"] = summary

        instruction = args.get("instruction")
        if isinstance(instruction, str) and instruction.strip():
            config["seed_input"] = instruction.strip()

        updates["config"] = config
        ok = await self.store.update(rec.model_copy(update=updates))
        if not ok:
            raise ValueError("couldn't update the task")
        return ToolResult(
            content=f"Updated task {rec.name!r}.",
            meta={"trigger_id": str(rec.id), "action": "update"},
        )

    async def _cancel(self, args: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        rec = await self._get_owned(args, ctx)
        ok = await self.store.delete(trigger_id=rec.id, tenant_id=rec.tenant_id)
        if not ok:
            raise ValueError("couldn't cancel the task")
        return ToolResult(content=f"Cancelled task {rec.name!r}.")
