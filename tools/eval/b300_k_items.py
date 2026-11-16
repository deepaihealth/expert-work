# ruff: noqa: RUF001, E501  # 场景文字原样保留(中文全角标点、长句),是测试数据
"""B-300 第 9 版 K 组(设计 2026-11-15 §11.2a)的合成内容。纯数据,不调模型。

* **10 组内容**(:data:`ITEMS`):5 个领域(办公行政、客服售后、学习备考、家务、兴趣社团)各 2 组,
  每个领域中、英各一组;内容形态轮换(编号、日期、状态、人名、数字、结论性短语,每种至少一次)。
  18 类场景都从这 10 组里出(同一对旧值 X / 新说法,只换新说法是怎么来的,§11.2a 首段)。
* 每组两条**近邻**(同一话题、另一个人 / 另一样东西 / 另一个场合;第一条与旧值 X 用同一个值),
  量「近邻被动过」(§11.3 末行)。新说法只针对 X。
* **怎么写的**:写的时候没有读设计附录 A / B / C / E 里给模型的指令与例子,也没有读平台代码里
  的指令原文;只读了规则章节(§6.5、§7.1、§7.3、§7.8、§7.12、§11)。规则章节里顺带出现的几个
  例子(过敏、周会、搬家、养猫、接孩子、两个合成的名字)都没有用。领域避开了现有评测已占的
  (S 组与形状留出集的 10 个领域)和 ``b300_redesign_facts`` 文件头列出的附录 A 的领域。
* 没有租户名、客户内容、行业专有内容;人名、编号都是编的。
"""

from __future__ import annotations

from dataclasses import dataclass

#: K 组的 5 个领域(与 S 组的 ``b300_facts.DOMAINS``、形状留出集的 ``SHAPE_DOMAINS`` 都不重叠)。
K_DOMAINS = ("office", "aftercare", "study", "household", "club")
K_FORMS = ("identifier", "date", "status", "name", "number", "conclusion")


@dataclass(frozen=True)
class Val:
    """一个值:对话里怎么说(``text``)、在记忆里怎么找(``key``,``|`` 分开几种写法)、第三人称的
    记忆句(``memory``,当种子用)。"""

    text: str
    key: str
    memory: str


@dataclass(frozen=True)
class Compound:
    """K-p:一条复合的记忆「A,且 B」;用户只否认 B。"""

    memory: str
    a_key: str
    b_key: str
    deny_b: str
    #: 新说法(否认 B 的那句)存下后在记忆里找它用的关键串。
    deny_key: str


@dataclass(frozen=True)
class Trio:
    """K-s:三条成员 A、B、C 和它们的汇总句;一半场景否认 B,另一半把 B 换个说法再说一遍。"""

    a: Val
    b: Val
    c: Val
    summary: str
    deny_b: str
    deny_key: str
    restate_b: str


@dataclass(frozen=True)
class Late:
    """K-late:一个写回覆盖两轮。``fuller``:第 1 轮助手问、用户短回答给出说得更全的值,第 2 轮
    用户整句顺口说了个更笼统的;``switch``:第 1 轮同意了助手的提议,第 2 轮整句改口。"""

    kind: str  # fuller | switch
    opener: str
    assistant: str  # 第 1 轮助手的提问 / 提议
    first: str  # 第 1 轮之后用户的回答(短回答 / 「好」)
    second: str  # 第 2 轮用户的整句
    #: fuller:说得更全的那部分;switch:被改掉的、更早的那个值。
    early_key: str
    #: fuller:更笼统的那句;switch:改口之后的值。
    late_key: str


@dataclass(frozen=True)
class KItem:
    key: str
    domain: str
    form: str
    lang: str
    topic: str
    x: Val
    y: Val
    z: Val
    #: 两条近邻的记忆句(第一条:另一个人 / 另一样东西,值与 X 相同;第二条:另一个场合 / 另一个值)。
    nears: tuple[str, str]
    change_full: str  # 用户整句明确改成 Y
    state_y: str  # 用户整句平铺直叙地说 Y(与 y.memory 逐字对应,只换人称)
    state_y_alt: str  # 同一件事换个说法
    hedge_full: str  # 用户整句犹豫地说「可能是 Z,不确定」
    hedge_memory: str  # 那句犹豫的话存成的记忆(K-bp 的种子)
    hedge_words: str  # 犹豫的措辞(场景自检用:那句话里确实带着;``|`` 分开)
    state_z: str  # 用户明确说 Z
    gone: str  # 用户说 X 没有了,不给替代
    gone_key: str
    x2: Val  # 与 X 同一格的另一条(K-g)
    both_gone: str  # 一句话让 X 与 x2 都不成立
    both_key: str
    compound: Compound
    trio: Trio
    proposal: str  # K-cp:助手在相反的结论后面提的建议(与 Y 无关的另一件事)
    late: Late


ITEMS: tuple[KItem, ...] = (
    KItem(
        key="cost-center",
        domain="office",
        form="identifier",
        lang="zh",
        topic="办公用品采购走的成本中心",
        x=Val("CC-4120", "cc-4120", "用户采购办公用品走的成本中心是 CC-4120。"),
        y=Val("CC-4375", "cc-4375", "用户采购办公用品走的成本中心是 CC-4375。"),
        z=Val("CC-4590", "cc-4590", "用户采购办公用品走的成本中心是 CC-4590。"),
        nears=(
            "用户的搭档邱岚采购办公用品走的成本中心是 CC-4120。",
            "用户订购培训教材走的成本中心是 CC-3301。",
        ),
        change_full="我采购办公用品走的成本中心改了，现在是 CC-4375，不再是原来那个。",
        state_y="我采购办公用品走的成本中心是 CC-4375。",
        state_y_alt="办公用品这块的采购，我这边记在 CC-4375 这个成本中心上。",
        hedge_full="我采购办公用品走的成本中心可能要换成 CC-4590，不过我还不确定。",
        hedge_memory="用户说采购办公用品走的成本中心可能要换成 CC-4590，还不确定。",
        hedge_words="可能|不确定",
        state_z="定下来了，我采购办公用品走的成本中心就是 CC-4590。",
        gone="办公用品我已经不用自己走成本中心了。",
        gone_key="不用自己走成本中心|不再|不用",
        x2=Val("行政主管签字", "签字", "用户采购办公用品要先找行政主管签字。"),
        both_gone="办公用品现在统一由行政代买，我既不用走成本中心，也不用找主管签字了。",
        both_key="代买",
        compound=Compound(
            "用户采购办公用品走成本中心 CC-4120，且单笔上限是 800 元。",
            "cc-4120",
            "800",
            "办公用品单笔 800 元的上限已经取消了。",
            "取消",
        ),
        trio=Trio(
            Val("", "碎纸机", "用户所在楼层的碎纸机放在 3 楼打印间。"),
            Val("", "周三", "用户所在楼层的文具周三统一补货。"),
            Val("", "访客", "用户所在楼层的访客要在前台登记后才能上楼。"),
            "用户所在楼层：碎纸机放在 3 楼打印间；文具周三统一补货；访客要在前台登记后才能上楼。",
            "我们楼层的文具已经不在周三统一补货了。",
            "不在周三|不再",
            "我们这层的文具是每个礼拜三一起补的。",
        ),
        proposal="另外，要不要我把这个月的采购清单整理成一张表？",
        late=Late(
            "fuller",
            "帮我填一下这次的采购申请。",
            "好的。这批东西的收货地点填哪里？",
            "3 号楼 2 层前台",
            "对了，这批东西送到公司就行，我下周要用。",
            "3号楼2层|3号楼二层",
            "送到公司",
        ),
    ),
    KItem(
        key="report-cutoff",
        domain="office",
        form="date",
        lang="en",
        topic="the monthly expense report cutoff",
        x=Val("the 25th", "25th", "The user's monthly expense report is due on the 25th."),
        y=Val("the 28th", "28th", "The user's monthly expense report is due on the 28th."),
        z=Val("the 20th", "20th", "The user's monthly expense report is due on the 20th."),
        nears=(
            "The user's teammate Anneke files her monthly expense report on the 25th.",
            "The user's quarterly inventory count is due on the 12th.",
        ),
        change_full="My monthly expense report cutoff has changed: it is now due on the 28th, not the old date.",
        state_y="My monthly expense report is due on the 28th.",
        state_y_alt="I have to hand in my expenses for the month by the 28th.",
        hedge_full="My monthly expense report might be moving to the 20th, but I'm not sure yet.",
        hedge_memory="The user says the monthly expense report might be moving to the 20th and is not sure yet.",
        hedge_words="might|not sure",
        state_z="It's settled: my monthly expense report is due on the 20th.",
        gone="I don't have a monthly expense report cutoff any more.",
        gone_key="any more|no longer|anymore",
        x2=Val(
            "receipts scanned",
            "scanned",
            "The user's monthly expense report needs every receipt scanned.",
        ),
        both_gone="Finance now pulls my expenses straight from the card feed, so I have no cutoff to meet and no receipts to scan.",
        both_key="card feed",
        compound=Compound(
            "The user's monthly expense report is due on the 25th, and it needs the manager's sign-off.",
            "25th",
            "sign-off|sign off|signoff",
            "My expense report no longer needs the manager's sign-off.",
            "no longer",
        ),
        trio=Trio(
            Val("", "locker", "The user keeps the spare projector in locker 14."),
            Val("", "thursday", "The user books the large meeting room every Thursday."),
            Val("", "badge", "The user collects visitor badges from the front desk."),
            "Around the office the user keeps the spare projector in locker 14, books the large meeting room every Thursday, and collects visitor badges from the front desk.",
            "I've stopped booking the large meeting room on Thursdays.",
            "stopped",
            "The big meeting room is the one I reserve each Thursday.",
        ),
        proposal="Also, shall I draft a checklist for closing out this month's paperwork?",
        late=Late(
            "switch",
            "I need to hand over the supplier file before my leave.",
            "Shall I set the handover for Friday?",
            "ok",
            "Actually, make the handover next Tuesday; Friday is too tight.",
            "friday",
            "tuesday",
        ),
    ),
    KItem(
        key="warranty",
        domain="aftercare",
        form="status",
        lang="zh",
        topic="那台洗碗机的保修状态",
        x=Val("还在保修期内", "保修期内", "用户家那台洗碗机还在保修期内。"),
        y=Val("已经过保", "过保", "用户家那台洗碗机已经过保。"),
        z=Val("转成延保", "延保", "用户家那台洗碗机转成延保。"),
        nears=(
            "用户父母家的洗碗机还在保修期内。",
            "用户家那台烤箱的保修要到后年才到期。",
        ),
        change_full="我家那台洗碗机的保修状态变了，现在已经过保，不在保修期里了。",
        state_y="我家那台洗碗机已经过保。",
        state_y_alt="洗碗机的保修到头了，我家这台现在算过保的机器。",
        hedge_full="我家那台洗碗机好像转成延保了，但我不太确定。",
        hedge_memory="用户说家里那台洗碗机好像转成延保了，但不太确定。",
        hedge_words="好像|不太确定",
        state_z="我查清楚了，我家那台洗碗机转成延保了。",
        gone="那台洗碗机我已经处理掉了，家里没有洗碗机了。",
        gone_key="处理掉|没有洗碗机",
        x2=Val("每季度上门保养", "保养", "用户家那台洗碗机每季度有一次上门保养。"),
        both_gone="那台洗碗机上周退货了，保修和上门保养都跟着没有了。",
        both_key="退货",
        compound=Compound(
            "用户家那台洗碗机还在保修期内，且报修要打售后专线。",
            "保修期内",
            "专线",
            "洗碗机报修不用打售后专线了。",
            "不用打",
        ),
        trio=Trio(
            Val("", "滤芯", "用户家净水器的滤芯每半年换一次。"),
            Val("", "上门前", "售后师傅上门前会先给用户打电话。"),
            Val("", "发票", "用户把家电的发票都收在书房抽屉里。"),
            "用户家电售后：净水器的滤芯每半年换一次；售后师傅上门前会先打电话；家电的发票都收在书房抽屉里。",
            "售后师傅现在上门前不会先给我打电话了。",
            "不会先|不再",
            "师傅来家里之前，都会提前来个电话。",
        ),
        proposal="另外，要不要我帮你列一下家里几件电器的购买日期？",
        late=Late(
            "switch",
            "我想约一次上门检修。",
            "要不要约在周四上午？",
            "好",
            "还是改到周六下午吧，周四我不在家。",
            "周四",
            "周六",
        ),
    ),
    KItem(
        key="ticket-agent",
        domain="aftercare",
        form="name",
        lang="en",
        topic="the agent on the repair ticket",
        x=Val(
            "Dana Whitlock", "whitlock", "The agent on the user's repair ticket is Dana Whitlock."
        ),
        y=Val(
            "Marcus Oyelaran",
            "oyelaran",
            "The agent on the user's repair ticket is Marcus Oyelaran.",
        ),
        z=Val(
            "Ingrid Solberg", "solberg", "The agent on the user's repair ticket is Ingrid Solberg."
        ),
        nears=(
            "The agent on the user's billing dispute is Dana Whitlock.",
            "The agent on the user's sister's repair ticket is Tobias Renner.",
        ),
        change_full="The agent on my repair ticket has changed: it is Marcus Oyelaran now, not the person I had before.",
        state_y="The agent on my repair ticket is Marcus Oyelaran.",
        state_y_alt="Marcus Oyelaran is the one looking after my repair case.",
        hedge_full="I think my repair ticket may have been passed to Ingrid Solberg, but I'm not certain.",
        hedge_memory="The user thinks the repair ticket may have been passed to Ingrid Solberg but is not certain.",
        hedge_words="may have|not certain|think",
        state_z="Confirmed: the agent on my repair ticket is Ingrid Solberg.",
        gone="Nobody is assigned to my repair ticket any more.",
        gone_key="nobody|no one|no longer|any more",
        x2=Val("ticket R-5512", "r-5512", "The user's repair ticket number is R-5512."),
        both_gone="The repair ticket was closed and withdrawn, so it has no number and no agent now.",
        both_key="withdrawn|closed",
        compound=Compound(
            "The agent on the user's repair ticket is Dana Whitlock, and she can be reached on extension 318.",
            "whitlock",
            "318",
            "Extension 318 doesn't work for my repair ticket any more.",
            "any more|no longer|doesn't work",
        ),
        trio=Trio(
            Val(
                "",
                "serial",
                "The user keeps the kettle's serial number on a note in the kitchen drawer.",
            ),
            Val("", "morning", "The user wants repair visits in the morning."),
            Val("", "loaner", "The user was promised a loaner unit during repairs."),
            "For repairs the user keeps the kettle's serial number on a note in the kitchen drawer, wants repair visits in the morning, and was promised a loaner unit during repairs.",
            "I no longer want repair visits in the morning.",
            "no longer",
            "When someone comes to fix things, before noon is what suits me.",
        ),
        proposal="Also, would you like me to list the open items on your other tickets?",
        late=Late(
            "fuller",
            "Please update my contact details for the repair.",
            "Sure. Which entrance should the technician use?",
            "the side gate on Alder Lane",
            "By the way, the technician can just come to the house; I'll be in all day.",
            "alder lane|side gate",
            "come to the house",
        ),
    ),
    KItem(
        key="daily-questions",
        domain="study",
        form="number",
        lang="zh",
        topic="工作日每天的做题量",
        x=Val("40 道", "40道|40题", "用户工作日每天做 40 道题。"),
        y=Val("60 道", "60道|60题", "用户工作日每天做 60 道题。"),
        z=Val("80 道", "80道|80题", "用户工作日每天做 80 道题。"),
        nears=(
            "用户的室友工作日每天做 40 道题。",
            "用户周末每天做 100 道模拟题。",
        ),
        change_full="我工作日每天的做题量调了，现在是每天 60 道，不是原来的数了。",
        state_y="我工作日每天做 60 道题。",
        state_y_alt="周一到周五，我一天刷 60 道题。",
        hedge_full="我工作日每天的做题量也许要加到 80 道，还没想好。",
        hedge_memory="用户说工作日每天的做题量也许要加到 80 道，还没想好。",
        hedge_words="也许|还没想好",
        state_z="想好了，我工作日每天做 80 道题。",
        gone="我工作日已经不做题了。",
        gone_key="不做题|不再",
        x2=Val("晚上九点开始", "九点|9点", "用户工作日做题从晚上九点开始。"),
        both_gone="考试已经考完了，工作日既不用每天做题，也不用九点开始了。",
        both_key="考完",
        compound=Compound(
            "用户工作日每天做 40 道题，且做完要把错题抄到本子上。",
            "40道|40题",
            "抄",
            "错题我已经不往本子上抄了。",
            "不往本子上抄|不再|不抄",
        ),
        trio=Trio(
            Val("", "图书馆", "用户备考时在图书馆四楼自习。"),
            Val("", "讲义", "用户备考用的是打印出来的讲义。"),
            Val("", "模考", "用户每两周参加一次模考。"),
            "用户备考：在图书馆四楼自习；用的是打印出来的讲义；每两周参加一次模考。",
            "我备考已经不用打印的讲义了。",
            "不用打印|不再",
            "我复习看的是自己打出来的纸质讲义。",
        ),
        proposal="另外，要不要我帮你把这周的错题按章节归一下类？",
        late=Late(
            "fuller",
            "帮我记一下这次模考的安排。",
            "好的。考场在哪里？",
            "二教 305 室",
            "对了，这次模考就在学校里考，不用跑远。",
            "305",
            "学校",
        ),
    ),
    KItem(
        key="first-chapter",
        domain="study",
        form="conclusion",
        lang="en",
        topic="the first thesis chapter to revise",
        x=Val(
            "the methods chapter",
            "methods",
            "The user will revise the methods chapter of the thesis first.",
        ),
        y=Val(
            "the literature review",
            "literature review",
            "The user will revise the literature review of the thesis first.",
        ),
        z=Val(
            "the discussion chapter",
            "discussion",
            "The user will revise the discussion chapter of the thesis first.",
        ),
        nears=(
            "The user's labmate Chen will revise the methods chapter of his thesis first.",
            "For the conference paper the user will revise the results section first.",
        ),
        change_full="I've changed which thesis chapter I revise first: it is the literature review now, not the one I named before.",
        state_y="I will revise the literature review of the thesis first.",
        state_y_alt="The literature review is where I'm starting my thesis revisions.",
        hedge_full="Maybe I should revise the discussion chapter of the thesis first; I haven't decided.",
        hedge_memory="The user says maybe the discussion chapter of the thesis should be revised first and has not decided.",
        hedge_words="maybe|not decided|haven't decided",
        state_z="Decided: I will revise the discussion chapter of the thesis first.",
        gone="I'm not revising any thesis chapter first any more.",
        gone_key="any more|no longer|not revising",
        x2=Val(
            "by the end of June",
            "june",
            "The user will finish the first thesis revision by the end of June.",
        ),
        both_gone="My supervisor accepted the thesis as it stands, so there is no chapter to revise first and no June deadline.",
        both_key="accepted|as it stands",
        compound=Compound(
            "The user will revise the methods chapter of the thesis first, and will send it to the second reader afterwards.",
            "methods",
            "second reader",
            "I won't be sending the chapter to the second reader after all.",
            "won't|not be sending|after all",
        ),
        trio=Trio(
            Val("", "reference manager", "The user keeps thesis sources in a reference manager."),
            Val("", "sunday", "The user backs up the thesis every Sunday."),
            Val("", "carrel", "The user writes in a library carrel on the second floor."),
            "For the thesis the user keeps sources in a reference manager, backs it up every Sunday, and writes in a library carrel on the second floor.",
            "I've stopped backing up the thesis on Sundays.",
            "stopped",
            "Sunday is the day I make a backup copy of my thesis each week.",
        ),
        proposal="Also, shall I turn your supervisor's comments into a to-do list?",
        late=Late(
            "switch",
            "I need to book a slot to practise my defence talk.",
            "Shall I put the practice talk on Wednesday?",
            "ok",
            "On second thought, schedule the practice talk for Monday; Wednesday clashes with the lab meeting.",
            "wednesday",
            "monday",
        ),
    ),
    KItem(
        key="cleaning-day",
        domain="household",
        form="date",
        lang="zh",
        topic="家里大扫除的固定时间",
        x=Val("周六上午", "周六", "用户家大扫除固定在周六上午。"),
        y=Val("周日下午", "周日", "用户家大扫除固定在周日下午。"),
        z=Val("周五晚上", "周五", "用户家大扫除固定在周五晚上。"),
        nears=(
            "用户爸妈家大扫除固定在周六上午。",
            "用户家洗床单固定在周三晚上。",
        ),
        change_full="我家大扫除的固定时间换了，现在是周日下午，不是原来那个时间了。",
        state_y="我家大扫除固定在周日下午。",
        state_y_alt="每个礼拜的大扫除，我们家放在周日下午做。",
        hedge_full="我家大扫除说不定要挪到周五晚上，现在还说不准。",
        hedge_memory="用户说家里大扫除说不定要挪到周五晚上，现在还说不准。",
        hedge_words="说不定|说不准",
        state_z="商量好了，我家大扫除固定在周五晚上。",
        gone="我家已经不固定时间做大扫除了。",
        gone_key="不固定|不再",
        x2=Val("全家一起做", "全家", "用户家大扫除是全家一起做。"),
        both_gone="我们请了保洁每周上门，不用自己固定时间、也不用全家一起大扫除了。",
        both_key="保洁",
        compound=Compound(
            "用户家大扫除固定在周六上午，且由用户负责擦窗户。",
            "周六",
            "擦窗户",
            "擦窗户现在不归我负责了。",
            "不归|不再",
        ),
        trio=Trio(
            Val("", "阳台", "用户家的晾衣架放在北阳台。"),
            Val("", "厨余", "用户家每天晚饭后倒厨余垃圾。"),
            Val("", "吸尘器", "用户家的吸尘器收在玄关柜里。"),
            "用户家务：晾衣架放在北阳台；每天晚饭后倒厨余垃圾；吸尘器收在玄关柜里。",
            "我家已经不在晚饭后倒厨余垃圾了。",
            "不在晚饭后|不再",
            "厨余垃圾我们家是吃完晚饭就拿下去倒。",
        ),
        proposal="另外，要不要我帮你排一张下个月的家务分工表？",
        late=Late(
            "switch",
            "帮我安排一下换季收纳。",
            "要不要定在这个月 15 号做？",
            "行",
            "还是改到 22 号吧，15 号家里要来客人。",
            "15号|15日",
            "22号|22日",
        ),
    ),
    KItem(
        key="grocery-budget",
        domain="household",
        form="number",
        lang="en",
        topic="the monthly grocery budget",
        x=Val("450 dollars", "450", "The user's monthly grocery budget is 450 dollars."),
        y=Val("520 dollars", "520", "The user's monthly grocery budget is 520 dollars."),
        z=Val("600 dollars", "600", "The user's monthly grocery budget is 600 dollars."),
        nears=(
            "The user's flatmate Lena keeps a monthly grocery budget of 450 dollars.",
            "The user's monthly budget for eating out is 180 dollars.",
        ),
        change_full="I've changed my monthly grocery budget: it is 520 dollars now, not the earlier figure.",
        state_y="My monthly grocery budget is 520 dollars.",
        state_y_alt="I allow myself 520 dollars a month for groceries.",
        hedge_full="I might raise my monthly grocery budget to 600 dollars, but I'm still unsure.",
        hedge_memory="The user says the monthly grocery budget might be raised to 600 dollars and is still unsure.",
        hedge_words="might|unsure",
        state_z="I've made up my mind: my monthly grocery budget is 600 dollars.",
        gone="I don't keep a monthly grocery budget any more.",
        gone_key="any more|no longer|don't keep",
        x2=Val(
            "the market on Saturdays",
            "saturday",
            "The user does the grocery shopping at the market on Saturdays.",
        ),
        both_gone="We've signed up for a meal-kit plan, so I have no grocery budget and no Saturday market run now.",
        both_key="meal-kit|meal kit",
        compound=Compound(
            "The user's monthly grocery budget is 450 dollars, and it includes cleaning supplies.",
            "450",
            "cleaning supplies",
            "Cleaning supplies are no longer part of my grocery budget.",
            "no longer",
        ),
        trio=Trio(
            Val("", "freezer", "The user keeps a chest freezer in the garage."),
            Val("", "bulk", "The user buys rice in bulk every two months."),
            Val("", "tin", "The user keeps receipts in a tin on the fridge."),
            "For the household the user keeps a chest freezer in the garage, buys rice in bulk every two months, and keeps receipts in a tin on the fridge.",
            "I've stopped buying rice in bulk.",
            "stopped",
            "Every couple of months I get a big bulk bag of rice.",
        ),
        proposal="Also, shall I sort last month's receipts into categories?",
        late=Late(
            "fuller",
            "Add the new delivery slot to my notes.",
            "Sure. Where should the driver leave the boxes?",
            "the blue bench by the back door",
            "By the way, the boxes can simply be left at the house; someone is always around.",
            "blue bench|back door",
            "left at the house",
        ),
    ),
    KItem(
        key="weekend-lead",
        domain="club",
        form="name",
        lang="zh",
        topic="羽毛球社周末活动的负责人",
        x=Val("裴若筠", "裴若筠", "用户所在羽毛球社周末活动的负责人是裴若筠。"),
        y=Val("卓一帆", "卓一帆", "用户所在羽毛球社周末活动的负责人是卓一帆。"),
        z=Val("汤可昕", "汤可昕", "用户所在羽毛球社周末活动的负责人是汤可昕。"),
        nears=(
            "用户所在羽毛球社的器材保管人是裴若筠。",
            "用户参加的读书会周末活动的负责人是蓝思齐。",
        ),
        change_full="我们羽毛球社周末活动的负责人换了，现在是卓一帆，不是之前那位了。",
        state_y="我所在羽毛球社周末活动的负责人是卓一帆。",
        state_y_alt="羽毛球社周末那场活动，现在归卓一帆管。",
        hedge_full="我们羽毛球社周末活动的负责人可能要换成汤可昕，我也不确定。",
        hedge_memory="用户说羽毛球社周末活动的负责人可能要换成汤可昕，也不确定。",
        hedge_words="可能|不确定",
        state_z="通知出来了，我们羽毛球社周末活动的负责人是汤可昕。",
        gone="我们羽毛球社的周末活动已经没有负责人了。",
        gone_key="没有负责人|不再",
        x2=Val("体育馆二号场", "二号场|2号场", "用户所在羽毛球社周末活动在体育馆二号场。"),
        both_gone="羽毛球社的周末活动整个停办了，负责人和二号场都不作数了。",
        both_key="停办",
        compound=Compound(
            "用户所在羽毛球社周末活动的负责人是裴若筠，且报名要在周四前发给她。",
            "裴若筠",
            "周四",
            "周末活动的报名不用在周四前发了。",
            "不用在周四|不再",
        ),
        trio=Trio(
            Val("", "会费", "用户所在羽毛球社的会费按学期收。"),
            Val("", "拍子", "用户在羽毛球社用的是社里的公用拍子。"),
            Val("", "群公告", "用户所在羽毛球社的通知都发在群公告里。"),
            "用户所在羽毛球社：会费按学期收；用户用的是社里的公用拍子；通知都发在群公告里。",
            "我在羽毛球社已经不用社里的公用拍子了。",
            "不用社里|不再",
            "我打球拿的是社团提供的那种大家共用的球拍。",
        ),
        proposal="另外，要不要我帮你把下个月的活动时间抄进日历？",
        late=Late(
            "fuller",
            "帮我记一下下次集训的地点。",
            "好的。在哪个场地？",
            "东区体育馆地下一层 4 号场",
            "对了，下次集训就在体育馆，离宿舍很近。",
            "4号场|地下一层",
            "体育馆",
        ),
    ),
    KItem(
        key="choir-status",
        domain="club",
        form="status",
        lang="en",
        topic="the community choir membership status",
        x=Val(
            "waiting list",
            "waiting list|waitlist",
            "The user is on the waiting list for the community choir.",
        ),
        y=Val("full member", "full member", "The user is a full member of the community choir."),
        z=Val("trial period", "trial", "The user is on a trial period with the community choir."),
        nears=(
            "The user's friend Odette is on the waiting list for the community choir.",
            "The user is a returning student in the Thursday pottery class.",
        ),
        change_full="My status with the community choir has changed: I am a full member now, not what I was before.",
        state_y="I am a full member of the community choir.",
        state_y_alt="The community choir has taken me on as a full member.",
        hedge_full="I may be on a trial period with the community choir now, though I'm not sure.",
        hedge_memory="The user says they may be on a trial period with the community choir and is not sure.",
        hedge_words="may be|not sure",
        state_z="I've heard back: I am on a trial period with the community choir.",
        gone="I have nothing to do with the community choir any more.",
        gone_key="any more|no longer|nothing to do",
        x2=Val(
            "alto section",
            "alto",
            "The user asked to join the alto section of the community choir.",
        ),
        both_gone="The community choir has disbanded, so there is no list for me to be on and no alto section to join.",
        both_key="disbanded",
        compound=Compound(
            "The user is on the waiting list for the community choir, and the choir will call when a place opens.",
            "waiting list|waitlist",
            "call",
            "The choir won't be calling me when a place opens after all.",
            "won't|after all|not be calling",
        ),
        trio=Trio(
            Val("", "folder", "The user keeps choir sheet music in a green folder."),
            Val("", "bus", "The user takes the number 9 bus to rehearsals."),
            Val("", "tuesday", "The user's rehearsals are on Tuesday evenings."),
            "For the choir the user keeps sheet music in a green folder, takes the number 9 bus to rehearsals, and rehearses on Tuesday evenings.",
            "I no longer take the number 9 bus to rehearsals.",
            "no longer",
            "Getting to rehearsal, the 9 bus is how I travel.",
        ),
        proposal="Also, shall I note the dates of the spring concerts for you?",
        late=Late(
            "switch",
            "I should sign up for one of the workshop sessions.",
            "Shall I put you down for the early session?",
            "sure",
            "Actually, put me down for the late session instead; the early one is before I finish work.",
            "early session",
            "late session",
        ),
    ),
)

ITEM_BY_KEY = {item.key: item for item in ITEMS}

#: K-s 的近邻(与被否认 / 被重说的成员 B 同一话题、另一个人 / 另一样东西 / 另一个场合);键 = 内容的键。
TRIO_NEARS: dict[str, tuple[str, str]] = {
    "cost-center": ("用户所在楼层的饮用水周三统一补货。", "隔壁楼层的文具周五统一补货。"),
    "report-cutoff": (
        "The user's manager books the small meeting room every Thursday.",
        "The user books the training lab every other Monday.",
    ),
    "warranty": ("快递员上门前会先给用户打电话。", "售后师傅去用户父母家上门前会先发短信。"),
    "ticket-agent": (
        "The user wants grocery deliveries in the morning.",
        "The user's sister wants repair visits in the evening.",
    ),
    "daily-questions": ("用户的室友备考用的是打印出来的讲义。", "用户上课记笔记用的是平板。"),
    "first-chapter": (
        "The user backs up the lab data every Sunday.",
        "The user's labmate Chen backs up his thesis every Friday.",
    ),
    "cleaning-day": ("用户爸妈家每天晚饭后倒厨余垃圾。", "用户家每周一早上扔可回收垃圾。"),
    "grocery-budget": (
        "The user's flatmate Lena buys flour in bulk every two months.",
        "The user buys coffee beans every week.",
    ),
    "weekend-lead": ("用户的队友用的是社里的公用拍子。", "用户在乒乓球社用的是自己的拍子。"),
    "choir-status": (
        "The user's friend Odette takes the number 9 bus to rehearsals.",
        "The user cycles to the pottery class.",
    ),
}

#: 两种语言的通用句式(与内容无关;``{topic}`` / ``{v}`` 由内容填)。
PHRASES: dict[str, dict[str, str]] = {
    "zh": {
        "open": "帮我核对一下我登记的资料。",
        "ack": "好的，记下了。",
        "ask_now": "好的。现在{topic}是什么？",
        "ask_is": "好的。{topic}是不是已经是{v}了？",
        "maybe": "可能吧",
        "lookup": "帮我查一下记录里的{topic}。",
        "record": "记录：{topic}为 {v}。",
        "found": "我查了记录，{topic}是{v}。",
        "unrelated": "顺便问一下，今天下午会下雨吗？",
        "unrelated_reply": "我这里查不到天气，建议看一下天气应用。",
        "quiz_open": "考考我，看我还记不记得{topic}。",
        "quiz_ask": "好，那我问你：{topic}是什么？",
        "quiz_ack": "我先不揭晓答案，你可以再想想。",
        "recall_open": "我想核对一下{topic}。",
        "recall_ask": "你印象里之前登记的是什么？",
        "recall_short": "是{v}吧？",
        "hypo_open": "我在考虑要不要调整{topic}。",
        "hypo_ask": "假如要换，你会换成什么？",
        "hearsay_open": "同事跟我提过{topic}的事。",
        "hearsay_ask": "他是怎么说的？",
        "n2_recall": "我以前是不是说过{topic}是{v}？",
        "n2_hypo": "假如把{topic}换成{v}，会有什么影响？",
        "n2_hearsay": "我同事说{topic}应该是{v}。",
        "n2_quiz": "考我一下……{topic}应该是{v}？",
        "n_reply": "这个我需要再核对一下。",
    },
    "en": {
        "open": "Please check the details you have on file for me.",
        "ack": "Okay, noted.",
        "ask_now": "Sure. What is {topic} now?",
        "ask_is": "Sure. Is {topic} already {v}?",
        "maybe": "maybe",
        "lookup": "Please look up {topic} in the records.",
        "record": "Record: {topic} is {v}.",
        "found": "I checked the records: {topic} is {v}.",
        "unrelated": "By the way, is it going to rain this afternoon?",
        "unrelated_reply": "I can't look up the weather here; a weather app will tell you.",
        "quiz_open": "Quiz me and see whether I still remember {topic}.",
        "quiz_ask": "All right, here is the question: what is {topic}?",
        "quiz_ack": "I won't reveal the answer yet; take another moment.",
        "recall_open": "I'd like to double-check {topic}.",
        "recall_ask": "What do you remember having on file before?",
        "recall_short": "{v}, right?",
        "hypo_open": "I'm wondering whether to change {topic}.",
        "hypo_ask": "If you did change it, what would you change it to?",
        "hearsay_open": "A colleague mentioned something about {topic}.",
        "hearsay_ask": "What did they say?",
        "n2_recall": "Did I say before that {topic} was {v}?",
        "n2_hypo": "Suppose {topic} were {v} instead; what would that affect?",
        "n2_hearsay": "My colleague says {topic} should be {v}.",
        "n2_quiz": "Test me... {topic} should be {v}?",
        "n_reply": "I would need to check that again.",
    },
}

#: K-cp:用户那句含糊的回答,按场景序号轮换(§11.2a:「好」「嗯」「谢谢」「继续」)。
VAGUE: dict[str, tuple[str, ...]] = {
    "zh": ("好", "嗯", "谢谢", "继续"),
    "en": ("ok", "mm-hm", "thanks", "go on"),
}
