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
    ("G-K-a", "retired_to_new", "min", 6, 8, None),
    ("G-K-b", "x_still_valid", "min", 9, 10, None),
    ("G-K-n2", "x_retired", "max", 0, 10, None),
    ("G-K-cp", "x_retired", "max", 0, 10, None),
    ("G-ALL", "write_guarantee_broken", "max", 0, 1, None),
    # 闸门里全部 100 个单元(60 个 P6 / P7 + 40 个 K),与写的保证同口径(审查 M2)
    ("G-ALL", "reconcile_reply_error", "max", 0, 100, None),
    # §11.3 K 组
    # 用户 11-16:带事件日期的 2 个场景(6 个单元)剔出 K-a 的线、单独报数;线按原 24 / 30 同比例,
    # 三个数(19 / 24、漏记 ≤ 2 / 24、闸门 ≥ 6 / 8)与逐领域三分之二都由用户 11-16 确认
    ("K-a", "retired_to_new", "min", 19, 24, None),
    ("K-a", "missed", "max", 2, 24, None),
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

#: 各领域单元数不一样的 ``min`` 线:每个领域做到的比例不低于它。K-a 原来是「逐领域 ≥ 4 / 6」;
#: 剔出带事件日期的场景之后有两个领域只剩 3 个单元,按同一个比例(6 个里 4 个、3 个里 2 个)。
PER_DOMAIN_SHARE: dict[tuple[str, str], float] = {("K-a", "retired_to_new"): 2 / 3}

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
    {
        "shape": "≥ 19 / 24(坏的 ≤ 5)",
        "one_sided": "约 39%",
        "two_sided": "约 42%",
        "used_by": "K-a 新值有效、旧值作废(用户 11-16)",
    },
    {
        "shape": "≤ 2 / 24",
        "one_sided": "约 24%",
        "two_sided": "约 27%",
        "used_by": "K-a 漏记(用户 11-16)",
    },
    {
        "shape": "≥ 6 / 8(坏的 ≤ 2)",
        "one_sided": "约 60%",
        "two_sided": "约 65%",
        "used_by": "闸门的 K-a(用户 11-16)—— 很弱,只挡得住大面积退不掉",
    },
    {
        "shape": "≤ 2 / 15",
        "one_sided": "约 36%",
        "two_sided": "约 41%",
        "used_by": "K-s 重写后 B 仍在、再说一遍多出同义行",
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
    "K-n1 / K-n2 按「候选出自哪一次提炼、带不带更正标记」分开报数(§11.2a,第 10 版):"
    "x_retired_by_<pass1|pass2>_<correction|no_correction|unknown> 是退掉 X 的那条候选(取材料里"
    "候选的来源与 is_correction;第一次的产出一律是用户说的),x_listed_by_<同上> 是把 X 列进清单的"
    "候选,z_extracted_<同上> 是两次提炼原样的回复里含 Z 的记忆。读法:*_no_correction 退了 X 是"
    "代码错,pass1_correction 退了 X 是指令没挡住(没有程序兜底);线照旧是 x_retired 0 / 30。",
    "近邻:除 K-late(设计写明不种)外每个单元两条,共 17 x 30 x 2 = 1,020 条;设计 §11.3 写的"
    "「900 条」是加 K-n2、K-late、K-s 之前的数。「模型列了、被程序挡下」按「回复里列了、那一行"
    "写回后仍有效」数(counts 的 listed_blocked / listed_blocked_on_neighbour),不读进程级计数。",
    "K-a 里内容形态是数字的 2 个场景给旧值带了事件日期(「带日期的碰上不带日期的」,§11.2a 末)。"
    "用户 11-16 定:这 6 个单元不进 K-a 的线,只在 counts 里报数(dated_units / "
    "dated_retired_to_new / dated_retired / dated_coexists / dated_missed),不设线;K-a 的线按原 "
    "24 / 30 同比例改成 19 / 24(漏记 ≤ 2 / 24),逐领域按做到的不少于三分之二(6 个单元的领域 ≥ 4、"
    "3 个单元的 ≥ 2)。闸门里同理:8 个单元、≥ 6。这几个数用户 11-16 确认。",
    "K-s 否认的那一半,场景不改、照实量(用户 11-16)。汇总行与两条近邻争「同格前 2 名」,排不进去"
    "时模型列了它也不打标、不重写,「B 仍在汇总句里」就会不达标 —— 原因是名次规则,不是重写。所以"
    "每个单元走到哪一步在 counts 里分开报:deny_not_listed(模型没列汇总行)、deny_listed_blocked"
    "(列了、程序没让打标)、deny_marked_not_rewritten(打了标、重写没落库)、deny_rewritten;不达标的"
    "次数再按这四种分:<指标>_bad_<原因>(如 b_still_in_summary_bad_listed_blocked = 被名次等规则"
    "挡下,b_still_in_summary_bad_rewritten = 重写本身错)。线的判定照旧。",
    "B4d 的汇总行在提问之前可能被平台的整理巡检(每 4 小时一轮,没有按租户的开关)重写掉:那时种下"
    "的汇总行不在召回里,recall_ok 判前提不成立、补跑;补跑的花费不在报价里。",
    "K-f、K-p 的 stored_new 只看「有一条新写的、有效的用户一方普通行」,不看内容;那句话的关键串"
    "有没有命中另报(K-f 的 new_key_hit)。",
    "K-s 当场重写的模型调用若出错(限流用完重试、传输错误),这一条记录判「评测出错、要补跑」,"
    "不算成产品「打了标没重写」。",
    "K-s 再说一遍的那一半(用户 11-16):提炼出的话与某条已并入汇总的成员原文归一后相同、撞上那条"
    "成员(它被作废并连到新行)而新写的一行,单独计数(counts 的 restate_member_verbatim),不算进"
    "「多出一行同义的 ≤ 2 / 15」;那条线只算其它原因多出来的同义行。",
    "K-e2 那条用户确认的 Y(用户 11-16 定的规则,审查 M-2):模型判用户的整句与它「重复」时,用户的"
    "话一定写下;归一后内容相同 → 那条用户确认作废、连到新行;内容不同(换说法的那一半常见)→ 两条"
    "都有效(新行的 supersedes 只记一条:同一次也退了 X 时记的是 X)。所以「用户确认的那条 Y 作废」"
    "不设线,counts 里分开报 weaker_y_retired、weaker_y_coexists 与其中的 "
    "weaker_y_coexists_linked;K-w 里那条 Y 来自助手,照旧被退。K-s:判成重复的候选若把汇总行列进 "
    "replaces、而它重复的是一条有效的用户一方行,汇总行会由那条行打标 —— 这时没有新写的行,"
    "new_stored 不达标的那几次看 new_stored_bad_<原因>。",
    "K-e2 / K-w「逐字相同必须全中」的前提是提炼写出的正文与种下的那条一字不差,这由模型决定;"
    "counts 的 verbatim_extracted 报这个前提成立了几次,不成立的那几次这条线量的是模型而不是程序。",
    "写的保证只查进了去重合并模型调用的用户一方候选(材料里看得到的那些)。写入扫描拦下的那一条"
    "用平台同一个扫描函数认出来、单独计数(guarantee_scan_blocked),同一轮其它候选照查;写回里出了"
    "错(被吞的异常)的那一轮不查,这个单元在这条线上报没量到(writeback_error),不算「没坏」。"
    "被测代码不是第 9 版时报 incomplete。",
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
