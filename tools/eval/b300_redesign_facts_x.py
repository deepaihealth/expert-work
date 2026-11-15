"""B-300 重做评测(设计 2026-11-15 §11.3)A13 / A14 与同一天读数的合成内容(从
``b300_redesign_facts`` 拆出,每个文件不过 800 行)。纯数据,不调模型。A15 用 S 组的命题与 S7 的
结论,在 ``b300_redesign_facts`` 里。没有租户名、客户内容。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Exchange:
    """一个形状场景:若干轮 (用户, 助手),以及要查的关键串。"""

    key: str
    domain: str
    lang: str
    turns: tuple[tuple[str, str], ...]
    keys: tuple[str, ...]


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
        ("地址", "收货", "寄送"),
        "人民路5号",
    ),
    Split(
        "serial",
        "support",
        "en",
        "The serial number on the unit is",
        "SN-77410-Q",
        ("serial", "unit", "device"),
        "sn-77410-q",
    ),
    Split(
        "counsel",
        "legal",
        "zh",
        "我们这边的代理律师换成了",
        "钱文博律师",
        ("代理", "委托", "代表我方"),
        "钱文博",
    ),
    Split(
        "governing-law",
        "legal",
        "en",
        "The contract's governing law will be",
        "the law of Ontario",
        ("governing", "jurisdiction", "applicable"),
        "ontario",
    ),
    Split(
        "pool-cap",
        "coding",
        "zh",
        "生产数据库的连接上限要调到",
        "240 个",
        ("上限", "连接", "最大"),
        "240",
    ),
    Split(
        "on-call",
        "coding",
        "en",
        "Our on-call rotation for next week is",
        "Ingrid first, then Tomasz",
        ("on-call", "oncall", "rotation", "duty"),
        "ingrid",
    ),
    Split(
        "payment-terms",
        "sales",
        "zh",
        "这单的付款方式定为",
        "分三期,每期 30 天",
        ("付款", "支付", "结算"),
        "分三期",
    ),
    Split(
        "meeting-day",
        "sales",
        "en",
        "The client's preferred meeting day is",
        "Wednesday mornings",
        ("meeting", "meet", "prefer"),
        "wednesday",
    ),
    Split("allergy", "health", "zh", "我对这种药过敏:", "青霉素", ("过敏", "不能用"), "青霉素"),
    Split(
        "bedtime",
        "health",
        "en",
        "My usual bedtime these days is",
        "around 11:30 pm",
        ("bed", "sleep", "asleep"),
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
    Reading("minutes-billed", "legal", "en", "the minutes billed", " minutes", "135", "153", "210"),
    Reading("build-seconds", "coding", "zh", "构建耗时", " 秒", "147", "174", "118"),
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
    Reading("calls-made", "sales", "zh", "打出的电话数", " 通", "125", "152", "140"),
    Reading("units-shipped", "sales", "en", "the number of units shipped", "", "140", "410", "215"),
    Reading("resting-pulse", "health", "zh", "静息心率", " 次/分", "64", "46", "72"),
    Reading("body-weight", "health", "en", "my weight", " kg", "71.2", "72.1", "70.9"),
)
