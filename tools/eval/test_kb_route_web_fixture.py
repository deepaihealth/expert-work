"""B-267 \u2014 the ``web_search`` fixture of the KB-routing eval (``eval-kb-route``).

``datasets/kb_route/b267-kb-route.json`` answers the eval's web searches with
fixed public information: C2 public definitions, C3 the PUBLIC value the
knowledge base's internal version deliberately contradicts, C4 web-only /
time-sensitive facts, and the public half of the C7 questions. The fictional
C1 / C7 terms (the knowledge base's own book, products, forms, people) must
miss \u2014 a real web search would not know them either.

The likely queries below are what a model plausibly sends: the question
verbatim, a keyword form, an English form. Every one of them must reach the
intended entry, so a reworded query does not silently turn into a miss.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from orchestrator.tools.web_search_fixture import (
    FixtureWebSearchClient,
    load_web_search_fixture,
    match_entry,
    normalize,
)

_REPO = Path(__file__).resolve().parents[2]
_DIR = Path(__file__).parent / "datasets" / "kb_route"
_NAME = "b267-kb-route"
#: The copy the test cluster mounts (kustomize cannot load files outside the
#: overlay, so it is vendored \u2014 this test keeps the two identical).
_DEPLOYED = _REPO / "infra" / "k8s" / "overlays" / "test" / "web-search-fixtures" / f"{_NAME}.json"

#: The knowledge base's invented proper nouns (dataset ``kb_only_terms``).
_KB_ONLY_TERMS = (
    "栖桐", "TQ-7", "TQ7", "桐舟", "桐记", "SP-03", "QT-STD-12", "QT-HR-07", "QT-OPS-03",
    "柏简舟", "云岭茶事考", "青岚书局", "QL-2024-0387", "苏晚禾", "汪若岚", "楠溪",
    "溪山听雨录", "竹炉小记", "澄州", "白石井", "三窨", "十八茶行",
)  # fmt: skip

# (case id, entry id, likely queries)
_HITS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("c2-01", "dew-point", ("什么是露点?", "露点 定义", "dew point definition")),
    (
        "c2-02",
        "relative-humidity",
        ("相对湿度是什么意思?", "相对湿度 定义", "relative humidity meaning"),
    ),
    (
        "c2-03",
        "celsius-fahrenheit",
        ("摄氏度怎么换算成华氏度?", "摄氏度 华氏度 换算公式", "celsius to fahrenheit formula"),
    ),
    (
        "c2-04",
        "mean-kinetic-temperature",
        ("平均动力学温度 MKT 是什么?", "MKT 定义", "mean kinetic temperature"),
    ),
    (
        "c2-05",
        "resolution-vs-accuracy",
        (
            "温度计的分辨率和精度有什么区别?",
            "分辨率 精度 区别",
            "thermometer resolution vs accuracy",
        ),
    ),
    (
        "c2-06",
        "cold-chain-break",
        ("冷链里说的「断链」是什么意思?", "冷链 断链 含义", "cold chain break meaning"),
    ),
    (
        "c2-07",
        "dew-point-saturation",
        (
            "露点等于气温的时候,相对湿度是多少?",
            "露点 等于 气温 相对湿度",
            "dew point equals air temperature relative humidity",
        ),
    ),
    (
        "c2-08",
        "mean-kinetic-temperature",
        (
            "为什么平均动力学温度通常比算术平均温度高?",
            "MKT 高于 算术平均温度 原因",
            "why mean kinetic temperature is higher than arithmetic mean",
        ),
    ),
    (
        "c3-01",
        "freezer-temperature",
        (
            "冷冻区温度要控制在多少度以下?",
            "冷冻区 温度 标准",
            "冷冻库 储存温度 国家标准",
            "frozen storage temperature standard",
        ),
    ),
    (
        "c3-02",
        "refrigerated-temperature",
        ("冷藏区的温度范围是多少?", "冷藏 温度范围 标准", "refrigerated storage temperature range"),
    ),
    (
        "c3-03",
        "cool-storage-temperature",
        ("阴凉库的温度上限是多少度?", "阴凉库 温度 要求", "cool storage temperature limit"),
    ),
    (
        "c3-04",
        "room-temperature-storage",
        ("常温库的温度范围应该是多少?", "常温库 温度 范围 标准", "room temperature storage range"),
    ),
    (
        "c3-05",
        "warehouse-humidity",
        (
            "仓库的相对湿度应该控制在什么范围?",
            "仓库 相对湿度 标准 范围",
            "warehouse relative humidity range",
        ),
    ),
    (
        "c3-06",
        "logger-calibration",
        (
            "温湿度记录仪多久要校准一次?",
            "温湿度记录仪 校准周期",
            "data logger calibration interval",
        ),
    ),
    ("c3-07", "freezer-temperature", ("按我们的标准,冷冻库温度应该是多少?", "冷冻库 温度 标准")),
    (
        "c3-08",
        "precool-reefer",
        (
            "冷藏车装货前要预冷多长时间?",
            "冷藏车 预冷 时间",
            "冷藏车 预冷 温度",
            "reefer truck precooling time",
        ),
    ),
    (
        "c4-01",
        "beijing-weather",
        ("今天北京天气怎么样?", "北京 今天 天气", "Beijing weather today"),
    ),
    (
        "c4-02",
        "usd-cny-rate",
        ("今天美元兑人民币的汇率是多少?", "美元 人民币 汇率 今日", "USD CNY exchange rate today"),
    ),
    ("c4-03", "tech-news", ("最近有什么重要的科技新闻?", "最新 科技 新闻", "latest tech news")),
    (
        "c4-04",
        "everest-height",
        ("珠穆朗玛峰最新测定的高度是多少米?", "珠峰 高度 最新测量", "Mount Everest height"),
    ),
    (
        "c4-05",
        "world-cup-2026",
        ("2026 年世界杯在哪些国家举办?", "2026世界杯 举办国", "2026 World Cup host countries"),
    ),
    (
        "c4-06",
        "latest-iphone",
        ("现在最新的 iPhone 是哪一款?", "最新 iPhone 型号", "latest iPhone model"),
    ),
    ("c4-07", "france-capital", ("法国的首都是哪里?", "法国 首都", "capital of France")),
    ("c4-08", "speed-of-light", ("真空中的光速是多少?", "光速 数值", "speed of light in vacuum")),
    ("c7-01", "logger-accuracy", ("温湿度记录仪 温度精度 一般多少", "这个精度在同类产品里算高吗")),
    ("c7-02", "shanghai-hotel", ("上海 600-700元 酒店 档次", "上海 酒店 每晚 680 元")),
    ("c7-03", "freezer-temperature", ("冷冻 温度 国家标准",)),
    ("c7-04", "express-delivery-time", ("国内快递一般几天能送到?", "国内快递 时效")),
    ("c7-06", "cr2477-price", ("CR2477 电池 价格", "CR2477 纽扣电池 多少钱")),
)

#: Queries about the knowledge base's own (fictional) content: no entry.
_MISSES = (
    "《云岭茶事考》是谁写的?", "云岭茶事考 作者", "青岚书局 出版", "QL-2024-0387",
    "云岭最早成片的茶园是什么时候开辟的?", "一线城市出差住宿每晚最多能报多少钱?",
    "每年的外部培训费用额度是多少?没用完能结转到明年吗?", "TQ-7 用的是什么型号的电池?",
    "TQ-7 红灯连续闪三次是什么意思?", "怎么把 TQ-7 恢复出厂设置?", "给客户寄样品要填哪张表?",
    "SP-03 表 审核人", "一个桐记 App 账号最多能绑定多少台设备?", "柏简舟 写过的书",
    "白石井 参观", "TQ-7 温度精度",
)  # fmt: skip

#: C3: (entry, the public value the content must state, the KB value it must not).
_PUBLIC_VS_KB = (
    ("freezer-temperature", r"(-|\u2212|零下\s*)18", r"(-|\u2212|零下\s*)22"),
    (
        "refrigerated-temperature",
        r"(2|二)\s*(℃|°C|度)?\s*[~\uff5e\-\u2014\u2013至到]\s*(8|八)",
        r"(3|三)\s*(℃|°C|度)?\s*[~\uff5e\-\u2014\u2013至到]\s*(7|七)",
    ),
    ("cool-storage-temperature", r"20\s*(℃|°C|度)", r"18\s*(℃|°C|度)"),
    (
        "room-temperature-storage",
        r"10\s*(℃|°C|度)?\s*[~\uff5e\-\u2014\u2013至到]\s*30",
        r"12\s*(℃|°C|度)?\s*[~\uff5e\-\u2014\u2013至到]\s*28",
    ),
    (
        "warehouse-humidity",
        r"35\s*%?\s*(RH)?\s*[~\uff5e\-\u2014\u2013至到]\s*75",
        r"40\s*%?\s*(RH)?\s*[~\uff5e\-\u2014\u2013至到]\s*65",
    ),
    ("logger-calibration", r"(12|十二)\s*个月", r"(6|六)\s*个月|半年"),
    ("precool-reefer", r"30\s*分钟", r"45"),
)


@pytest.fixture(scope="module")
def client() -> FixtureWebSearchClient:
    return load_web_search_fixture(str(_DIR), _NAME)


def _content(client: FixtureWebSearchClient, entry_id: str) -> str:
    [entry] = [e for e in client.entries if e.id == entry_id]
    return "\n".join(r["content"] for r in entry.results)


@pytest.mark.parametrize(
    ("entry_id", "query"),
    [(entry, q) for _case, entry, queries in _HITS for q in queries],
    ids=[f"{case}:{q}" for case, _entry, queries in _HITS for q in queries],
)
def test_likely_queries_reach_the_intended_entry(
    client: FixtureWebSearchClient, entry_id: str, query: str
) -> None:
    hit = match_entry(client.entries, query)
    assert hit is not None, f"miss: {query!r}"
    assert hit.id == entry_id


@pytest.mark.parametrize("query", _MISSES)
def test_fictional_terms_miss(client: FixtureWebSearchClient, query: str) -> None:
    assert match_entry(client.entries, query) is None


@pytest.mark.parametrize(("entry_id", "public", "kb"), _PUBLIC_VS_KB)
def test_c3_entries_state_the_public_value_not_the_kb_value(
    client: FixtureWebSearchClient, entry_id: str, public: str, kb: str
) -> None:
    content = _content(client, entry_id)
    assert re.search(public, content)
    assert not re.search(kb, content)


def test_no_entry_knows_the_knowledge_base(client: FixtureWebSearchClient) -> None:
    """No keyword or result carries a KB-only term \u2014 a web search must not be
    able to answer, or even recognise, the knowledge base's own content."""
    terms = {normalize(t) for t in _KB_ONLY_TERMS}
    for entry in client.entries:
        keywords = {k for keywords in entry.keyword_lists for k in keywords}
        assert not keywords & terms, entry.id
        text = normalize(" ".join(" ".join(r.values()) for r in entry.results))
        assert not [t for t in terms if t in text], entry.id


def test_every_entry_is_reachable(client: FixtureWebSearchClient) -> None:
    assert {e.id for e in client.entries} == {entry for _case, entry, _queries in _HITS}


def test_search_returns_results_for_a_hit(client: FixtureWebSearchClient) -> None:
    raw = asyncio.run(client.search(query="什么是露点?", max_results=5, tenant_id=None))
    assert raw["results"]
    assert raw["fixture"]["fixture_entry"] == "dew-point"


def test_the_deployed_copy_is_identical() -> None:
    assert _DEPLOYED.read_bytes() == (_DIR / f"{_NAME}.json").read_bytes()
