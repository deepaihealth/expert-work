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

#: 代码里的 URL 字面量:到空白 / 引号 / 反引号 / 尖括号 / 右括号 / 右方括号 / 中文全角
#: 标点(常跟在裸 URL 后面的句读)为止;scheme 不区分大小写(``HTTPS://`` 一样抽得出来)。
#: 不排除 CJK 字:URL 路径里未编码的中文文件名是真实存在的输入。
URL_RE = re.compile(
    r"""https?://[^\s'"`<>)\]，。；：！？、）】」』]+""",  # noqa: RUF001 — 面向中文文本的全角标点
    re.IGNORECASE,
)
#: 「多一位 / 少一位 / 改一字」都在 3 以内;真栈验收的读数出来后再定(spec §十四)。
MAX_EDIT_DISTANCE = 3
#: 只比 host 之后不超过这么长的串:两个 run API 都把字符串输入截到 8192 字符,更长的
#: URL 不会是真实输入。编辑距离按 Ukkonen 对角线带宽裁剪(见 :func:`levenshtein`),
#: 不再是 O(n·m) 的全矩阵,所以这个上限只挡「不可能是真输入」的串,不是 CPU 顾虑。
MAX_COMPARE_CHARS = 8192


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
    """只关心 ≤ ``cap`` 的距离,超过就返回 ``cap + 1``:长度差先短路;其余按 Ukkonen 对角线
    带宽裁剪 —— 任何编辑距离 ≤ ``cap`` 的对齐路径都不会偏离主对角线超过 ``cap`` 格,所以只算
    ``|i - j| <= cap`` 的格子就够了(行最小值仍然是行内的早退)。复杂度 O(len · (2·cap+1)),
    不再是全矩阵的 O(len(a)·len(b)) —— 全矩阵版本在几千字符的串上单次就要跑到十秒量级
    (fix round 1 实测,见 report),签名 URL 的 query 串常有这个长度。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    over = cap + 1
    len_b = len(b)
    # 每行只保留带宽内的一段,用 (起始列, 这段值的列表) 表示,查带宽外的列一律按 over 处理
    # (带宽定理保证它们不会更优)。列表下标而非 dict,常数因子更小 —— 8000 字符的尾串量级
    # 上,dict 版本单次比较要几毫秒,20x20 的最坏情形(尾串几乎相同)会堆到秒级。
    prev_lo = 0
    prev: list[int] = list(range(min(cap, len_b) + 1))
    for i, ca in enumerate(a, 1):
        lo, hi = max(0, i - cap), min(len_b, i + cap)
        cur: list[int] = []
        row_min = over
        for j in range(lo, hi + 1):
            up = prev[j - prev_lo] + 1 if prev_lo <= j < prev_lo + len(prev) else over
            # j == 0 是矩阵的真实边界(没有 b[-1],左邻 / 对角都不存在);j == lo 只是带宽窗口
            # 的左沿,若 lo > 0 那是带宽裁剪出来的,上一行的对角格仍可能落在带宽里,不能跳过。
            if j == 0:
                val = up
            else:
                left = cur[-1] + 1 if cur else over
                d_idx = j - 1 - prev_lo
                diag = prev[d_idx] + (ca != b[j - 1]) if 0 <= d_idx < len(prev) else over
                val = min(up, left, diag)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > cap:
            return over
        prev, prev_lo = cur, lo
    idx = len_b - prev_lo
    result = prev[idx] if 0 <= idx < len(prev) else over
    return result if result <= cap else over


def _split(url: str) -> tuple[str, str]:
    """``(scheme://host 小写, host 之后的全部)`` —— 裁定 4:query 也在比较范围内。"""
    parsed = urlparse(url)
    head_len = len(parsed.scheme) + 3 + len(parsed.netloc)
    return f"{parsed.scheme}://{parsed.netloc}".lower(), url[head_len:]


def find_retyped_url(code: str, candidates: Sequence[UrlCandidate]) -> GuardHit | None:
    """代码里第一处「完全一致」的命中优先;都不一致时取最小编辑距离的那一处;没有则 ``None``。"""
    if not candidates:
        return None
    # 每个候选的 (host, tail) 只算一次:同一批候选要跟代码里每一处 URL 字面量比,搬到
    # 外层省掉重复 urlparse。
    cand_splits = [(cand, *_split(cand.url)) for cand in candidates]
    best: GuardHit | None = None
    for written in URL_RE.findall(code):
        exact = next((c for c in candidates if c.url == written), None)
        if exact is not None:
            return GuardHit(var_name=exact.var_name, link=exact.link, distance=0, written=written)
        host, tail = _split(written)
        if len(tail) > MAX_COMPARE_CHARS:
            continue
        for cand, cand_host, cand_tail in cand_splits:
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
