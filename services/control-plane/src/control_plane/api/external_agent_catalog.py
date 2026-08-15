"""对外 agent 目录 —— ``GET /v1/agent-catalog``。

阶段 3 (3.1)。第三方对接时得先知道「这个租户有哪些 agent 能调」,否则只能
人工问一遍 agent_code 写死在客户端里,agent 上下线客户端也不知道。

**为什么是新前缀而不是 ``/v1/agents``**:那个前缀已经被控制台面占死 ——
``85abdb39`` 给它下面 9 条路由补了 ``console_only()``,因为一把 write scope
的 key 曾能穿过 RBAC 摸到整个 manifest 面(建 / 改 / 删 / 禁用 agent)。
控制台的 ``GET /v1/agents`` 吐完整 manifest(系统提示词、工具清单、模型
配置),那是第三方永远不该看到的东西。这里只给四个字段。

新前缀的代价:三个 external 自审(``test_external_only_gate`` /
``test_external_path_param_nul_guard`` / ``test_external_route_reachability``)
原本靠 ``path.startswith("/v1/agents/")`` 发现路由,会漏掉这里。阶段 3 PR-A
Task 4 把那三个发现器改成纯 tag 驱动,所以本模块的 ``tags=["external"]``
是**必需**的,不是装饰。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from control_plane.api._authz import external_only, require
from control_plane.api._external import reject_nul_path_params
from expert_work.persistence.agent_disable import AgentDisableStore
from expert_work.persistence.agent_spec import AgentSpecStore
from expert_work.protocol import AgentSpecStatus


def _get_agent_spec_repo(request: Request) -> AgentSpecStore:
    return request.app.state.agent_spec_repo  # type: ignore[no-any-return]


def _get_agent_disable_repo(request: Request) -> AgentDisableStore:
    return request.app.state.agent_disable_repo  # type: ignore[no-any-return]


def build_external_agent_catalog_router() -> APIRouter:
    """挂载对外 agent 目录端点。"""
    router = APIRouter(
        prefix="/v1/agent-catalog",
        tags=["external"],
        # 这个前缀下没有路径参数,但守卫仍然挂上:它是 router 级的,
        # 挂在构造函数里意味着以后往这个 router 加带路径参数的路由时
        # 自动被覆盖 —— 而不是等着某个人记得补(``_external.py`` 里
        # ``reject_nul_path_params`` 的 docstring 讲的就是这条)。
        dependencies=[Depends(reject_nul_path_params), Depends(external_only())],
    )

    @router.get(
        "",
        response_model=None,
        # ``manifest:read``——``read`` scope 的 key 映射成 VIEWER 角色,
        # VIEWER 的矩阵里 manifest 是 {read},通过;零 scope 的 key 一个
        # 角色都没有,挡住(#1153 堵的就是零权限 key 的绕行)。
        dependencies=[Depends(require("manifest", "read"))],
    )
    async def list_agent_catalog(
        request: Request,
        repo: Annotated[AgentSpecStore, Depends(_get_agent_spec_repo)],
        disable_repo: Annotated[AgentDisableStore, Depends(_get_agent_disable_repo)],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JSONResponse:
        """这个租户有哪些 agent 可以调。

        ``available`` 与 ``agents.py:_resolve_session`` 用**同一对判据**:
        没被 kill switch 禁用,且存在 ``status=ACTIVE`` 的版本。两处判据
        各自漂移会让目录列出一个「点了就 403」的 agent,是客户端最难排查
        的那类不一致 —— 所以测试里把两边对在同一个断言下。

        禁用的 agent **仍然列出**,只是 ``available: false``:客户端界面上
        置灰比「凭空消失」好排查。
        """
        tenant_id: UUID = request.state.tenant_id

        # ACTIVE 版本决定「这个 code 能不能调」,也是分页的唯一驱动。同一个
        # name 可能有多个版本行,按 name 去重 —— 第三方不选版本,平台自动用
        # ACTIVE 的那个。
        #
        # 只有 DEPRECATED / DELETED 版本、没有任何 ACTIVE 版本的 code **不出
        # 现在目录里**(而不是以 available: false 出现),原因有三:
        # 1. 目录回答的是「这个租户有哪些 agent 可以调」——一个 code 没有任何
        #    可调版本 = 已退役,不属于这个问题的答案。
        # 2. disabled 和 deprecated-only 语义不同,不该一致处理:disabled 是
        #    kill switch,可逆的临时管理动作,置灰等它回来对客户端有意义;
        #    deprecated-only 是版本生命周期的终态,不会自己变回可用,列一个
        #    永远 false 且不会变的条目只是噪音。
        # 3. deprecated 是租户内部的版本管理状态,不该对第三方暴露。
        active = await repo.list_by_tenant(
            tenant_id=tenant_id, status=AgentSpecStatus.ACTIVE, limit=limit, offset=offset
        )
        # 一次拿全租户的禁用集,而不是每个 agent 查一次(N+1)。
        disabled = await disable_repo.list_disabled_names(tenant_id=tenant_id)

        seen: set[str] = set()
        agents: list[dict[str, object]] = []
        for record in active:
            if record.name in seen:
                continue
            seen.add(record.name)
            body = record.spec.spec
            agents.append(
                {
                    "agent_code": record.name,
                    # 空显示名回落到 code —— 对外响应里这个字段永远非空。
                    "display_name": body.display_name or record.name,
                    "description": body.description,
                    "available": record.name not in disabled,
                }
            )

        return JSONResponse(
            {
                "success": True,
                "data": {"agents": agents, "limit": limit, "offset": offset},
                "error": None,
            }
        )

    return router
