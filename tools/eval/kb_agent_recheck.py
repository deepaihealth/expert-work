"""难负例答前复核(B-206 11-10):删句只删得掉锚那一句,答案常以别的措辞、在别的文档或表格行里
还留在库里(11-10 诊断:30 道里 26 道),智能体找到了照答,却被当成「资料没写」的题判。

做法(参考 CRUMQs arXiv 2510.11956:删完拿题目去库里取前 k 块,让 LLM 确认答不了才保留):
删过句的库建好之后、答题之前,每道难负例用**与答题同一条检索**(``knowledge_search`` 工具本身,
``limit`` = k,默认 20)取前 k 段,交给判分模型判「这些段落能不能答出这道题(给参考答案作对照)」,
每题判 ``votes`` 次取严格多数。判为能答的题记 :data:`kb_agent_judge.EXCLUDED`:不答题、
不进任何分母,统计里单列题号。判分回复解析不了的那一票不算「能答」(题照常答),也不进缓存。
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent))

from kb_agent_cost import Spend  # type: ignore[import-not-found]
from kb_agent_judge import EXCLUDED, JudgeCache, parse_verdict  # type: ignore[import-not-found]
from kb_dataset import KbCase  # type: ignore[import-not-found]

from orchestrator.tools.registry import ToolContext, ToolResult, ToolSpec

#: 每道难负例复核时取前几段;``knowledge_search`` 的 ``limit`` 上限也是 20。
DEFAULT_RECHECK_K = 20
MAX_RECHECK_K = 20

RECHECK_TEMPLATE = """判断下面这些从公司知识库检索到的资料段落,能不能回答用户的问题。

【用户问题】
{question}

【参考答案】(这道题原本的答案,只用来对照)
{reference}

【检索到的资料段落】
{passages}

只按资料段落判断,不看你自己的知识:
- 段落里写了参考答案的要点(措辞不同、在别的文档里、在表格的某一行里都算)→ "answerable": true;
- 段落里只有相关内容,得不出参考答案的要点 → "answerable": false。

只输出 JSON,格式:{{"answerable": true, "reason": "一句话中文理由"}}
"""


class _Search(Protocol):
    @property
    def spec(self) -> ToolSpec:
        """工具声明。"""

    async def call(self, args: Mapping[str, Any], *, ctx: ToolContext) -> ToolResult:
        """执行一次检索。"""


class _Judge(Protocol):
    @property
    def judge_model(self) -> str:
        """判分模型名。"""

    async def judge(self, prompt: str) -> tuple[str, float]:
        """判分模型的原始回复与花费(元)。"""


def recheck_prompt(question: str, reference: str, passages: str) -> str:
    return RECHECK_TEMPLATE.format(
        question=question,
        reference=reference or "(未提供)",
        passages=passages or "(没有检索到段落)",
    )


def parse_recheck(raw: str) -> bool | None:
    """``answerable`` 布尔;解析不了 / 不是布尔时 None。"""
    value = parse_verdict(raw).get("answerable")
    return value if isinstance(value, bool) else None


async def _passages(search: _Search, question: str, *, k: int, tenant_id: UUID) -> str:
    try:
        result = await search.call(
            {"query": question, "limit": k}, ctx=ToolContext(tenant_id=tenant_id)
        )
    except Exception as exc:  # 检索报错:没有段落可判,照「答不了」处理并记下原因
        print(f"WARNING recheck search failed for {question!r}: {exc}", file=sys.stderr)
        return ""
    return str(result.content or "")


async def recheck_hard_negatives(
    *,
    cases: Sequence[KbCase],
    search: _Search,
    tenant_id: UUID,
    models: _Judge,
    cache: JudgeCache,
    references: Mapping[str, str],
    spend: Spend,
    votes: int = 1,
    k: int = DEFAULT_RECHECK_K,
    cache_verdicts: bool = True,
) -> dict[str, dict[str, Any]]:
    """题号 → ``{"excluded", "votes", "cost_cny", "k"}``;``excluded`` = 严格多数票判为能答。"""
    out: dict[str, dict[str, Any]] = {}
    for case in cases:
        passages = await _passages(search, case.query, k=k, tenant_id=tenant_id)
        prompt = recheck_prompt(case.query, references.get(case.id, ""), passages)
        answers: list[bool | None] = []
        cost = 0.0
        for vote in range(max(1, votes)):
            raw = cache.get(models.judge_model, prompt, vote=vote)
            if raw is None:
                raw, call_cny = await models.judge(prompt)
                cost += call_cny
                spend.judge_cny += call_cny
                spend.judge_calls += 1
                if cache_verdicts and parse_recheck(raw) is not None:
                    cache.put(models.judge_model, prompt, raw, vote=vote)
            answers.append(parse_recheck(raw))
        yes = sum(1 for a in answers if a is True)
        out[case.id] = {
            "excluded": yes * 2 > len(answers),
            "votes": answers,
            "cost_cny": round(cost, 6),
            "k": k,
        }
    return out


def excluded_row(
    label: str, case: KbCase, check: Mapping[str, Any], *, meta: Mapping[str, Any]
) -> dict[str, Any]:
    """复核剔除的题落盘的那一行:不答题、不判分;``meta`` = 模型 / 尺子 / 票数 / 轮次这些
    续跑守卫要核的字段。"""
    votes = list(check["votes"])
    yes = sum(1 for v in votes if v is True)
    return {
        "case_id": case.id,
        "set": label,
        "answerable": False,
        "label": case.answer_label,
        "question": case.query,
        "answer": None,
        "searches": [],
        "reads": [],
        "verdict": {"recheck": dict(check)},
        "votes": votes,
        "agreement": f"{yes}/{len(votes)}",
        "category": EXCLUDED,
        **meta,
        "cost_cny": {"answer": 0.0, "judge": float(check["cost_cny"])},
    }


def recheck_references(
    deleted: Mapping[str, Sequence[str]], answers: Mapping[str, str | None]
) -> dict[str, str]:
    """难负例题号 → 给复核对照的参考答案:原题的文字答案(有的话)+ 被删掉的原文。"""
    out: dict[str, str] = {}
    for case_id, anchors in deleted.items():
        answer = answers.get(case_id)
        parts = [answer] if answer else []
        out[case_id] = "\n".join([*parts, *(f"原文:{a}" for a in anchors)])
    return out


def write_excluded(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


__all__ = [
    "DEFAULT_RECHECK_K",
    "MAX_RECHECK_K",
    "excluded_row",
    "parse_recheck",
    "recheck_hard_negatives",
    "recheck_prompt",
    "recheck_references",
    "write_excluded",
]
