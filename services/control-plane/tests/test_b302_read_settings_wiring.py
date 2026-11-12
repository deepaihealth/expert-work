"""B-302 PR 2 —— 两个平台设置(第 1 层读取规矩、第 2 层 ``figure_view_steps``)从设置到构建路径。

合并时都关(§4.2.9):``reading_rule`` 默认 ``False``,``figure_view_steps`` 默认 ``0``。
启动时读一次(``EXPERT_WORK_READING_RULE`` / ``EXPERT_WORK_FIGURE_VIEW_STEPS``),改了要重启。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from control_plane.app import create_app
from control_plane.runtime import build_tool_env
from control_plane.settings import Settings
from orchestrator import ToolEnv
from tests.auth_fixtures import build_test_jwt_verifier


class _TenantConfig:
    async def get(self, *, tenant_id: UUID, actor_id: str | None = None) -> object:
        return SimpleNamespace(http_tool_allowlist=[], http_tool_denylist=[])


def test_both_are_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXPERT_WORK_READING_RULE", raising=False)
    monkeypatch.delenv("EXPERT_WORK_FIGURE_VIEW_STEPS", raising=False)
    settings = Settings()
    assert settings.reading_rule is False
    assert settings.figure_view_steps == 0


def test_both_read_their_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_READING_RULE", "true")
    monkeypatch.setenv("EXPERT_WORK_FIGURE_VIEW_STEPS", "3")
    settings = Settings()
    assert settings.reading_rule is True
    assert settings.figure_view_steps == 3


def test_the_window_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPERT_WORK_FIGURE_VIEW_STEPS", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_build_tool_env_carries_both() -> None:
    env = build_tool_env(
        _TenantConfig(),  # type: ignore[arg-type]
        reading_rule=True,
        figure_view_steps=3,
    )
    assert env.reading_rule is True
    assert env.figure_view_steps == 3
    unwired = build_tool_env(_TenantConfig())  # type: ignore[arg-type]
    assert unwired.reading_rule is False
    assert unwired.figure_view_steps == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("rule", "steps"), [(False, 0), (True, 3)], ids=["off", "on"])
async def test_lifespan_passes_the_settings_to_the_build_path(
    monkeypatch: pytest.MonkeyPatch, rule: bool, steps: int
) -> None:
    seen: list[ToolEnv] = []

    def _spy(*args: Any, **kwargs: Any) -> ToolEnv:
        env = build_tool_env(*args, **kwargs)
        seen.append(env)
        return env

    monkeypatch.setattr("control_plane.app.build_tool_env", _spy)
    app = create_app(
        settings=Settings(
            checkpointer_backend="memory", reading_rule=rule, figure_view_steps=steps
        ),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
    )
    async with app.router.lifespan_context(app):
        pass
    [env] = seen
    assert env.reading_rule is rule
    assert env.figure_view_steps == steps
