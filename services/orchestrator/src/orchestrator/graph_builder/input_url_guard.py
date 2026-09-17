"""B-67 §七 —— 手抄守卫:沙箱代码里出现本轮输入 URL 的原文或近似,拦下并告知正确用法。

判定是纯函数;唯一的副作用是比较预算用完时的一行日志(只有计数)。接线在
``builder.tools_node``(派发前)。为什么不放 ``before_tool_dispatch`` 中间件:它的 payload
只有 ``tool_name / tool_args``,拿不到本轮 inputs;``tools_node`` 里 ``_fill_bound_args``
已经在读 ``configurable[PROMPT_INPUTS_KEY]``,守卫放同一处。

判定(spec §7.1,P25 收窄):

* **完全一致**:代码里出现候选 URL 的原文,且原文后面紧跟的不是 URL 的延续(只隔着
  ASCII 句读 ``.,;:!?)`` 也算)→ 命中。按子串找,不依赖字面量抽取 —— 路径里带全角标点
  或括号的 URL 会被 :data:`URL_RE` 截断。scheme / host 只差大小写也算完全一致。
* **近似**:从代码里抽 URL 字面量(去掉尾随的 ASCII 句读),与同 scheme+host 的候选比
  host 之后的全部(裁定 4:含 query / fragment)。允许的编辑距离随**候选**尾串长度走:
  ``min(3, 尾串长度 // 16)`` —— 短于 16 字符的尾串只拦完全一致(``/v1`` 与 ``/v2``、
  ``img_0.png`` 与 ``img_7.png`` 本来就是不同的地址)。
* 先找完全一致;近似按字面量顺序,**第一个**有命中的字面量就返回(同一字面量里取最小
  距离)。进 DP 之前先过两道便宜的下界(长度差、字符多重集差);DP 按格子数计预算,
  用完就停、已找到的照常返回(失败放行)。

抄对了也拦:这次对不代表下次对。候选集与预拉 / 渲染同一个 walker
(``inputs_doc.linked_sites``),所以提示里说的链接名就是真实存在的那个。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orchestrator.tools.inputs_doc import linked_sites

logger = logging.getLogger(__name__)

#: URL 字面量的终止字符:空白 / 引号 / 反引号 / 尖括号 / 右括号 / 右方括号 / 中文全角标点
#: (常跟在裸 URL 后面的句读)。不排除 CJK 字:URL 路径里未编码的中文文件名是真实存在的输入。
_URL_STOP = r"""\s'"`<>)\]，。；：！？、）】」』"""  # noqa: RUF001 — 面向中文文本的全角标点
#: 代码里的 URL 字面量;scheme 不区分大小写(``HTTPS://`` 一样抽得出来)。
URL_RE = re.compile(rf"https?://[^{_URL_STOP}]+", re.IGNORECASE)
#: 字面量尾随的 ASCII 句读(``curl URL;``、``[URL, x]``、句末的 ``.``)不是地址的一部分。
#: ``/`` 不在里面:它是路径的一部分。
_TRAILING_PUNCT = ".,;:!?"
#: 候选原文在代码里出现之后,「这一串到此为止」的判定:跳过 ASCII 句读与右括号后,
#: 是字面量终止字符或代码结尾。``https://x/a`` 出现在 ``https://x/a.png`` 里不算完全一致。
_EXACT_END_RE = re.compile(rf"[.,;:!?)]*(?:[{_URL_STOP}]|\Z)")
#: ``(scheme://host, host 之后的全部)``;不用 ``urlparse``:它对 ``https://[^/`` 这类正则
#: 片段、host 里的全角斜杠 / 全角 at 符号会抛 ``ValueError``(C1)。
_SPLIT_RE = re.compile(r"([^:/?#]+://[^/?#]*)(.*)", re.DOTALL)
#: 「多一位 / 少一位 / 改一字」都在 3 以内;真栈验收的读数出来后再定(spec §十四)。
MAX_EDIT_DISTANCE = 3
#: P25 —— 候选尾串每这么多字符允许 1 处编辑(上限 :data:`MAX_EDIT_DISTANCE`)。
CHARS_PER_EDIT = 16
#: 只比 host 之后不超过这么长的串:两个 run API 都把字符串输入截到 8192 字符,更长的
#: URL 不会是真实输入。编辑距离按 Ukkonen 对角线带宽裁剪(见 :func:`levenshtein`),
#: 不再是 O(n·m) 的全矩阵,所以这个上限只挡「不可能是真输入」的串,不是 CPU 顾虑。
MAX_COMPARE_CHARS = 8192
#: 每次调用近似比较的预算,按 DP 格子数的上界(尾串长度乘带宽)累计。实测带宽 DP 每秒
#: 几百万格,这个数把一次调用的最坏耗时压在亚秒级;按次数计不行 —— 一次 8000 字符的
#: 比较与一次 100 字符的比较差两个数量级。
MAX_DP_CELLS = 2_000_000


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
    """``(scheme://host 小写, host 之后的全部)`` —— 裁定 4:query 也在比较范围内。永不抛。"""
    match = _SPLIT_RE.match(url)
    if match is None:
        return "", url
    return match.group(1).lower(), match.group(2)


def _allowed_distance(tail: str) -> int:
    """P25 —— 候选尾串允许的编辑距离:``min(3, 长度 // 16)``;0 表示只认完全一致。"""
    return min(MAX_EDIT_DISTANCE, len(tail) // CHARS_PER_EDIT)


def _bag_bound(a: Counter[str], b: Counter[str]) -> int:
    """编辑距离的下界:两边字符多重集之差。一次替换让两个方向各差 1,插入 / 删除只让一个
    方向差 1,所以任一方向的差都不超过编辑距离。"""
    return max(sum((a - b).values()), sum((b - a).values()))


@dataclass(frozen=True)
class _Prepared:
    cand: UrlCandidate
    host: str
    tail: str
    cap: int
    bag: Counter[str]


def _prepare(cand: UrlCandidate) -> _Prepared:
    host, tail = _split(cand.url)
    cap = _allowed_distance(tail)
    return _Prepared(cand, host, tail, cap, Counter(tail) if cap else Counter())


def _exact_copy(code: str, candidates: Sequence[UrlCandidate]) -> GuardHit | None:
    """候选原文在代码里出现、且后面不是 URL 的延续 → 完全一致。按子串找,见模块说明。"""
    for cand in candidates:
        start = code.find(cand.url)
        while start != -1:
            if _EXACT_END_RE.match(code, start + len(cand.url)):
                return GuardHit(
                    var_name=cand.var_name, link=cand.link, distance=0, written=cand.url
                )
            start = code.find(cand.url, start + 1)
    return None


def find_retyped_url(code: str, candidates: Sequence[UrlCandidate]) -> GuardHit | None:
    """完全一致优先;否则第一个有近似命中的字面量(取其最小距离);没有则 ``None``。"""
    if not candidates:
        return None
    exact = _exact_copy(code, candidates)
    if exact is not None:
        return exact
    literals: list[tuple[str, str, str]] = []
    for raw in URL_RE.findall(code):
        written = raw.rstrip(_TRAILING_PUNCT)
        literals.append((written, *_split(written)))
    prepared = [_prepare(cand) for cand in candidates]
    keyed = {(p.host, p.tail): p.cand for p in reversed(prepared)}
    for written, host, tail in literals:
        # scheme / host 只差大小写:仍是原文照抄。
        same = keyed.get((host, tail))
        if same is not None:
            return GuardHit(var_name=same.var_name, link=same.link, distance=0, written=written)
    return _near_copy(literals, [p for p in prepared if p.cap > 0])


def _near_copy(
    literals: Sequence[tuple[str, str, str]], prepared: Sequence[_Prepared]
) -> GuardHit | None:
    cells = 0
    dp_runs = 0
    for written, host, tail in literals:
        if len(tail) > MAX_COMPARE_CHARS:
            continue
        best: GuardHit | None = None
        bag: Counter[str] | None = None
        for p in prepared:
            if p.host != host or abs(len(tail) - len(p.tail)) > p.cap:
                continue
            if bag is None:
                bag = Counter(tail)
            if _bag_bound(bag, p.bag) > p.cap:
                continue
            cells += len(tail) * (2 * p.cap + 1)
            if cells > MAX_DP_CELLS:
                logger.warning(
                    "tools.input_url_guard_budget_exhausted literals=%d candidates=%d dp_runs=%d",
                    len(literals),
                    len(prepared),
                    dp_runs,
                )
                return best
            dp_runs += 1
            distance = levenshtein(tail, p.tail, cap=p.cap)
            if distance <= p.cap and (best is None or distance < best.distance):
                best = GuardHit(
                    var_name=p.cand.var_name, link=p.cand.link, distance=distance, written=written
                )
                if distance == 1:
                    break
        if best is not None:
            return best
    return None


def guard_message(hit: GuardHit, *, trusted: bool) -> str:
    """回给模型的那句话 —— 它就是最准时的提醒(spec §6.3 否决每轮提醒的理由)。

    ``trusted=False``:这条消息是平台合成的,不过 spotlight 围栏(只有真工具输出过);而
    链接名(列表项说明 / dict 键的 slug、URL 后缀)与模型抄的那串都带租户数据 —— 两样都
    不写,只说目录与清单。

    近似命中(P25)可能真是另一个地址:给出路 —— 那就让代码从它自己的来源取得。
    """
    near = hit.distance > 0
    if trusted:
        subject = f"代码里的地址 {hit.written} "
        where = (
            f"这个文件应在 $EXPERT_WORK_INPUTS_DIR/{hit.link}（不在则按清单里的原地址下载）；"  # noqa: RUF001
            f"请改用它，或用代码从 $EXPERT_WORK_INPUTS 清单里读 {hit.var_name} 的原地址，"  # noqa: RUF001
            "不要手抄。"
        )
    else:
        subject = "代码里有一处地址"
        where = (
            "这个文件应在 $EXPERT_WORK_INPUTS_DIR 下，确切文件名见 $EXPERT_WORK_INPUTS 清单里"  # noqa: RUF001
            f" {hit.var_name} 对应条目的 local_path（不在则按清单里的原地址下载）；"  # noqa: RUF001
            "请用代码从清单里读路径或原地址，不要手抄。"  # noqa: RUF001
        )
    if not near:
        return (
            f"[blocked] {subject}是输入 {hit.var_name} 的手抄件"
            f"（平台比对：与输入一致）。{where}"  # noqa: RUF001
        )
    return (
        f"[blocked] {subject}像是输入 {hit.var_name} 的地址手抄出来的"
        f"（平台比对：疑似抄错 {hit.distance} 处）。若是它：{where}"  # noqa: RUF001
        "如果它确实是另一个地址，请让代码从它自己的来源取得（读文件、接口返回或上一步的输出），"  # noqa: RUF001
        "不要在代码里手写这串地址。"
    )
