"""Run lifecycle registry — in-memory :class:`RunManager` + durable :class:`RunStore`.

Algorithm pattern borrowed from bytedance/deer-flow runtime/runs/* @
``813d3c94``. ``RunManager`` stays in-memory (per-process registry +
5-minute TTL); Mini-ADR J-41 adds the durable ``agent_run`` table
behind :class:`RunStore` so a run's status survives the TTL sweep and
control-plane restarts. Run queueing / retry / DLQ remain J.10 work.
"""

from expert_work.runtime.runs.event_store import (
    InMemoryRunEventStore as InMemoryRunEventStore,
)
from expert_work.runtime.runs.event_store import RunEventRecord as RunEventRecord
from expert_work.runtime.runs.event_store import RunEventStore as RunEventStore
from expert_work.runtime.runs.event_store import SqlRunEventStore as SqlRunEventStore
from expert_work.runtime.runs.event_store import make_event_record as make_event_record
from expert_work.runtime.runs.manager import DrainObserver as DrainObserver
from expert_work.runtime.runs.manager import RunManager as RunManager
from expert_work.runtime.runs.manager import RunRecord as RunRecord
from expert_work.runtime.runs.schemas import INCOMPLETE_EXIT_REASONS as INCOMPLETE_EXIT_REASONS
from expert_work.runtime.runs.schemas import RUN_EXIT_REASONS as RUN_EXIT_REASONS
from expert_work.runtime.runs.schemas import THREAD_BUSY_STATUSES as THREAD_BUSY_STATUSES
from expert_work.runtime.runs.schemas import DisconnectMode as DisconnectMode
from expert_work.runtime.runs.schemas import ExternalRunStatus as ExternalRunStatus
from expert_work.runtime.runs.schemas import InterruptReason as InterruptReason
from expert_work.runtime.runs.schemas import RunInfo as RunInfo
from expert_work.runtime.runs.schemas import RunStatus as RunStatus
from expert_work.runtime.runs.schemas import compute_completed as compute_completed
from expert_work.runtime.runs.schemas import external_run_status as external_run_status
from expert_work.runtime.runs.schemas import run_status_filter as run_status_filter
from expert_work.runtime.runs.schemas import unresolved_failures_view as unresolved_failures_view
from expert_work.runtime.runs.store import InMemoryRunStore as InMemoryRunStore
from expert_work.runtime.runs.store import RunIdempotencyConflict as RunIdempotencyConflict
from expert_work.runtime.runs.store import RunStore as RunStore
from expert_work.runtime.runs.store import SqlRunStore as SqlRunStore

__all__ = [
    "INCOMPLETE_EXIT_REASONS",
    "RUN_EXIT_REASONS",
    "DisconnectMode",
    "DrainObserver",
    "ExternalRunStatus",
    "InMemoryRunEventStore",
    "InMemoryRunStore",
    "InterruptReason",
    "RunEventRecord",
    "RunEventStore",
    "RunIdempotencyConflict",
    "RunInfo",
    "RunManager",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "SqlRunEventStore",
    "SqlRunStore",
    "compute_completed",
    "external_run_status",
    "make_event_record",
    "run_status_filter",
    "unresolved_failures_view",
]
