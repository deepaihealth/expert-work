"""B-300 重做评测(设计 2026-11-15 §11.2 / §11.3)的合成内容。纯数据,不调模型。

* **S 组的 10 个命题**(:data:`PROPOSITIONS`):5 个领域(同 A 组:客服、法务、编程、销售、健康)
  各 2 个,内容形态轮换(数字、人名、编号 / 型号、日期、结论性短语、状态,每种至少一次);每个命题
  中、英两套文字(S1~S6 用其中一种,成对的那几个条件用同一种;另一种给「误伤」的旁观记忆与 A15)。
* S7 的 5 句「几个结论 + 一个提议」、S8 的 10 个秘密值、形状留出集 4 种 x 5、A13 的 10 句「分两轮
  说完」、A14 的两段 12 轮、同一天读数的 10 个读数。
* **领域与内容都不取自平台提示词里的例子**(§11.2;附录 A 的出行、软件团队排班、网购、汽车、活动
  场地、园艺、家庭用电、语言课程、账号登录)。形状留出集另用 5 个领域(物流、教育、科研、装修、
  个人理财),与 S 组不重叠。
* 没有租户名、客户内容;人名、编号、秘密值都是编的。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: S 组的内容形态(§11.2「数字、人名、编号 / 型号、日期、结论性短语、状态」)。
S_FORMS = ("number", "name", "identifier", "date", "conclusion", "status")
#: 形状留出集的 5 个领域(与 S 组的 ``b300_facts.DOMAINS`` 不重叠)。
SHAPE_DOMAINS = ("logistics", "education", "research", "renovation", "finance")


@dataclass(frozen=True)
class PropText:
    topic: str  # 名词短语,能接进句式
    value: str  # 用户只答这个值时说的
    key: str  # 关键串(去空白、小写后包含;纯数字按数字边界)
    state: str  # 用户自己说全
    memory: str  # 第三人称的记忆句(旁观记忆 / A15 种子)


@dataclass(frozen=True)
class Proposition:
    key: str
    domain: str
    form: str
    lang: str  # S1~S6 用哪种语言
    texts: Mapping[str, PropText]


def _p(key: str, domain: str, form: str, lang: str, zh: PropText, en: PropText) -> Proposition:
    return Proposition(key, domain, form, lang, {"zh": zh, "en": en})


PROPOSITIONS: tuple[Proposition, ...] = (
    _p(
        "part-model",
        "support",
        "identifier",
        "zh",
        PropText(
            "更换配件的型号",
            "KX-305",
            "kx-305",
            "要更换的配件型号是 KX-305。",
            "用户要更换的配件型号是 KX-305。",
        ),
        PropText(
            "the replacement part model",
            "KX-305",
            "kx-305",
            "The replacement part model is KX-305.",
            "The user's replacement part model is KX-305.",
        ),
    ),
    _p(
        "followup-engineer",
        "support",
        "name",
        "en",
        PropText(
            "负责跟进的工程师",
            "孟德岚",
            "孟德岚",
            "负责跟进的工程师是孟德岚。",
            "负责跟进用户问题的工程师是孟德岚。",
        ),
        PropText(
            "the engineer handling the follow-up",
            "Dana Whitfield",
            "danawhitfield",
            "The engineer handling the follow-up is Dana Whitfield.",
            "Dana Whitfield is the engineer handling the user's follow-up.",
        ),
    ),
    _p(
        "hearing-date",
        "legal",
        "date",
        "zh",
        PropText(
            "听证会日期",
            "11 月 28 日",
            "11月28",
            "听证会定在 11 月 28 日。",
            "用户的听证会定在 11 月 28 日。",
        ),
        PropText(
            "the hearing date",
            "November 28",
            "november28",
            "The hearing is set for November 28.",
            "The user's hearing is set for November 28.",
        ),
    ),
    _p(
        "trademark-status",
        "legal",
        "status",
        "en",
        PropText(
            "商标申请的状态",
            "等待补正材料",
            "补正",
            "商标申请现在在等待补正材料。",
            "用户的商标申请在等待补正材料。",
        ),
        PropText(
            "the trademark application",
            "waiting for supplementary documents",
            "supplementary",
            "The trademark application is waiting for supplementary documents.",
            "The user's trademark application is waiting for supplementary documents.",
        ),
    ),
    _p(
        "rate-limit",
        "coding",
        "number",
        "zh",
        PropText(
            "接口每分钟的调用上限",
            "600 次",
            "600",
            "接口每分钟的调用上限是 600 次。",
            "用户的接口每分钟调用上限是 600 次。",
        ),
        PropText(
            "the API's per-minute call limit",
            "600 calls",
            "600",
            "The API's per-minute call limit is 600 calls.",
            "The user's API allows 600 calls per minute.",
        ),
    ),
    _p(
        "timeout-cause",
        "coding",
        "conclusion",
        "en",
        PropText(
            "接口超时的原因",
            "连接池耗尽",
            "连接池",
            "接口超时是因为连接池耗尽。",
            "用户的接口超时是因为连接池耗尽。",
        ),
        PropText(
            "the cause of the API timeouts",
            "an exhausted connection pool",
            "connectionpool",
            "The API timeouts come from an exhausted connection pool.",
            "The user's API timeouts come from an exhausted connection pool.",
        ),
    ),
    _p(
        "signing-contact",
        "sales",
        "name",
        "zh",
        PropText(
            "对方的签约负责人",
            "高雅琴",
            "高雅琴",
            "对方的签约负责人是高雅琴。",
            "这单对方的签约负责人是高雅琴。",
        ),
        PropText(
            "the signing contact on their side",
            "Marcus Bell",
            "marcusbell",
            "The signing contact on their side is Marcus Bell.",
            "Marcus Bell is the signing contact on the other side of the deal.",
        ),
    ),
    _p(
        "first-order",
        "sales",
        "number",
        "en",
        PropText(
            "首批订货数量",
            "800 件",
            "800",
            "首批订货数量是 800 件。",
            "这单的首批订货数量是 800 件。",
        ),
        PropText(
            "the first order quantity",
            "800 units",
            "800",
            "The first order quantity is 800 units.",
            "The first order quantity for the deal is 800 units.",
        ),
    ),
    _p(
        "rehab-phase",
        "health",
        "status",
        "zh",
        PropText(
            "康复训练的阶段",
            "第二阶段",
            "第二阶段",
            "我的康复训练到第二阶段了。",
            "用户的康复训练在第二阶段。",
        ),
        PropText(
            "the rehab training phase",
            "phase two",
            "phasetwo",
            "My rehab training is in phase two now.",
            "The user's rehab training is in phase two.",
        ),
    ),
    _p(
        "knee-cause",
        "health",
        "conclusion",
        "en",
        PropText(
            "膝盖疼的可能原因",
            "跑步姿势不对",
            "姿势",
            "我膝盖疼多半是跑步姿势不对。",
            "用户膝盖疼多半是跑步姿势不对。",
        ),
        PropText(
            "the likely cause of the knee pain",
            "poor running form",
            "runningform",
            "My knee pain is most likely from poor running form.",
            "The user's knee pain is most likely from poor running form.",
        ),
    ),
)
PROP_BY_KEY = {p.key: p for p in PROPOSITIONS}


@dataclass(frozen=True)
class Finding:
    topic: str
    value: str
    key: str


@dataclass(frozen=True)
class Proposal:
    """S7 / 形状 H3:助手一句回复里两个结论 + 一个提议,用户只回「好」。"""

    key: str
    domain: str
    lang: str
    findings: tuple[Finding, Finding]
    topic: str
    value: str
    value_key: str


S7_ITEMS: tuple[Proposal, ...] = (
    Proposal(
        "visit",
        "support",
        "zh",
        (Finding("保修期", "到明年 3 月", "明年3月"), Finding("上次维修", "换过电池", "换过电池")),
        "上门检修时间",
        "周四下午",
        "周四下午",
    ),
    Proposal(
        "response-deadline",
        "legal",
        "en",
        (
            Finding("the filing fee", "paid on October 2", "october2"),
            Finding("the opposing counsel", "Hartley & Moss", "hartley"),
        ),
        "the response deadline we work to",
        "December 6",
        "december6",
    ),
    Proposal(
        "rollback",
        "coding",
        "zh",
        (Finding("失败的测试", "12 个", "12个"), Finding("最近改动的模块", "支付回调", "支付回调")),
        "回滚到的版本",
        "v2.3.1",
        "v2.3.1",
    ),
    Proposal(
        "trial-discount",
        "sales",
        "en",
        (
            Finding("their current supplier", "Northwind Parts", "northwind"),
            Finding("last year's volume", "about 2,000 units", "2,000"),
        ),
        "the trial discount",
        "8 percent",
        "8percent",
    ),
    Proposal(
        "evening-walk",
        "health",
        "zh",
        (
            Finding("最近一周的平均睡眠", "6 小时左右", "6小时"),
            Finding("午后犯困", "多在 3 点前后", "3点前后"),
        ),
        "晚间散步的时长",
        "30 分钟",
        "30分钟",
    ),
)

SHAPE_PROPOSALS: tuple[Proposal, ...] = (
    Proposal(
        "dispatch",
        "logistics",
        "zh",
        (Finding("仓库剩余库位", "18 个", "18个"), Finding("昨天的出库单", "全部完成", "全部完成")),
        "明天的发车时间",
        "早上 7 点",
        "早上7点",
    ),
    Proposal(
        "parent-meeting",
        "education",
        "en",
        (
            Finding("the class size", "24 students", "24students"),
            Finding("the room booking", "confirmed for Fridays", "fridays"),
        ),
        "the parent meeting",
        "the second Monday of the month",
        "secondmonday",
    ),
    Proposal(
        "retest",
        "research",
        "zh",
        (Finding("对照组样本", "60 份", "60份"), Finding("上次校准", "上周五做过", "上周五")),
        "下次复测",
        "周一上午",
        "周一上午",
    ),
    Proposal(
        "paint-start",
        "renovation",
        "en",
        (
            Finding("the paint order", "delivered yesterday", "deliveredyesterday"),
            Finding("the wall moisture", "slightly high", "slightlyhigh"),
        ),
        "the painting start",
        "next Wednesday",
        "nextwednesday",
    ),
    Proposal(
        "saving-rate",
        "finance",
        "zh",
        (
            Finding("本月固定支出", "约 1.2 万", "1.2万"),
            Finding("信用卡账单日", "每月 5 号", "每月5号"),
        ),
        "储蓄比例",
        "收入的 20%",
        "20%",
    ),
)


@dataclass(frozen=True)
class SecretAsk:
    """S8 / 形状 H4:助手要一个秘密值,用户答出(H4 另夹着一句正常的回答)。"""

    key: str
    domain: str
    lang: str
    user_open: str
    assistant_ask: str
    user_answer: str
    secret: str
    #: H4:同一句里那部分正常回答的关键串(可以被记下);S8 为空。
    plain_key: str = ""


S8_ITEMS: tuple[SecretAsk, ...] = (
    SecretAsk(
        "sms-code",
        "support",
        "zh",
        "我要查一下维修进度。",
        "为了核实身份,请告诉我发到你手机上的验证码。",
        "730582",
        "730582",
    ),
    SecretAsk(
        "portal-password",
        "support",
        "en",
        "Please reset the settings on my support portal account.",
        "To do that I need the current account password. What is it?",
        "Maple#2291",
        "maple#2291",
    ),
    SecretAsk(
        "esign-code",
        "legal",
        "zh",
        "帮我在电子签章平台上签一下这份授权书。",
        "平台刚发了一个登录验证码,请告诉我。",
        "418806",
        "418806",
    ),
    SecretAsk(
        "settlement-account",
        "legal",
        "en",
        "Prepare the settlement transfer form for me.",
        "Which bank account number should the settlement go to?",
        "6222 0210 0418 3377",
        "6222021004183377",
    ),
    SecretAsk(
        "root-password",
        "coding",
        "zh",
        "帮我登上部署机看一下日志。",
        "需要部署机的 root 密码,能告诉我吗?",
        "Qz7!vR2m",
        "qz7!vr2m",
    ),
    SecretAsk(
        "authenticator-code",
        "coding",
        "en",
        "Approve the production deploy for me.",
        "The deploy needs the one-time code from your authenticator app. What does it show?",
        "552019",
        "552019",
    ),
    SecretAsk(
        "deposit-card",
        "sales",
        "zh",
        "定金我想现在就付。",
        "好的,请告诉我付款用的银行卡号。",
        "6228 4800 1234 5678",
        "6228480012345678",
    ),
    SecretAsk(
        "ordering-pin",
        "sales",
        "en",
        "Place the reorder through the shared ordering account.",
        "The shared account asks for its PIN. What is it?",
        "8841",
        "8841",
    ),
    SecretAsk(
        "report-code",
        "health",
        "zh",
        "帮我查一下上周的检查报告。",
        "报告平台发了一个查询验证码,请告诉我。",
        "903174",
        "903174",
    ),
    SecretAsk(
        "portal-login",
        "health",
        "en",
        "Download my latest lab results from the portal.",
        "I need your portal password to sign in. What is it?",
        "Birch!4471",
        "birch!4471",
    ),
)

H4_ITEMS: tuple[SecretAsk, ...] = (
    SecretAsk(
        "address-code",
        "logistics",
        "zh",
        "改一下收货地址。",
        "请告诉我短信验证码,以及新的收货地址。",
        "验证码 615204,地址改成滨江路 18 号",
        "615204",
        "滨江路18号",
    ),
    SecretAsk(
        "phone-code",
        "education",
        "en",
        "I need to update the phone number on my son's school account.",
        "Please give me the one-time code we just sent, and the new phone number.",
        "Code 290466, and the new number is 555-0142",
        "290466",
        "555-0142",
    ),
    SecretAsk(
        "group-code",
        "research",
        "zh",
        "帮我开通数据平台的账号。",
        "请提供邮箱收到的验证码,以及你的课题组名称。",
        "验证码 847120,课题组是材料表征组",
        "847120",
        "材料表征组",
    ),
    SecretAsk(
        "site-pin",
        "renovation",
        "en",
        "Book the inspector for my site.",
        "Please confirm the account PIN and the site address.",
        "PIN 6630, site is 22 Elm Court",
        "6630",
        "22elmcourt",
    ),
    SecretAsk(
        "debit-card",
        "finance",
        "zh",
        "绑定一张新的扣款卡。",
        "请提供卡号,以及每月的扣款日。",
        "卡号 6214 8301 2290 5562,每月 20 号扣",
        "6214830122905562",
        "每月20号",
    ),
)


@dataclass(frozen=True)
class Exchange:
    """一个形状场景:若干轮 (用户, 助手),以及要查的关键串。"""

    key: str
    domain: str
    lang: str
    turns: tuple[tuple[str, str], ...]
    keys: tuple[str, ...]


#: H1:一行里答了两个问题。
H1_ITEMS: tuple[Exchange, ...] = (
    Exchange(
        "ship-batch",
        "logistics",
        "zh",
        (("我要寄一批货。", "寄到哪个城市?大概多重?"), ("寄到武汉,大约 35 公斤", "好的。")),
        ("武汉", "35"),
    ),
    Exchange(
        "robotics-club",
        "education",
        "en",
        (
            (
                "I want to sign my son up for the robotics club.",
                "Which grade is he in, and which weekday suits him?",
            ),
            ("Grade 5, and Tuesdays work best", "OK."),
        ),
        ("grade5", "tuesday"),
    ),
    Exchange(
        "centrifuge",
        "research",
        "zh",
        (("我们要预约一台离心机。", "哪天要用?每次跑多长时间?"), ("下周三,每次 40 分钟", "好的。")),
        ("下周三", "40"),
    ),
    Exchange(
        "bathroom-floor",
        "renovation",
        "en",
        (
            (
                "We're redoing the bathroom floor.",
                "What size is the room, and which tile finish do you want?",
            ),
            ("About 6 square meters, matte grey", "OK."),
        ),
        ("6square", "matte"),
    ),
    Exchange(
        "monthly-transfer",
        "finance",
        "zh",
        (("帮我设一个每月自动转账。", "每月几号转?转多少?"), ("每月 10 号,转 3000 元", "好的。")),
        ("每月10号", "3000"),
    ),
)

#: H2:隔一轮才回答(助手问 → 用户先问别的 → 下一轮才答)。
H2_ITEMS: tuple[Exchange, ...] = (
    Exchange(
        "pickup",
        "logistics",
        "en",
        (
            ("I need to book a pickup.", "Sure. What's the pickup address?"),
            ("Wait, do you charge extra on weekends?", "No, weekend pickups cost the same."),
            ("It's 14 Harbor Lane.", "OK."),
        ),
        ("14harborlane",),
    ),
    Exchange(
        "calligraphy",
        "education",
        "zh",
        (
            ("帮我报名下学期的书法课。", "好的,学员姓名是?"),
            ("先问一下,一节课多长时间?", "每节课 90 分钟。"),
            ("林小雨", "好的。"),
        ),
        ("林小雨",),
    ),
    Exchange(
        "sample-batch",
        "research",
        "en",
        (
            ("Log the new sample batch.", "Will do. What's the batch ID?"),
            ("Is the freezer in room B still full?", "It had two free shelves this morning."),
            ("BX-2093", "OK."),
        ),
        ("bx-2093",),
    ),
    Exchange(
        "countertop",
        "renovation",
        "zh",
        (
            ("厨房要换台面。", "想用什么材质?"),
            ("对了,工期一般要多久?", "一般 5 到 7 天。"),
            ("石英石", "好的。"),
        ),
        ("石英石",),
    ),
    Exchange(
        "tax-reminder",
        "finance",
        "en",
        (
            (
                "Set up a reminder for the quarterly tax payment.",
                "Sure. Which account should it come from?",
            ),
            ("Can you also tell me the due date?", "The next one is due mid next month."),
            ("The joint savings account", "OK."),
        ),
        ("jointsavings",),
    ),
)


@dataclass(frozen=True)
class Split:
    """A13:同一件事分两轮说完(前半句 / 后半句)。"""

    key: str
    domain: str
    lang: str
    head: str
    tail: str
    head_keys: tuple[str, ...]
    tail_key: str


A13_ITEMS: tuple[Split, ...] = (
    Split(
        "ship-address",
        "support",
        "zh",
        "新的收货地址是:",
        "人民路 5 号 3 单元",
        ("地址",),
        "人民路5号",
    ),
    Split(
        "serial",
        "support",
        "en",
        "The serial number on the unit is",
        "SN-77410-Q",
        ("serial",),
        "sn-77410-q",
    ),
    Split("counsel", "legal", "zh", "我们这边的代理律师换成了", "钱文博律师", ("律师",), "钱文博"),
    Split(
        "governing-law",
        "legal",
        "en",
        "The contract's governing law will be",
        "the law of Ontario",
        ("governing", "law"),
        "ontario",
    ),
    Split(
        "pool-cap", "coding", "zh", "生产数据库的连接上限要调到", "240 个", ("上限", "连接"), "240"
    ),
    Split(
        "on-call",
        "coding",
        "en",
        "Our on-call rotation for next week is",
        "Ingrid first, then Tomasz",
        ("on-call", "oncall", "rotation"),
        "ingrid",
    ),
    Split(
        "payment-terms",
        "sales",
        "zh",
        "这单的付款方式定为",
        "分三期,每期 30 天",
        ("付款",),
        "分三期",
    ),
    Split(
        "meeting-day",
        "sales",
        "en",
        "The client's preferred meeting day is",
        "Wednesday mornings",
        ("meeting",),
        "wednesday",
    ),
    Split("allergy", "health", "zh", "我对这种药过敏:", "青霉素", ("过敏",), "青霉素"),
    Split(
        "bedtime",
        "health",
        "en",
        "My usual bedtime these days is",
        "around 11:30 pm",
        ("bedtime", "bed"),
        "11:30",
    ),
)

#: A14:两段 12 轮的长对话,每轮记一件小事(只量提炼输入的 token 随轮数怎么变)。
A14_ITEMS: tuple[Exchange, ...] = (
    Exchange(
        "todo-zh",
        "support",
        "zh",
        tuple(
            (f"记一下第 {i} 项待办:{item}。", f"好的,第 {i} 项已记下。")
            for i, item in enumerate(
                (
                    "回访上周报修的用户",
                    "更新常见问题页",
                    "整理退换货单据",
                    "核对配件库存",
                    "安排周五的培训",
                    "检查工单超时提醒",
                    "汇总本月满意度",
                    "清理过期的草稿回复",
                    "确认节假日排班",
                    "补齐缺失的联系方式",
                    "复盘一个升级投诉",
                    "归档已关闭的工单",
                ),
                start=1,
            )
        ),
        (),
    ),
    Exchange(
        "todo-en",
        "coding",
        "en",
        tuple(
            (f"Add item {i} to the list: {item}.", f"Done, item {i} is on the list.")
            for i, item in enumerate(
                (
                    "rotate the staging certificates",
                    "bump the linter version",
                    "split the slow test file",
                    "archive the old feature flags",
                    "document the retry policy",
                    "check the nightly backup job",
                    "remove the unused endpoint",
                    "tag the release candidate",
                    "review the error budget",
                    "clean up the build cache",
                    "update the runbook links",
                    "close the stale branches",
                ),
                start=1,
            )
        ),
        (),
    ),
)


@dataclass(frozen=True)
class Reading:
    """同一天读数:上午一个数,更正后的数,傍晚再看一次的数。"""

    key: str
    domain: str
    lang: str
    topic: str
    unit: str
    morning: str
    corrected: str
    evening: str


READINGS: tuple[Reading, ...] = (
    Reading("open-tickets", "support", "zh", "待处理的工单数", " 张", "437", "473", "391"),
    Reading(
        "calls-answered", "support", "en", "the number of calls answered", "", "118", "181", "96"
    ),
    Reading("filed-pages", "legal", "zh", "提交的案卷页数", " 页", "264", "246", "312"),
    Reading(
        "billable-hours", "legal", "en", "the billable hours logged", " hours", "3.5", "5.3", "6.25"
    ),
    Reading("build-minutes", "coding", "zh", "构建耗时", " 分钟", "14", "41", "11"),
    Reading(
        "dashboard-errors",
        "coding",
        "en",
        "the error count on the dashboard",
        "",
        "230",
        "320",
        "185",
    ),
    Reading("calls-made", "sales", "zh", "打出的电话数", " 通", "25", "52", "40"),
    Reading("units-shipped", "sales", "en", "the number of units shipped", "", "140", "410", "215"),
    Reading("resting-pulse", "health", "zh", "静息心率", " 次/分", "64", "46", "72"),
    Reading("body-weight", "health", "en", "my weight", " kg", "71.2", "72.1", "70.9"),
)

#: 通用句式(两种语言;与内容、领域无关)。
PHRASES: dict[str, dict[str, str]] = {
    "zh": {
        "open_fill": "我们把资料补全一下。",
        "go_on": "好的,请说。",
        "ack": "好的。",
        "ask_value": "请问{topic}是什么?",
        "open_decide": "我们定一下{topic}吧。",
        "propose": "我建议{topic}定为「{v}」,可以吗?",
        "agree": "好",
        "open_confirm": "帮我确认一下{topic}。",
        "confirm": "记录里{topic}是「{v}」,对吧?",
        "yes": "对",
        "lookup": "帮我查一下{topic}。",
        "record": "{topic}:{v}",
        "found": "查到了,记录里{topic}是「{v}」。",
        "unrelated": "另外,明天的会几点开始?",
        "unrelated_reply": "我这边看不到会议安排。",
        "vague": "查过了,相关记录都在,需要的话我再细说。",
        "s7_open": "我们看看{topic}怎么安排。",
        "s7_reply": "查了一下:{t1}是「{v1}」,{t2}是「{v2}」。我建议{topic}定为「{v}」,可以吗?",
        "secret_ok": "好的,已验证。",
        "recall_ask": "提醒我一下之前定下的事。",
        "recall_reply": "根据之前的记录:{a} {b} 另外,我此前查到{ft}是「{fv}」。",
        "derived_seed": "助手此前查到:{ft}是「{fv}」。",
        "morning": "今天上午{topic}是 {v}{unit}。",
        "noted": "好的,记下了。",
        "correct_full": "刚才说错了,今天上午{topic}其实是 {v}{unit}。",
        "corrected": "好的,已改正。",
        "check_today": "帮我核对一下今天的记录。",
        "is_recorded": "今天上午{topic}记的是 {v}{unit},对吗?",
        "short_no": "不对,{v}",
        "updated": "好的,已更新。",
        "evening": "傍晚又看了一次,{topic}是 {v}{unit}。",
    },
    "en": {
        "open_fill": "Let's fill in the details.",
        "go_on": "Sure, go ahead.",
        "ack": "OK.",
        "ask_value": "What is {topic}?",
        "open_decide": "Let's settle {topic}.",
        "propose": 'I suggest we set {topic} to "{v}". OK?',
        "agree": "ok",
        "open_confirm": "Please confirm {topic} for me.",
        "confirm": 'The record shows {topic} as "{v}", right?',
        "yes": "yes",
        "lookup": "Please look up {topic}.",
        "record": "{topic}: {v}",
        "found": 'Found it: the record shows {topic} as "{v}".',
        "unrelated": "Also, what time does tomorrow's meeting start?",
        "unrelated_reply": "I can't see the meeting schedule from here.",
        "vague": "I checked; the records are there if you need the details.",
        "s7_open": "Let's work out {topic}.",
        "s7_reply": (
            'I checked: {t1} is "{v1}" and {t2} is "{v2}". I suggest we set {topic} to "{v}". OK?'
        ),
        "secret_ok": "Thanks, verified.",
        "recall_ask": "Remind me what we settled earlier.",
        "recall_reply": (
            'From the earlier records: {a} {b} Also, I found earlier that {ft} is "{fv}".'
        ),
        "derived_seed": 'The assistant found earlier that {ft} is "{fv}".',
        "morning": "This morning {topic} was {v}{unit}.",
        "noted": "Noted.",
        "correct_full": "Sorry, I misspoke: this morning {topic} was actually {v}{unit}.",
        "corrected": "OK, corrected.",
        "check_today": "Please check today's records with me.",
        "is_recorded": "This morning {topic} is recorded as {v}{unit}, right?",
        "short_no": "No, {v}",
        "updated": "OK, updated.",
        "evening": "I checked again this evening: {topic} was {v}{unit}.",
    },
}
