"""B-84 PR-2 —— 工作区快照块接进系统提示词。

四条硬要求(spec §5), 每条一组断言:

1. **块首自声明会过期** —— 照 hermes 的措辞。
2. **明说工具失败不等于文件不在** —— 与 PR-1 在错误消息里加的那句是同一个常量。
3. **只读元数据, 永不读内容** —— ``read_file`` 设成绊线。
4. **拿不到 listing 时整块不出现**, 而且 agent 照样能构建 —— 绝不出现半截块, 也
   绝不让 run 挂在一次列目录上。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from expert_work.protocol import AgentSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator.agent_factory import build_agent
from orchestrator.built_agent import BuiltAgent
from orchestrator.tools import ToolEnv
from orchestrator.tools.error_classifier import EXISTENCE_UNKNOWN
from orchestrator.tools.skill_seed import sanitize_agent_key
from orchestrator.tools.workspace_store import RecordingWorkspaceStore, WorkspaceFileEntry
from orchestrator.tools.workspace_tree import WORKSPACE_BLOCK_HEADING

_ANTHROPIC_KEY_NAME = "expert-work/dev/llm/anthropic"
_TENANT = uuid4()
_USER = uuid4()
_AGENT_NAME = "pf-probe"
_AGENT_KEY = sanitize_agent_key(_AGENT_NAME)

_SPEC_DOC: dict[str, Any] = {
    "apiVersion": "expert_work.io/v1",
    "kind": "Agent",
    "metadata": {"name": _AGENT_NAME, "version": "1.0.0", "tenant": "platform-eng"},
    "spec": {
        "tenant_config": {},
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-4-6",
            "api_key_ref": f"secret://{_ANTHROPIC_KEY_NAME}",
        },
        "system_prompt": {"template": "you are a test agent"},
        "tools": [{"type": "builtin", "name": "list_dir"}],
        "sandbox": {
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "network": {"egress": "proxy", "allowlist": ["api.anthropic.com"]},
            "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
        },
    },
}


def _spec() -> AgentSpec:
    return AgentSpec.model_validate(deepcopy(_SPEC_DOC))


async def _platform_resolver(provider: str) -> list[str]:
    del provider
    return [f"secret://{_ANTHROPIC_KEY_NAME}"]


def _entry(path: str, size: int = 21173) -> WorkspaceFileEntry:
    return WorkspaceFileEntry(path=path, size=size, mtime=datetime(2026, 9, 16, tzinfo=UTC))


def _store(**kwargs: Any) -> RecordingWorkspaceStore:
    kwargs.setdefault(
        "workspace_files",
        [
            _entry(f"agents/{_AGENT_KEY}/style/render_plan.py"),
            _entry(f"agents/{_AGENT_KEY}/style/PLAN_STYLE.md", 1229),
        ],
    )
    return RecordingWorkspaceStore(**kwargs)


async def _build(
    spec: AgentSpec, *, workspace_store: Any | None = None, **kwargs: Any
) -> BuiltAgent:
    async with make_checkpointer("memory") as cp:
        return await build_agent(
            spec,
            secret_store=LocalDevSecretStore.from_mapping({_ANTHROPIC_KEY_NAME: "sk-ant-test"}),
            checkpointer=cp,
            provider_key_resolver=_platform_resolver,
            tool_env=ToolEnv(workspace_store=workspace_store),
            tenant_id=_TENANT,
            **kwargs,
        )


async def test_block_lands_in_the_system_prompt() -> None:
    built = await _build(_spec(), workspace_store=_store(), workspace_user_id=_USER)
    prompt = built.system_prompt
    assert WORKSPACE_BLOCK_HEADING in prompt
    # 要求 1 —— 块自己声明它是快照、会过期。
    assert "snapshot" in prompt
    assert "stale" in prompt
    # 要求 2 —— 与工具错误消息里那句免责逐字同一个常量。
    assert EXISTENCE_UNKNOWN in prompt
    # 树本身:路径 + 大小, 没有别的。
    assert "render_plan.py" in prompt
    assert "21.2 KB" in prompt
    # 人写的 base 仍然原样在最前 —— 块是追加的平台段。
    assert prompt.startswith("you are a test agent")


async def test_block_reads_the_agents_own_layer_by_key() -> None:
    """列的是 ``agents/<agent_key>/``, 不是 ``agents/<agent 名>/``。

    手工拼名字会指向一个不存在的目录, 而失败形态是"目录是空的"不是报错 —— 所以
    诱饵目录里放一个只有拼错了才会被列出来的文件。
    """
    store = _store(
        workspace_files=[
            _entry(f"agents/{_AGENT_KEY}/style/anchor.py"),
            _entry(f"agents/{_AGENT_NAME}/style/decoy.py"),
        ]
    )
    built = await _build(_spec(), workspace_store=store, workspace_user_id=_USER)
    assert "anchor.py" in built.system_prompt
    assert "decoy.py" not in built.system_prompt


async def test_listing_failure_omits_the_block_and_still_builds() -> None:
    """要求 4 —— listing 抛异常时块不出现, 而 agent 仍然构建得出来。

    半截块("(无法读取)"那一类)会被模型读成"工作区是空的", 正是本 PR 要治的误判;
    而让一次列目录失败整个干掉 run, 是拿一个补充信息去换可用性。
    """
    store = _store(workspace_list_error=RuntimeError("NAS is down"))
    built = await _build(_spec(), workspace_store=store, workspace_user_id=_USER)
    assert isinstance(built, BuiltAgent)
    assert WORKSPACE_BLOCK_HEADING not in built.system_prompt
    assert "无法读取" not in built.system_prompt
    assert built.system_prompt.startswith("you are a test agent")


async def test_empty_workspace_omits_the_block() -> None:
    built = await _build(
        _spec(), workspace_store=_store(workspace_files=[]), workspace_user_id=_USER
    )
    assert WORKSPACE_BLOCK_HEADING not in built.system_prompt


async def test_no_user_binding_means_no_block() -> None:
    # 工作区按 ``{tenant}/{user}`` 存 —— 少了 user 就没有那棵树可列。这一档
    # (eval CLI 一类无 user 的 run)系统提示词与 B-84 之前逐字相同, 而不是回落成
    # "读到了一棵空树" —— 那正好是把"这个 run 没有持久工作区"伪装成"你的文件不在了"。
    built = await _build(_spec(), workspace_store=_store())
    assert WORKSPACE_BLOCK_HEADING not in built.system_prompt


async def test_block_never_reads_file_contents() -> None:
    """要求 3 —— 绊线:谁在这条路径上去读文件内容, 谁当场炸。

    工作区里有客户数据。"我只写了元数据"是一句需要测试咬着的承诺, 不是注释。
    """

    class _TripWire(RecordingWorkspaceStore):
        async def read_file(self, **kwargs: object) -> bytes:
            raise AssertionError("the workspace block must never read file contents")

    store = _TripWire(workspace_files=[_entry(f"agents/{_AGENT_KEY}/style/render_plan.py")])
    built = await _build(_spec(), workspace_store=store, workspace_user_id=_USER)
    assert WORKSPACE_BLOCK_HEADING in built.system_prompt
    assert "render_plan.py" in built.system_prompt
