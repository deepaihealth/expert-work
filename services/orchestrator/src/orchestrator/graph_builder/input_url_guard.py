"""B-67 §七 —— 手抄守卫:沙箱代码里出现本轮输入 URL 的原文或近似,拦下并告知正确用法。

全是纯函数,不碰 IO;接线在 ``builder.tools_node``(派发前)。为什么不放
``before_tool_dispatch`` 中间件:它的 payload 只有 ``tool_name / tool_args``,拿不到本轮
inputs;``tools_node`` 里 ``_fill_bound_args`` 已经在读 ``configurable[PROMPT_INPUTS_KEY]``,
守卫放同一处。

判定(spec §7.1):从代码里抽 URL 字面量;与候选集比 —— 完全一致命中;同 scheme+host 且
host 之后的部分编辑距离 ≤ 3 命中(事故里距离是 1)。抄对了也拦:这次对不代表下次对。
候选集与预拉 / 渲染同一个 walker(``inputs_doc.linked_sites``),所以提示里说的链接名
就是真实存在的那个。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from orchestrator.tools.inputs_doc import linked_sites

#: 代码里的 URL 字面量:到空白 / 引号 / 反引号 / 尖括号 / 右括号 / 右方括号为止。
URL_RE = re.compile(r"""https?://[^\s'"`<>)\]]+""")
#: 「多一位 / 少一位 / 改一字」都在 3 以内;真栈验收的读数出来后再定(spec §十四)。
MAX_EDIT_DISTANCE = 3
#: 只比 host 之后不超过这么长的串(编辑距离是 O(n·m),这是 CPU 上限)。
MAX_COMPARE_CHARS = 2048


@dataclass(frozen=True)
class UrlCandidate:
    var_name: str
    url: str
    #: run 目录里的链接名(``inputs_doc.link_names``),提示模型改用它。
    link: str


@dataclass(frozen=True)
class GuardHit:
    var_name: str
    link: str
    distance: int
    #: 模型自己写的那串 —— 只进回给模型的 ToolMessage,不进日志、不进审计。
    written: str


def candidates_from_inputs(inputs: Mapping[str, Any]) -> tuple[UrlCandidate, ...]:
    """本轮 inputs 里所有 URL site(含 §4.3 的 JSON 字符串形态),带链接名。"""
    return tuple(
        UrlCandidate(var_name=linked.site.var_name, url=linked.site.url, link=linked.link)
        for name, value in inputs.items()
        for linked in linked_sites(name, value)
    )


def levenshtein(a: str, b: str, *, cap: int) -> int:
    """经典 DP;只关心 ≤ ``cap`` 的距离,超过就返回 ``cap + 1``(长度差先短路,行最小值再短路)。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1] if prev[-1] <= cap else cap + 1


def _split(url: str) -> tuple[str, str]:
    """``(scheme://host 小写, host 之后的全部)`` —— 裁定 4:query 也在比较范围内。"""
    parsed = urlparse(url)
    head_len = len(parsed.scheme) + 3 + len(parsed.netloc)
    return f"{parsed.scheme}://{parsed.netloc}".lower(), url[head_len:]


def find_retyped_url(code: str, candidates: Sequence[UrlCandidate]) -> GuardHit | None:
    """代码里第一处「完全一致」的命中优先;都不一致时取最小编辑距离的那一处;没有则 ``None``。"""
    if not candidates:
        return None
    best: GuardHit | None = None
    for written in URL_RE.findall(code):
        exact = next((c for c in candidates if c.url == written), None)
        if exact is not None:
            return GuardHit(var_name=exact.var_name, link=exact.link, distance=0, written=written)
        host, tail = _split(written)
        if len(tail) > MAX_COMPARE_CHARS:
            continue
        for cand in candidates:
            cand_host, cand_tail = _split(cand.url)
            if cand_host != host:
                continue
            distance = levenshtein(tail, cand_tail, cap=MAX_EDIT_DISTANCE)
            if distance <= MAX_EDIT_DISTANCE and (best is None or distance < best.distance):
                best = GuardHit(
                    var_name=cand.var_name, link=cand.link, distance=distance, written=written
                )
    return best


def guard_message(hit: GuardHit) -> str:
    """回给模型的那句话 —— 它就是最准时的提醒(spec §6.3 否决每轮提醒的理由)。"""
    verdict = "与输入一致" if hit.distance == 0 else f"疑似抄错 {hit.distance} 处"
    return (
        f"[blocked] 代码里的地址 {hit.written} 是输入 {hit.var_name} 的手抄件"
        f"（平台比对：{verdict}）。"  # noqa: RUF001 — 面向模型的中文全角标点
        f"这个文件应在 $EXPERT_WORK_INPUTS_DIR/{hit.link}（不在则按清单里的原地址下载）；"  # noqa: RUF001
        f"请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {hit.var_name} 的原地址，"  # noqa: RUF001
        "不要手抄。"
    )
