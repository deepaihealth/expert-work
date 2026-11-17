"""B-300 重做评测(设计 2026-11-15 §11.2 / §11.3)的合成内容。纯数据,不调模型。

* **S 组的 10 个命题**(:data:`PROPOSITIONS`):5 个领域(同 A 组:客服、法务、编程、销售、健康)
  各 2 个,内容形态轮换(数字、人名、编号 / 型号、日期、结论性短语、状态,每种至少一次);每个命题
  中、英两套文字(S1~S6 用其中一种,成对的那几个条件用同一种;另一种给「误伤」的旁观记忆与 A15)。
* S7 的 5 句「几个结论 + 一个提议」、S8 的 10 个秘密值。形状留出集(4 种 x 5,由没看过提示词例子
  的人写)在 ``b300_redesign_heldout``;A13~A15 与同一天读数在 ``b300_redesign_facts_x``。
* **领域与内容都不取自平台提示词里的例子**(§11.2;附录 A 的出行、软件团队排班、网购、汽车、活动
  场地、园艺、家庭用电、语言课程、账号登录)。形状留出集另用 5 个领域(物流、教育、科研、装修、
  个人理财),与 S 组不重叠。卡号类的秘密值都是 0 开头、不过 Luhn 的假号(审查 M3)。
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
            "11月28|11-28|11/28",
            "听证会定在 11 月 28 日。",
            "用户的听证会定在 11 月 28 日。",
        ),
        PropText(
            "the hearing date",
            "November 28",
            "november28|nov28|11-28|11/28",
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
            Finding("the filing fee", "paid on October 9", "october9|oct9|10-09|10/9"),
            Finding("the opposing counsel", "Hartley & Moss", "hartley"),
        ),
        "the response deadline we work to",
        "December 6",
        "december6|dec6|12-06|12/6",
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
    # 第 9 版(审查 M-1):S7 从 5 个命题加到 10 个(30 次);前 5 个原样不动(A15 仍只用前 5 个)。
    Proposal(
        "callback-slot",
        "support",
        "en",
        (
            Finding("the order status", "held at the depot", "depot"),
            Finding("the previous claim", "closed in March", "march"),
        ),
        "the callback slot",
        "Tuesday morning",
        "tuesday morning",
    ),
    Proposal(
        "addendum-drafter",
        "legal",
        "zh",
        (Finding("合同的签署方", "三家", "三家"), Finding("违约金条款", "在第 9 条", "第9条")),
        "补充协议的起草方",
        "我方",
        "我方",
    ),
    Proposal(
        "export-timeout",
        "coding",
        "en",
        (
            Finding("the slowest endpoint", "the export report", "export report"),
            Finding("the library flagged as outdated", "the PDF renderer", "pdf renderer"),
        ),
        "the request timeout for the export job",
        "45 seconds",
        "45 seconds|45s|45-second",
    ),
    Proposal(
        "next-visit",
        "sales",
        "zh",
        (
            Finding("客户去年的采购额", "约 36 万", "36万"),
            Finding("他们的决策人", "采购部的贺经理", "贺经理"),
        ),
        "下次拜访的时间",
        "下周三上午",
        "下周三",
    ),
    Proposal(
        "stretch-break",
        "health",
        "en",
        (
            Finding("the average resting heart rate last week", "about 64", "64"),
            Finding("the longest sitting stretch", "around three hours", "three hours|3 hours"),
        ),
        "the daily stretching break",
        "every 90 minutes",
        "90 minutes|90-minute",
    ),
)
#: A15(召回后复述)只用前 5 个(§11.3:5 场景 x 3)。
A15_ITEMS = S7_ITEMS[:5]


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
        "0000 7713 2048 5502",
        "0000771320485502",
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
        "0000 4800 1234 5679",
        "0000480012345679",
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
