"""知识库智能体层评测的两个模型:答题 / 判分(真接口与 ``--dry`` 的脚本假模型)。
从 ``kb_agent_eval.py`` 拆出(B-206 11-09)。"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

sys.path.insert(0, str(Path(__file__).parent))

import httpx
from kb_agent_cost import Price, call_cost  # type: ignore[import-not-found]
from kb_agent_judge import JUDGE_SYSTEM  # type: ignore[import-not-found]

READ_TOOL = "knowledge_read"
#: ``--dry`` 的结果行记这个模型名,与真跑的行混不到一个 ``--out`` 里。
DRY_MODEL = "dry"


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


class Models(Protocol):
    @property
    def answer_model(self) -> str:
        """Name of the model that answers."""

    @property
    def judge_model(self) -> str:
        """Name of the model that judges."""

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        """One answer-model turn: the assistant message and what it cost (CNY)."""

    async def judge(self, prompt: str) -> tuple[str, float]:
        """The judge's raw reply and what it cost (CNY)."""


async def _post(
    client: httpx.AsyncClient, url: str, key: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    for attempt in range(5):
        try:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=180.0
            )
        except httpx.TransportError:
            if attempt == 4:
                raise
            await asyncio.sleep(2 * (attempt + 1))
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 4:
            await asyncio.sleep(3 * (attempt + 1))
            continue
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())
    raise RuntimeError("unreachable")


@dataclass(frozen=True)
class ChatClient:
    """OpenAI 兼容的 ``/chat/completions``,按回包的 ``usage`` 与调用时刻记账。"""

    model: str
    base_url: str
    api_key: str
    price: Price
    http: httpx.AsyncClient

    async def complete(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], float]:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        data = await _post(self.http, url, self.api_key, {"model": self.model, **body})
        cost = call_cost(
            data.get("usage") or {}, self.price, model=self.model, at=datetime.now(UTC)
        )
        return cast(dict[str, Any], data["choices"][0]["message"]), cost


@dataclass(frozen=True)
class RealModels:
    answer_client: ChatClient
    judge_client: ChatClient

    @property
    def answer_model(self) -> str:
        return self.answer_client.model

    @property
    def judge_model(self) -> str:
        return self.judge_client.model

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        # 厂商默认的思考设置,与 B-175 PR3 那次一样不另传。
        return await self.answer_client.complete({"messages": messages, "tools": tools})

    async def judge(self, prompt: str) -> tuple[str, float]:
        message, cost = await self.judge_client.complete(
            {
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            }
        )
        return str(message.get("content") or ""), cost


class FakeModels:
    """``--dry``:用题目原文搜一次;给了 knowledge_read 就再读一次检索结果里第一个
    ``[D<n>#<k>]``(B-206 K-2);然后回一句占位。判分固定。不花钱、不连网。"""

    answer_model = DRY_MODEL
    judge_model = DRY_MODEL

    async def agent(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], float]:
        results = [str(m["content"]) for m in messages if m["role"] == "tool"]
        if not results:
            question = messages[-1]["content"]
            return _dry_call(tools[0]["function"]["name"], {"query": question}), 0.0
        offered = {t["function"]["name"] for t in tools}
        reference = _DRY_REFERENCE.search(results[0])
        if READ_TOOL in offered and len(results) == 1 and reference is not None:
            return _dry_call(READ_TOOL, {"document": reference.group(1)}), 0.0
        return {"role": "assistant", "content": "(空跑)"}, 0.0

    async def judge(self, prompt: str) -> tuple[str, float]:
        verdict = {"claims": [], "refused": False, "matches_reference": "n/a"}
        return json.dumps({**verdict, "reason": "dry"}), 0.0


_DRY_REFERENCE = re.compile(r"\[(D[0-9]+#[0-9]+)\]")


def _dry_call(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    call = {
        "id": f"call_dry_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }
    return {"role": "assistant", "content": "", "tool_calls": [call]}
