"""二期 PR2 T3 — ``CredentialValueCache`` 的 lifespan 接线.

create_app 的 lifespan 造一个进程级 :class:`CredentialValueCache` 挂到
``app.state.credential_value_cache``,并把它接进 embedder /
knowledge retriever 的 reranker / quality judge。这里经
``app.router.lifespan_context``(ASGI 服务器同款 boot 路径,照
``test_eval_worker_wiring`` / ``test_checkpointer_wiring``)驱动真实
build 分支——防「漏一处 ``secret_cache=`` 接线,单测全绿但缓存静默
失效」的回归。

aux 两路(memory consolidator / skill-evolution worker)分别被
``enable_scheduler`` / ``enable_skill_evolution_worker`` 闸住,这里不开
(开了要多拉一串后台 worker);其接线由 Task 5 真栈冒烟兜底。
"""

from __future__ import annotations

import pytest

from control_plane.app import create_app
from control_plane.credential_value_cache import CredentialValueCache
from control_plane.settings import Settings
from tests.auth_fixtures import build_test_jwt_verifier


@pytest.mark.asyncio
async def test_lifespan_wires_secret_cache_into_consumers() -> None:
    app = create_app(
        settings=Settings(checkpointer_backend="memory"),
        jwt_verifier=build_test_jwt_verifier(),
        enable_reaper=False,
        enable_scheduler=False,
    )
    async with app.router.lifespan_context(app):
        cache = app.state.credential_value_cache
        assert isinstance(cache, CredentialValueCache)
        # embed 路径(DynamicResolvingEmbedder)。
        assert app.state.embedder.secret_cache is cache
        # rerank 路径(knowledge retriever 里的 DynamicResolvingReranker)。
        reranker = app.state.knowledge_retriever.reranker
        assert reranker is not None
        assert reranker.secret_cache is cache
        # judge 路径(quality monitor 常驻,默认 300s sleep-first,不真跑)。
        # 私有属性探针——接线没有公共暴露面,钉住比整洁重要。
        assert app.state.quality_monitor._judge._secret_cache is cache
