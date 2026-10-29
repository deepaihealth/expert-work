"""The error an ingest stops on once its document is no longer its own.

Its own module so the figure describer can tell this refusal apart from any
other failure without importing the ingestion runner (which imports it).
Re-exported by :mod:`control_plane.knowledge.ingestion`.
"""

from __future__ import annotations


class StaleIngestionError(Exception):
    """A fenced write-back was rejected: the document was re-uploaded (new
    ``ingest_generation``), cancelled (B-222) or already settled while this
    worker was busy. Nothing was written; the worker must stop quietly — the
    document now belongs to another claim."""


class ModelsCheckFailedError(StaleIngestionError):
    """B2b review —— a confirmed ingest's model check could not run (settings
    or key lookup failed). Refused like a stale claim — every paid call that
    checks stops (a call's checkpoint only stops on a
    :class:`StaleIngestionError`) — but the document is still this claim's:
    it is retried, its parse snapshot kept, and fails with
    ``models_check_failed`` once its attempts are used up."""
