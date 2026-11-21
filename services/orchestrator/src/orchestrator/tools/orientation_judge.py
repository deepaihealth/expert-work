"""B-301 的两步问方向,抽成可复用的函数(B-322 设计 §9 PR ①)。

两个调用方:

* ``ask_image``(B-301,:meth:`orchestrator.tools.vision.AskImageTool._ask_orientation`):候选图
  写进本 run 的裁图目录,按 ref 问看图模型;行为与抽取之前逐字节相同。
* 主模型自己看图时的转正(B-322 PR ①,:mod:`orchestrator.graph_builder.image_upright`):候选图
  在内存里生成,问主模型自己的「关思考」路由。

本模块只管两步的次序、每次调用前现算的时间、候选图失败与没时间时下什么结论;「怎么得到一张候选图」
(``candidate``)与「怎么问一次」(``ask``)由调用方给。结论照 B-301 的口径(:class:`Orientation`),
B-322 另按回答重新归类(:func:`~orchestrator.tools.image_orientation.classify_main_model_answers`)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from orchestrator.tools.error_classifier import ExpectedRefusalError
from orchestrator.tools.image_orientation import (
    TURNS,
    Answer,
    Orientation,
    first_conclusion,
    turned_conclusion,
)

logger = logging.getLogger(__name__)


class CandidateDeferredError(Exception):
    """``candidate`` 抛它 = 调用方决定这张图**这一步不判**(B-322 复审第五轮 M2:借出的候选到了全进程
    上限)。原样穿出 :func:`judge_orientation`,不当成「候选生成不出来」、不记日志;调用方自己处理。
    ``ask_image`` 从不抛它,所以它的行为不变。"""


async def judge_orientation[C](
    *,
    candidate: Callable[[int], Awaitable[C]],
    ask: Callable[[C, float], Awaitable[tuple[Answer, str | None]]],
    budget: Callable[[float], float | None],
    first: tuple[Answer, str | None] | None = None,
    log_prefix: str = "ask_image",
) -> Orientation:
    """B-301 设计 §4.1 的两步。没时间、0 度候选生成不出来、第 1 步没答成 = 没问成;第 1 步答 no
    之后没时间问第 2 步也是没问成。

    * ``candidate(角度)``:一张顺时针转了这么多度的候选(ref / 内容块,随调用方);抛错 = 生成不出来
      (:class:`CandidateDeferredError` 除外:原样往外抛)。
    * ``ask(候选, 最多等多少秒)`` → ``(回答, 应答的模型)``;从不往外抛(没答成就是 ``timeout`` /
      ``error``)。
    * ``budget(还要花的秒数)``:此刻再问一次最多等多久;``None`` = 不问了。

    ``first``:第 1 步已有的回答(B-322 的 pending 再判只重问第 2 步;第 1 步不重问、不生成 0 度候选)。

    时间每次调用前现算:生成候选不设限时(截断它会留下半张 JPEG),但它用掉的时间算数,生成完已经
    过了线就不再问。"""
    copy_s = 0.0
    if first is None:
        if budget(0.0) is None:
            return Orientation("not_asked", reason="deadline")
        started = time.monotonic()
        try:
            zero = await candidate(0)
        except CandidateDeferredError:
            raise
        except ExpectedRefusalError:
            # B-332 —— 图太大、宿主不裁:预期之内,不是异常,不打堆栈。
            logger.info("%s.orientation_candidate_refused", log_prefix)
            return Orientation("not_asked", reason="no_copy")
        except Exception:
            logger.warning("%s.orientation_candidate_failed", log_prefix, exc_info=True)
            return Orientation("not_asked", reason="no_copy")
        copy_s = time.monotonic() - started
        first_budget = budget(0.0)
        if first_budget is None:
            return Orientation("not_asked", reason="deadline")
        first = await ask(zero, first_budget)
    first_answer, first_model = first
    answers: dict[str, Answer] = {"0": first_answer}
    models: dict[str, str | None] = {"0": first_model}
    conclusion = first_conclusion(first_answer)
    if conclusion == "not_asked":
        return Orientation(conclusion, 0, answers, models, "no_answer", extra_calls=1)
    if conclusion is not None:
        return Orientation(conclusion, 0, answers, models, extra_calls=1)
    out_of_time = Orientation("not_asked", 0, answers, models, "deadline", extra_calls=1)
    turned: list[C] = []
    for index, turn in enumerate(TURNS):
        # 生成之前先估:剩的时间要够「下限 + 还要生成的张数 乘 到目前为止最慢的一张」(第一次估用
        # 0 度那张),不够就不生成了 —— 生成到一半才发现,吃掉的是留给读的时间。
        needed = (len(TURNS) - index) * copy_s
        if budget(needed) is None:
            return out_of_time
        started = time.monotonic()
        try:
            turned.append(await candidate(turn))
        except CandidateDeferredError:
            raise
        except ExpectedRefusalError:
            logger.info("%s.orientation_candidate_refused", log_prefix)
            return Orientation("undetermined", 0, answers, models, extra_calls=1)
        except Exception:
            logger.warning("%s.orientation_candidate_failed", log_prefix, exc_info=True)
            return Orientation("undetermined", 0, answers, models, extra_calls=1)
        copy_s = max(copy_s, time.monotonic() - started)
    turned_budget = budget(0.0)
    if turned_budget is None:
        return out_of_time
    replies = await asyncio.gather(*(ask(item, turned_budget) for item in turned))
    by_turn: dict[int, Answer] = {}
    for turn, (reply, model) in zip(TURNS, replies, strict=True):
        by_turn[turn] = reply
        answers[str(turn)] = reply
        models[str(turn)] = model
    conclusion, degrees = turned_conclusion(by_turn)
    return Orientation(conclusion, degrees, answers, models, extra_calls=1 + len(TURNS))
