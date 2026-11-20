"""B-322 PR ① —— 构建期:``build_agent`` 给 Path A 主模型装上问方向路由(设计 §4.1.2、§6.1 构建期)。

真的 ``build_agent`` / 路由 / 盖章层 / 用量存储;只把厂商适配器换成替身。验证:

* 问方向的调用走主模型自己的路由、照常落 ``token_usage``(本次构建口径);
* 有用量存储时挂了盖章层 —— 结论里记的应答模型是盖章解析出的 ``provider/name``;
* 备用树里没核实的节点被剪掉:主模型答不了时,问方向**不会**落到没核实的备用模型上
  (主循环照旧可以用它)。
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from PIL import Image

from expert_work.persistence.token_usage_store import InMemoryTokenUsageStore
from expert_work.protocol import MODEL_CATALOG, AgentSpec, ModelSpec, StructuredOutputSpec
from expert_work.runtime.checkpointer import make_checkpointer
from expert_work.runtime.middleware import LLMStreamStaleError
from expert_work.runtime.secret_store import LocalDevSecretStore
from orchestrator import MiddlewareEnv, ToolEnv, build_agent, image_views
from orchestrator.llm.endpoints import ProviderKey
from orchestrator.multimodal import InMemoryImageResolver, ResolvedImage
from orchestrator.tools.image_orientation import ORIENTATION_QUESTION
from orchestrator.tools.registry import ToolSpec

_TENANT = UUID("32200000-0000-4000-8000-0000000000a1")
_USER = UUID("32200000-0000-4000-8000-0000000000a2")
_KEY = "expert-work/dev/llm/any"
_REF = f"expert_work://image/{_TENANT}/{_USER}/00000000-0000-4000-8000-000000000001.png"
_USAGE = {"input_tokens": 1500, "output_tokens": 1, "total_tokens": 1501}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (300, 200), (200, 10, 10)).save(buffer, format="PNG")
    return buffer.getvalue()


def _spec(fallback: str = "deepseek/deepseek-flash") -> AgentSpec:
    provider, _, name = fallback.partition("/")
    return AgentSpec.model_validate(
        {
            "apiVersion": "expert_work.io/v1",
            "kind": "Agent",
            "metadata": {"name": "b322-probe", "version": "1.0.0", "tenant": "t"},
            "spec": {
                "tenant_config": {},
                "model": {
                    "provider": "qwen",
                    "name": "qwen4-max",
                    "supports_vision": True,
                    "fallback": [{"provider": provider, "name": name}],
                },
                "system_prompt": {"template": "t"},
                "sandbox": {
                    "resources": {"cpu": "1.0", "memory": "1Gi"},
                    "network": {"egress": "proxy", "allowlist": []},
                    "filesystem": {"readonly_root": True, "writable": ["/workspace"]},
                },
            },
        }
    )


def _is_question(messages: Sequence[BaseMessage]) -> int | None:
    """问方向的调用 → 候选的角度;否则 ``None``。"""
    if len(messages) < 2 or not isinstance(messages[1], HumanMessage):
        return None
    content = messages[1].content
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if isinstance(first, dict) and first.get("text") == ORIENTATION_QUESTION:
        block = content[1]
        assert isinstance(block, dict)
        return int(block["view"]["degrees"])
    return None


@dataclass
class _Qwen:
    """主模型:正常对话答 done;问方向时 0 度答 no、90 度答 yes(这张图要转 90 度)。"""

    stale_on_question: bool = False
    questions: list[int] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del tools, output_schema
        degrees = _is_question(messages)
        if degrees is None:
            return AIMessage(content="done", usage_metadata=_USAGE)  # type: ignore[arg-type]
        self.questions.append(degrees)
        if self.stale_on_question:
            raise LLMStreamStaleError("hung")
        reply = "yes" if degrees == 90 else "no"
        return AIMessage(content=reply, usage_metadata=_USAGE)  # type: ignore[arg-type]


@dataclass
class _Deepseek:
    questions: list[int] = field(default_factory=list)

    async def complete(
        self,
        *,
        messages: Sequence[BaseMessage],
        tools: Sequence[ToolSpec],
        output_schema: StructuredOutputSpec | None = None,
    ) -> AIMessage:
        del tools, output_schema
        degrees = _is_question(messages)
        if degrees is not None:
            self.questions.append(degrees)
        return AIMessage(content="yes", usage_metadata=_USAGE)  # type: ignore[arg-type]


async def _any_key(provider: str) -> list[ProviderKey]:
    del provider
    return [ProviderKey(f"secret://{_KEY}")]


@pytest.fixture(autouse=True)
def _verified_qwen(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    entries = tuple(
        e.model_copy(update={"orientation_verified": True})
        if e.name in ("qwen4-max", "qwen3.8-max")
        else e
        for e in MODEL_CATALOG["qwen"]
    )
    monkeypatch.setitem(MODEL_CATALOG, "qwen", entries)
    image_views.UPRIGHT_VIEWS.clear()
    yield
    image_views.UPRIGHT_VIEWS.clear()


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    qwen: _Qwen,
    deepseek: _Deepseek,
    *,
    backup: _Qwen | None = None,
) -> tuple[dict[str, Any], InMemoryTokenUsageStore]:
    providers: dict[str, Any] = {"qwen4-max": qwen, "deepseek-flash": deepseek}
    if backup is not None:
        providers["qwen3.8-max"] = backup

    def _fake_build_provider(entry: ModelSpec, api_key: str, **kwargs: Any) -> Any:
        del api_key, kwargs
        return providers[entry.name]

    monkeypatch.setattr("orchestrator.agent_factory._build_provider", _fake_build_provider)
    store = InMemoryTokenUsageStore()
    resolver = InMemoryImageResolver({_REF: ResolvedImage(media_type="image/png", data=_png())})
    async with make_checkpointer("memory") as cp:
        built = await build_agent(
            _spec("qwen/qwen3.8-max" if backup is not None else "deepseek/deepseek-flash"),
            secret_store=LocalDevSecretStore.from_mapping({_KEY: "sk-test"}),
            checkpointer=cp,
            provider_key_resolver=_any_key,
            tool_env=ToolEnv(image_resolver=resolver),
            middleware_env=MiddlewareEnv(token_usage_store=store),
        )
        config: RunnableConfig = {
            "configurable": {
                "thread_id": str(uuid4()),
                "tenant_id": str(_TENANT),
                "user_id": str(_USER),
                "run_id": str(uuid4()),
            }
        }
        state = await built.graph.ainvoke(
            {
                "messages": [
                    HumanMessage(
                        content=[{"type": "text", "text": "看"}, {"type": "image_ref", "ref": _REF}]
                    )
                ],
                "turn_image_refs": [_REF],
                "step_count": 0,
                "max_steps": 5,
            },
            config=config,
        )
    return state, store


async def test_built_agent_asks_on_its_own_route_and_meters_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qwen, deepseek = _Qwen(), _Deepseek()
    state, store = await _run(monkeypatch, qwen, deepseek)
    (record,) = state["image_orientations"].values()
    assert (record["conclusion"], record["degrees"]) == ("rotated", 90)
    # 盖章层挂上了:每个回答都按盖章解析成 provider/name,全部被采信。
    assert set(record["models"].values()) == {"qwen/qwen4-max"}
    assert sorted(qwen.questions) == [0, 90, 180, 270]
    rows = await store.list_for_tenant(tenant_id=_TENANT)
    qwen_rows = [r for r in rows if r.model == "qwen4-max"]
    assert len(qwen_rows) == 4 + 1  # 四次问方向 + 一次主循环
    assert {r.usage_kind for r in qwen_rows} == {"conversation"}


async def test_question_never_falls_back_to_an_unverified_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主模型答不了问方向时,剪过枝的路由没有备用 —— 判不出(unjudged),而不是让没核实的
    deepseek 来判;主循环自己的路由照旧带着 deepseek 备用。"""
    qwen, deepseek = _Qwen(stale_on_question=True), _Deepseek()
    state, _store = await _run(monkeypatch, qwen, deepseek)
    (record,) = state["image_orientations"].values()
    assert record["conclusion"] == "unjudged"
    assert deepseek.questions == []
    assert qwen.questions  # 问过主模型


async def test_a_verified_backup_answering_is_recorded_by_its_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有用量存储时问方向路由挂了盖章层:核实的备用模型接管时,结论里记的是**它**
    (没有盖章时解析器只能报配置的主模型)。"""
    qwen, backup = _Qwen(stale_on_question=True), _Qwen()
    state, _store = await _run(monkeypatch, qwen, _Deepseek(), backup=backup)
    (record,) = state["image_orientations"].values()
    assert record["conclusion"] == "rotated"
    assert set(record["models"].values()) == {"qwen/qwen3.8-max"}
    assert sorted(backup.questions) == [0, 90, 180, 270]
