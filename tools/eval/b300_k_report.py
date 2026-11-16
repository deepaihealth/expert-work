"""B-300 第 9 版的线、统计力说明与照实说明(设计 2026-11-15 §11.1 第 2 步、§11.3)。纯数据。

``b300_report`` 把 :data:`K_LINES` 转成 ``PassLine`` 并进 ``LINES``;``b300_outcomes`` 在报告的
``disclosures`` 里带上 :func:`disclosures`,在 ``statistical_power`` 里带上 :data:`POWER`。
"""

from __future__ import annotations

from b300_k_platform import ASSUMPTIONS

APPROVED_V9 = "已拍板(用户 11-16,重做设计第 9 版 §11.1 / §11.3)"

#: (类, 指标, 比法, 线, 应有的单元数, 逐领域的线)。``min`` = 做到的次数不少于;``max`` = 坏的次数
#: 不超过。``G-`` = 回归闸门(各 10 个单元);``ALL-K`` = K 组全部;``ALL`` = 全部提炼段。
K_LINES: tuple[tuple[str, str, str, float, int, float | None], ...] = (
    # §11.1 第 2 步(第 9 版):闸门多看的三样
    ("G-K-a", "retired_to_new", "min", 8, 10, None),
    ("G-K-b", "x_still_valid", "min", 9, 10, None),
    ("G-K-n2", "x_retired", "max", 0, 10, None),
    ("G-K-cp", "x_retired", "max", 0, 10, None),
    ("G-ALL", "write_guarantee_broken", "max", 0, 1, None),
    ("G-ALL", "reconcile_reply_error", "max", 0, 40, None),
    # §11.3 K 组
    ("K-a", "retired_to_new", "min", 24, 30, 4),
    ("K-a", "missed", "max", 3, 30, None),
    ("K-ap", "new_valid", "min", 24, 30, None),
    ("K-b", "x_still_valid", "min", 27, 30, None),
    ("K-b", "hedge_kept", "min", 24, 30, None),
    ("K-b2", "x_still_valid", "min", 27, 30, None),
    ("K-b2", "hedge_kept", "min", 24, 30, None),
    ("K-bp", "settled", "min", 24, 30, None),
    ("K-c", "x_retired", "max", 0, 30, None),
    ("K-c", "mislabelled", "max", 1, 30, None),
    ("K-cp", "x_retired", "max", 0, 30, None),
    ("K-n1", "x_retired", "max", 0, 30, None),
    ("K-n2", "x_retired", "max", 0, 30, None),
    ("K-e1", "retired_to_new", "min", 24, 30, None),
    ("K-e1", "duplicate_added", "max", 0, 30, None),
    ("K-e2", "user_stated_y_verbatim", "min", 15, 15, None),
    ("K-e2", "user_stated_y_reworded", "min", 12, 15, None),
    ("K-e2", "x_retired_ok", "min", 24, 30, None),
    ("K-f", "retired_to_new", "min", 24, 30, None),
    ("K-f", "stored_new", "min", 27, 30, None),
    ("K-g", "any_retired", "min", 27, 30, None),
    ("K-p", "half_kept", "min", 29, 30, None),
    ("K-p", "stored_new", "min", 27, 30, None),
    ("K-w", "user_stated_y_verbatim", "min", 15, 15, None),
    ("K-w", "user_stated_y_reworded", "min", 12, 15, None),
    ("K-w", "x_retired_ok", "min", 24, 30, None),
    ("K-t", "x_retired", "max", 0, 30, None),
    ("K-t", "stored_new", "min", 27, 30, None),
    ("K-late", "fuller_kept", "min", 15, 15, None),
    ("K-late", "later_valid", "min", 15, 15, None),
    ("K-s", "new_stored", "min", 13, 15, None),
    ("K-s", "summary_retired_directly", "max", 0, 15, None),
    ("K-s", "b_still_in_summary", "max", 2, 15, None),
    ("K-s", "ac_lost", "max", 0, 15, None),
    ("K-s", "rewrite_marked", "max", 0, 15, None),
    ("K-s", "summary_changed", "max", 0, 15, None),
    ("K-s", "synonym_added", "max", 2, 15, None),
    # 17 类 x 30 个单元各两条近邻(K-late 按设计不种)
    ("ALL-K", "neighbour_retired", "max", 0, 510, None),
    ("ALL", "write_guarantee_broken", "max", 0, 1, None),
    # 使用侧(每家每类 30 次 0 违规,同 B 组)
    ("B4c", "violation", "max", 0, 30, None),
    ("B4d", "violation", "max", 0, 30, None),
)

#: §11.3「第 9 版各条线的统计力」:线的形状 → 过线时真实比例最高可以到(单侧 95%, 双侧 95% 的
#: 上限),与用在哪。精确二项区间,数取自设计的表。
POWER: tuple[dict[str, str], ...] = (
    {
        "shape": "0 / 30",
        "one_sided": "约 10%",
        "two_sided": "约 12%",
        "used_by": "K-c、K-cp、K-n1、K-n2、K-t 的「旧值被退」",
    },
    {
        "shape": "0 / 15",
        "one_sided": "约 18%",
        "two_sided": "约 22%",
        "used_by": "K-cp 的每一半、K-late 的每一半、K-s 每一半里的 0 / 15",
    },
    {
        "shape": "0 / 10",
        "one_sided": "约 26%",
        "two_sided": "约 31%",
        "used_by": "回归闸门里的 K-n2、K-cp",
    },
    {
        "shape": "≤ 1 / 30",
        "one_sided": "约 15%",
        "two_sided": "约 17%",
        "used_by": "S5、S7(加到 30 次后)、K-c 的误标、K-p 的「丢失」",
    },
    {
        "shape": "≤ 1 / 15",
        "one_sided": "约 28%",
        "two_sided": "约 32%",
        "used_by": "S7 原来的规模 —— 所以加到 30",
    },
    {
        "shape": "≥ 27 / 30(坏的 ≤ 3)",
        "one_sided": "约 24%",
        "two_sided": "约 27%",
        "used_by": "K-b、K-b2 的「旧值仍有效」等",
    },
    {
        "shape": "≥ 24 / 30(坏的 ≤ 6)",
        "one_sided": "约 36%",
        "two_sided": "约 39%",
        "used_by": "各「该退的退了」的线",
    },
)

_NOTES: tuple[str, ...] = (
    "第 9 版的线挡得住明显的问题,挡不住一成以内的偶发(各线的统计力见报告的 statistical_power)。"
    "没有程序兜底、只靠模型的三处是 K-b(整句犹豫)、K-n2(不是在陈述的整句)、K-p(否认半句):"
    "它们的线哪怕过了,线上仍可能有一两成的坏例(§11.3)。",
    "K 组的场景:18 类 x 10 个(5 个领域各 2 个,中英各 5),同 10 组内容只换新说法的来路;写的时候"
    "没有读设计附录 A / B / C / E 给模型的指令与例子。类名在脚本里只用 ASCII:K-ap = K-a′、"  # noqa: RUF001
    "K-bp = K-b′、K-cp = K-c′。",  # noqa: RUF001
    "K-c′、K-n1 / K-n2、K-e2 / K-w、K-late、K-s 分开报的半边 / 形状在 counts 里(x_retired_<变体>、"  # noqa: RUF001
    "units_<变体> 等);K-n 的四种形状按场景序号轮换,10 个场景里是 3 / 3 / 2 / 2,不是各 7.5 个单元。",
    "近邻:除 K-late(设计写明不种)外每个单元两条,共 17 x 30 x 2 = 1,020 条;设计 §11.3 写的"
    "「900 条」是加 K-n2、K-late、K-s 之前的数。「模型列了、被程序挡下」按「回复里列了、那一行"
    "写回后仍有效」数(counts 的 listed_blocked / listed_blocked_on_neighbour),不读进程级计数。",
    "K-a 里内容形态是数字的 2 个场景给旧值带了事件日期(「带日期的碰上不带日期的」,§11.2a 末);"
    "这 6 个单元照样进 K-a 的线,另在 counts 里单报(dated_retired / dated_coexists)。",
    "K-e2 / K-w「逐字相同必须全中」的前提是提炼写出的正文与种下的那条一字不差,这由模型决定;"
    "counts 的 verbatim_extracted 报这个前提成立了几次,不成立的那几次这条线量的是模型而不是程序。",
    "写的保证只查进了去重合并模型调用的用户一方候选(材料里看得到的那些);写入扫描拦下的在本机"
    "认不出来,会被算成不成立 —— 出现时要人工看那一条。被测代码不是第 9 版时报 incomplete。",
    "K-s 的「直接作废」「打了标」读的是写回落完库、当场重写还没开始那一刻的汇总行;要重写之后才"
    "量得到的两条(B 仍在 / A 或 C 丢失)在没有重写入口的代码上报 incomplete。",
    "回归闸门的 K 段(gate-k)是 K-a、K-b、K-n2、K-c′ 的 10 个场景各跑 1 次;0 / 10 只能说明真实"  # noqa: RUF001
    "比例大致低于 26%(§11.1)。ReconcileReplyError 按出错记录里的异常名数;legacy_op 只报数。",
    "使用侧 B4c / B4d:B4c 种一条控制台更正过的 X(置信度 1.0)和一条更晚的、用户说的 Y;B4d 种"
    "一条含 X 的汇总行(连同三条成员)和一条连着它的、更晚的用户说的 Y,汇总行由存储的 supersede"
    "(flag_summary_ids)打上重写标记 —— 就是写回点到它、重写还没发生的那个状态。违规 = 只按 X "
    "回答、不提 Y。",
)


def disclosures() -> tuple[str, ...]:
    """报告里第 9 版的照实说明 + 对代码侧接口的假设清单。"""
    return (*_NOTES, *(f"接口假设 {a}" for a in ASSUMPTIONS))
