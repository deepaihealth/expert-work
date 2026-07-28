"""Uvicorn entrypoint — ``uvicorn control_plane.main:app``."""

from __future__ import annotations

from control_plane.app import create_app
from control_plane.settings import Settings

# Resolve settings once so the three per-replica background-task switches
# (multi-replica deploy — Task 3) read the same instance ``create_app``
# builds its app around, instead of ``create_app`` constructing its own
# separate ``Settings()`` from the environment.
_settings = Settings()
app = create_app(
    settings=_settings,
    enable_scheduler=_settings.enable_scheduler,
    enable_curation_worker=_settings.enable_curation_worker,
    enable_reaper=_settings.enable_reaper,
)
