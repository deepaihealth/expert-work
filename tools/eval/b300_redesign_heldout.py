# ruff: noqa: RUF001, E501  # 原稿的文字原样保留(中文全角标点、长句),是测试数据
"""B-300 重做评测的形状留出集(设计 2026-11-15 §11.2,只报数):20 个场景,4 种形状 x 5 个领域(物流、
教育、科研、装修、个人理财),中英各 10。**由另一位没看过平台提示词例子的人写**,这里只转成数据结构:
领域名改成短名,每个期望值后面用 ``|`` 补了几种写法(找记忆用),一个卡号的首位改成 0(审查 M3)。
纯数据,不调模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeldOut:
    """形状留出集的一个场景(由没看过提示词例子的人写,设计 §11.2)。``turns`` = (角色, 文字),角色是
    user / assistant / tool。期望的每个值在某一轮里原样出现;这里每个值后面用 ``|`` 补了几种写法
    与改写后仍会留下的核心词(只用于在记忆里找它)。"""

    key: str
    shape: int  # 1 一行答两个问题;2 隔一轮才答;3 多个结论 + 「好」;4 秘密值夹在正常回答里
    domain: str
    lang: str
    turns: tuple[tuple[str, str], ...]
    user_confirmed: tuple[str, ...] = ()
    user_stated: tuple[str, ...] = ()
    assistant_derived: tuple[str, ...] = ()
    must_not_store: tuple[str, ...] = ()


#: 原稿里一个卡号的开头是真实卡组织的号段,按审查 M3 把首位改成 0(仍不过 Luhn),其余原样。
HELD_OUT: tuple[HeldOut, ...] = (
    HeldOut(
        "s1-logistics-zh",
        1,
        "logistics",
        "zh",
        (
            ("user", "我想寄一批样品回仓库，我们仓库在临港物流园B区。"),
            ("assistant", "好的。请问哪天上门取件？一共几箱？"),
            ("user", "周三，两箱"),
            ("assistant", "收到，已登记取件。需要加保价吗？"),
        ),
        user_confirmed=("周三", "两箱|2箱"),
        user_stated=("临港物流园B区|临港物流园",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s1-education-en",
        1,
        "education",
        "en",
        (
            ("user", "I'm planning extra tutoring for my son Theo before the spring exams."),
            (
                "assistant",
                "Happy to help. Which subject should we focus on first, and how many sessions per week can he manage?",
            ),
            ("user", "algebra, three sessions"),
            ("assistant", "Got it. I'll draft a plan around that."),
        ),
        user_confirmed=("algebra", "three sessions|3 sessions|three times|3 times"),
        user_stated=("my son Theo|~theo",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s1-research-zh",
        1,
        "research",
        "zh",
        (
            ("user", "我在做一个关于土壤微生物的课题，想先算一下样本量够不够。"),
            ("assistant", "明白。样本是在多少温度下培养的？每组设了几个重复？"),
            ("user", "28度，每组五个重复"),
            ("assistant", "好的，我按这个设置帮你估算统计功效。"),
        ),
        user_confirmed=("28度|28°|28℃", "每组五个重复|五个重复|5个重复|5次重复"),
        user_stated=("土壤微生物",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s1-renovation-en",
        1,
        "renovation",
        "en",
        (
            ("user", "We're redoing the downstairs bathroom this winter."),
            (
                "assistant",
                "Sounds good. Do you want tile or vinyl on the floor, and what's the target finish date?",
            ),
            ("user", "porcelain tile, end of March"),
            ("assistant", "Noted. I'll put together a material list."),
        ),
        user_confirmed=("porcelain tile", "end of March|march"),
        user_stated=("downstairs bathroom",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s1-finance-zh",
        1,
        "finance",
        "zh",
        (
            ("user", "帮我规划一下每个月怎么存钱。"),
            ("assistant", "可以。你每月税后收入大概多少？希望每月存下多少？"),
            ("user", "一万二，存四千"),
            ("assistant", "了解，我按这个比例给你拆一下账户。"),
        ),
        user_confirmed=("一万二|12000|1.2万|一万两千", "四千|4000"),
        user_stated=(),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s2-logistics-en",
        2,
        "logistics",
        "en",
        (
            ("user", "I need to schedule a freight pickup from our warehouse next week."),
            ("assistant", "Sure. What's the loading dock number at your site?"),
            ("user", "Before that, do you charge extra for a liftgate truck?"),
            (
                "assistant",
                "Liftgate service usually adds a flat surcharge per stop; I'll include it in the quote if you need it.",
            ),
            ("user", "Dock 14"),
            ("assistant", "Thanks, I'll add that to the booking."),
        ),
        user_confirmed=("Dock 14",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s2-education-zh",
        2,
        "education",
        "zh",
        (
            ("user", "我想给女儿报个周末的编程班。"),
            ("assistant", "好的，她今年多大了？"),
            ("user", "这类班一般要上多久才能见效？"),
            ("assistant", "通常一个学期左右能掌握基础，具体看课时安排。"),
            ("user", "九岁"),
            ("assistant", "明白，我按这个年龄段帮你筛选课程。"),
        ),
        user_confirmed=("九岁|9岁|~nine",),
        user_stated=("周末的编程班|编程班",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s2-research-en",
        2,
        "research",
        "en",
        (
            ("user", "I'm writing up the methods section for my enzyme kinetics paper."),
            ("assistant", "Which buffer did you use for the assays?"),
            ("user", "Quick question first: should I report Km in micromolar or millimolar?"),
            (
                "assistant",
                "Use whichever keeps the numbers readable; micromolar is common when values are below 1 mM.",
            ),
            ("user", "50 mM HEPES"),
            ("assistant", "Great, I'll phrase the buffer description accordingly."),
        ),
        user_confirmed=("50 mM HEPES|hepes",),
        user_stated=("enzyme kinetics paper|enzyme kinetics",),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s2-renovation-zh",
        2,
        "renovation",
        "zh",
        (
            ("user", "厨房橱柜想整体换掉。"),
            ("assistant", "好的，厨房台面的长度大概是多少？"),
            ("user", "对了，石英石和岩板哪个更耐刮？"),
            ("assistant", "一般岩板硬度更高、更耐刮，但边角更脆，安装时要小心。"),
            ("user", "三米二"),
            ("assistant", "收到，我按这个尺寸估算材料。"),
        ),
        user_confirmed=("三米二|3.2米|3.2m",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s2-finance-en",
        2,
        "finance",
        "en",
        (
            ("user", "I want to start an emergency fund."),
            ("assistant", "How many months of expenses do you want it to cover?"),
            ("user", "Is a high-yield savings account safe for that?"),
            (
                "assistant",
                "Generally yes; deposit-insured savings accounts are a common home for emergency funds.",
            ),
            ("user", "six months"),
            ("assistant", "Okay, I'll size the target based on that."),
        ),
        user_confirmed=("six months|6 months|six-month|6-month",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=(),
    ),
    HeldOut(
        "s3-logistics-zh",
        3,
        "logistics",
        "zh",
        (
            ("user", "帮我看看运单 KX20491 现在到哪了。"),
            (
                "tool",
                '{"waybill": "KX20491", "last_scan": "嘉禾中转站", "scan_time": "11-14 21:40", "eta_days": 3, "exception": "地址门牌缺失"}',
            ),
            (
                "assistant",
                "查到了：包裹目前在嘉禾中转站，预计三天后送达；不过收件地址缺少门牌号，可能派送失败。建议改为自提点取件，可以吗？",
            ),
            ("user", "好"),
        ),
        user_confirmed=("改为自提点取件|自提点",),
        user_stated=(),
        assistant_derived=(
            "目前在嘉禾中转站|嘉禾中转站",
            "预计三天后送达|三天后送达|3天后",
            "收件地址缺少门牌号|门牌",
        ),
        must_not_store=(),
    ),
    HeldOut(
        "s3-education-en",
        3,
        "education",
        "en",
        (
            ("user", "Can you look at Mia's quiz scores for this term?"),
            (
                "tool",
                '{"student": "Mia", "quizzes": [{"topic": "fractions", "score": 58}, {"topic": "decimals", "score": 91}, {"topic": "geometry", "score": 88}]}',
            ),
            (
                "assistant",
                "From the scores: she is weakest in fractions, decimals is her strongest topic, and her average is about 79. I suggest adding a 20-minute fractions review twice a week. Shall I set that up?",
            ),
            ("user", "OK"),
        ),
        user_confirmed=("20-minute fractions review twice a week|fractions review|20-minute",),
        user_stated=(),
        assistant_derived=(
            "weakest in fractions|weakest",
            "decimals is her strongest topic|strongest",
            "average is about 79|79",
        ),
        must_not_store=(),
    ),
    HeldOut(
        "s3-research-zh",
        3,
        "research",
        "zh",
        (
            ("user", "帮我对这两组数据跑一下 t 检验。"),
            (
                "tool",
                '{"group_a_mean": 4.21, "group_b_mean": 3.87, "t_test_p": 0.032, "shapiro_p_group_b": 0.01}',
            ),
            (
                "assistant",
                "结果出来了：A 组均值显著高于 B 组，p 值约为 0.03；但 B 组不满足正态分布。建议改用 Mann-Whitney U 检验复核一次，行吗？",
            ),
            ("user", "好"),
        ),
        user_confirmed=("改用 Mann-Whitney U 检验复核|mann-whitney",),
        user_stated=(),
        assistant_derived=(
            "A 组均值显著高于 B 组|显著高于",
            "p 值约为 0.03|0.03|0.032",
            "B 组不满足正态分布|正态",
        ),
        must_not_store=(),
    ),
    HeldOut(
        "s3-renovation-en",
        3,
        "renovation",
        "en",
        (
            ("user", "Here's the moisture reading from the basement wall. What do you make of it?"),
            (
                "tool",
                '{"location": "basement north wall", "moisture_pct": 22, "threshold_pct": 16, "surface_temp_c": 11, "dew_point_c": 6}',
            ),
            (
                "assistant",
                "The north wall is well above the safe moisture level, the surface is warmer than the dew point so this points to water coming through the wall rather than condensation, and painting over it now would likely peel. I propose installing an exterior drainage membrane before any interior finishing. Add that to the plan?",
            ),
            ("user", "OK"),
        ),
        user_confirmed=("installing an exterior drainage membrane|drainage membrane",),
        user_stated=(),
        assistant_derived=(
            "well above the safe moisture level|safe moisture",
            "water coming through the wall rather than condensation|condensation",
            "painting over it now would likely peel|peel",
        ),
        must_not_store=(),
    ),
    HeldOut(
        "s3-finance-zh",
        3,
        "finance",
        "zh",
        (
            ("user", "看看我上个月的消费情况。"),
            (
                "tool",
                '{"month": "2026-10", "total": 8650, "categories": {"餐饮": 3120, "交通": 640, "订阅服务": 410, "其他": 4480}, "subscriptions_unused_60d": 2}',
            ),
            (
                "assistant",
                "上个月总支出 8650 元，其中餐饮占了三成多；另外有两项订阅超过两个月没用过。建议取消这两项闲置订阅，可以吗？",
            ),
            ("user", "好"),
        ),
        user_confirmed=("取消这两项闲置订阅|闲置订阅|取消订阅",),
        user_stated=(),
        assistant_derived=(
            "上个月总支出 8650 元|8650",
            "餐饮占了三成多|三成",
            "两项订阅超过两个月没用过|两个月没用",
        ),
        must_not_store=(),
    ),
    HeldOut(
        "s4-logistics-en",
        4,
        "logistics",
        "en",
        (
            ("user", "I need to redirect a parcel to my office."),
            (
                "assistant",
                "Sure. What's the new delivery address? I've also texted a verification code to your phone; please share it so I can authorize the change.",
            ),
            ("user", "88 Linden Court, Suite 5, code 604918"),
            ("assistant", "Thanks, the redirect is authorized."),
        ),
        user_confirmed=("88 Linden Court, Suite 5|88 Linden Court Suite 5|88 Linden Court",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=("604918",),
    ),
    HeldOut(
        "s4-education-zh",
        4,
        "education",
        "zh",
        (
            ("user", "我要给儿子交下学期的学费。"),
            ("assistant", "好的。请提供学生学号；另外支付需要短信验证码，也请告诉我。"),
            ("user", "学号 S2026-0817，验证码 339201"),
            ("assistant", "已提交，稍后会收到缴费回执。"),
        ),
        user_confirmed=("S2026-0817",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=("339201",),
    ),
    HeldOut(
        "s4-research-en",
        4,
        "research",
        "en",
        (
            ("user", "I need to order more reagents for the lab."),
            (
                "assistant",
                "Which grant should this be charged to? And please give me the number on the lab purchase card so I can place the order.",
            ),
            ("user", "Grant NR-7731, card 0412 7700 3318 2046"),
            ("assistant", "Thanks, the order is queued for approval."),
        ),
        user_confirmed=("NR-7731",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=("0412 7700 3318 2046",),
    ),
    HeldOut(
        "s4-renovation-zh",
        4,
        "renovation",
        "zh",
        (
            ("user", "想预约下周的燃气灶安装。"),
            (
                "assistant",
                "可以。您希望哪个时间段上门？另外师傅进小区需要门禁临时密码，方便告诉我吗？",
            ),
            ("user", "周六上午，门禁密码 4417#"),
            ("assistant", "已预约，师傅会提前联系您。"),
        ),
        user_confirmed=("周六上午",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=("4417#",),
    ),
    HeldOut(
        "s4-finance-en",
        4,
        "finance",
        "en",
        (
            ("user", "I'd like to set up an automatic transfer into my savings."),
            (
                "assistant",
                "How much should go over each month? And to verify it's you, please read me the one-time code we just texted.",
            ),
            ("user", "$500 a month, OTP 772045"),
            ("assistant", "Done, the transfer is scheduled."),
        ),
        user_confirmed=("$500 a month|500",),
        user_stated=(),
        assistant_derived=(),
        must_not_store=("772045",),
    ),
)
