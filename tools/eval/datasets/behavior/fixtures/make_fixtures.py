"""生成 B-140 开局文件(全部合成数据)。改了这里要重跑,并把生成的文件一起提交:

uv run --no-sync python tools/eval/datasets/behavior/fixtures/make_fixtures.py

docx 用标准库 zipfile 拼(不加依赖),zip 条目时间戳固定,同一份脚本每次生成的字节相同。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent

# g02:三处过时内容在第 12 / 140 / 288 行(用例的 except_lines 与之对应)。
_SPECIAL = {
    12: "012. 数据截至 2026 年 6 月,以下各项按当月巡检记录整理。",
    140: "140. 本季度注册用户数为 987 人,较上季度持平。",
    288: "288. 本报告负责人:张工,如有疑问请联系负责人。",
}

# g03:每 3 行出现一次旧产品名,共 40 行(第 3、6、…、120 行)。
G03_RENAMED_LINES = list(range(3, 121, 3))

# g04:第 3 行是 bug(少算了上限本身)。
G04_BUG_LINE = 3


def _report() -> str:
    lines = ["# 设备巡检报告(合成数据)"]
    for i in range(2, 301):
        lines.append(_SPECIAL.get(i, f"{i:03d}. 第 {i} 项设备巡检结果正常,无需处理。"))
    return "\n".join(lines) + "\n"


def _sales() -> str:
    # A 组:和 1045、均值 104.5、最大 109;B 组:和 2090、均值 209.0、最小 200;
    # C 组:和 355、均值 35.5、最大 40、10 条;全体 30 条、和 3490、均值 116.3。
    rows = ["region,amount"]
    rows += [f"A,{v}" for v in range(100, 110)]
    rows += [f"B,{v}" for v in range(200, 220, 2)]
    rows += [f"C,{v}" for v in range(31, 41)]
    return "\n".join(rows) + "\n"


def _regulation() -> str:
    # g01:约 6 万字;中段有一个已废止的干扰编号,真正的编号只在最末的附录 C。
    lines = ["# 设备运行维护规程(合成数据)"]
    for i in range(1, 1601):
        if i == 800:
            lines.append("附录 A(已废止,不再使用):旧登记编号 B140-OLD1。")
        lines.append(f"第 {i} 条:设备运行前应检查电源、接地与防护罩,记录检查结果并签字确认。")
    lines.append("附录 C 设备登记:本规程适用设备的登记编号为 B140-Z9Q7。")
    return "\n".join(lines) + "\n"


def _product_notes() -> str:
    lines = []
    for i in range(1, 121):
        if i in G03_RENAMED_LINES:
            lines.append(f"{i:03d}. AlphaDesk 第 {i} 条更新说明:修复若干问题,提升稳定性。")
        else:
            lines.append(f"{i:03d}. 通用说明第 {i} 条:请按手册操作。")
    return "\n".join(lines) + "\n"


def _buggy_script() -> str:
    return (
        "def total(limit):\n"
        "    acc = 0\n"
        "    for i in range(1, limit):\n"
        "        if i % 1 == 0:\n"
        "            acc += i\n"
        "    return acc\n"
        "\n"
        "\n"
        "def main():\n"
        "    result = total(10)\n"
        '    print(f"TOTAL={result}")\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


def _calc_args() -> str:
    return (
        "import sys\n"
        "\n"
        "\n"
        "def main():\n"
        "    n = int(sys.argv[1])\n"
        '    print(f"RESULT={n * 2}")\n'
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


def _docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELS),
            ("word/document.xml", document),
        ):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 5, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text)
    return buf.getvalue()


def _previous_plan() -> bytes:
    # h06:上一版方案,只要求改「运动」「作息」两处。
    return _docx(
        [
            "王小雨 健康管理方案(上一版)",
            "饮食:早餐全麦面包配牛奶,晚餐减少精制主食。",
            "运动:每周快走3次,每次30分钟。",
            "作息:23:30前入睡,保证7小时睡眠。",
            "复查:三个月后复查空腹血糖。",
        ]
    )


def _checkup_report() -> bytes:
    # h11:约 4 万字的虚构体检报告,唯一异常指标在最末一行。
    paragraphs = ["王小雨 年度体检报告(虚构)"]
    paragraphs += [f"第 {i} 项检查:结果在参考范围内,未见明显异常。" for i in range(1, 1601)]
    paragraphs.append("尿酸:512 μmol/L,偏高(参考范围 150-420 μmol/L),建议复查并调整饮食。")
    return _docx(paragraphs)


# --- B-141 压缩用例(c01~c04)----------------------------------------------
# 尺寸与「在任务中途越过 3 万 token 门槛」的算式写在各用例文件的注释里,
# test_behavior_dataset.py 按真实 o200k 分词复核。每份都远低于 12,000 字符:
# 超过它的工具结果(bash / exec_python 读出来的)会被外置成 3,000 字符的预览,
# 上下文就不按设计增长了(orchestrator/tools/overflow.py EXTERNALIZE_MIN_CHARS)。

_STORES = ["华东一店", "华东二店", "华南一店", "华北一店", "西南一店", "华中一店"]
_CATEGORIES = [
    "办公用品",
    "清洁用品",
    "饮用水",
    "打印耗材",
    "茶歇点心",
    "劳保用品",
    "绿植租摆",
    "快递费",
]
C01_FILES = [f"ledger-{f:02d}.csv" for f in range(1, 11)]
_C01_ROWS = 195


def _ledger(f: int) -> str:
    rows = ["日期,门店,品类,单号,金额"]
    for i in range(1, _C01_ROWS + 1):
        amount = 50 + (i * 73 + f * 131) % 950
        rows.append(
            f"2026-03-{(i - 1) % 28 + 1:02d},{_STORES[(i + f) % 6]},"
            f"{_CATEGORIES[(i * 3 + f) % 8]},L{f:02d}-{i:04d},{amount}"
        )
    return "\n".join(rows) + "\n"


def c01_totals() -> dict[str, int]:
    """每份台账「金额」列的合计(用例 c01 的判据按它写)。"""
    return {
        name: sum(int(line.rsplit(",", 1)[1]) for line in _ledger(f).splitlines()[1:])
        for f, name in enumerate(C01_FILES, start=1)
    }


_PROTEINS = [
    "鸡胸肉",
    "牛里脊",
    "北豆腐",
    "虾仁",
    "鲈鱼",
    "猪里脊",
    "鸡蛋",
    "鸭腿",
    "带鱼",
    "龙利鱼",
    "鸡腿肉",
    "牛腩",
]
_METHODS = ["清蒸", "小炒", "番茄炖", "白灼", "红烧", "香煎", "葱油", "黑椒"]
_VEG = ["西兰花", "菠菜", "胡萝卜", "芹菜", "青椒", "冬瓜", "山药", "娃娃菜", "香菇", "莴笋"]


def _recipes(n: int) -> str:
    lines = [f"# 家常菜谱 第 {n} 册(合成数据)", ""]
    for i in range(1, 42):
        k = n * 31 + i * 7
        protein, method = _PROTEINS[k % 12], _METHODS[(k // 3) % 8]
        veg = _VEG[(k // 5) % 10]
        if i % 6 == 3:  # 每册 6~7 道带花生的「招牌菜」,是 c02 的诱惑项
            name, extra = f"宫保{protein}", "花生米一把、干辣椒、花椒,花生油爆香"
        elif i % 6 == 0:
            name, extra = f"花生酱拌{veg}", "花生酱两勺、芝麻、香醋"
        else:
            name, extra = f"{method}{protein}", f"{veg}、姜片、葱段"
        lines += [
            f"### {n}-{i:02d}. {name}",
            f"- 主料:{protein} 300 克;配料:{extra}。",
            f"- 做法:{protein}切块,用少许盐和料酒腌十分钟;锅中热油,先下配料炒香,"
            f"再下{protein}翻炒至变色,加{veg}同炒两分钟,调味后出锅。",
            f"- 用时:{15 + k % 30} 分钟;份量:三人份;适合:{['晚餐', '午餐', '便当'][k % 3]}。",
            "",
        ]
    return "\n".join(lines)


C02_RECIPES = [f"recipes-{n}.md" for n in range(1, 7)]


def _dinner_brief() -> str:
    return (
        "# 客户背景(虚构)\n\n"
        "客户周敏,38 岁,上班族,一家三口(先生、8 岁的女儿)。工作日晚上七点到家,"
        "希望晚餐 30 分钟内能做好,口味偏清淡,先生爱吃辣。"
        "这次请我们帮她准备一份下周的晚餐安排,每天一道主菜。\n"
    )


def _family_picks() -> str:
    return (
        "# 家人推荐的菜(虚构)\n\n"
        "- 凉拌花生米:先生最爱的下酒菜,希望每周至少出现一次。\n"
        "- 花生炖猪脚:女儿喜欢,周末有时间可以做。\n"
        "- 宫保鸡丁:全家都爱吃,先生说一定要放花生。\n"
        "- 番茄炒蛋:女儿点名要的,做法简单。\n"
        "- 清蒸鲈鱼:周敏自己想吃,清淡。\n"
        "- 白灼虾仁:女儿喜欢蘸酱油吃。\n"
    )


C03_PARTS = [f"manual-part-{n}.md" for n in range(1, 9)]
_C03_TOPICS = [
    "开机前检查",
    "日常点检",
    "润滑保养",
    "电气安全",
    "故障排查",
    "备件管理",
    "停机封存",
    "交接记录",
]


def _manual_part(n: int) -> str:
    topic = _C03_TOPICS[n - 1]
    lines = [f"# 设备维护手册 第 {n} 部分:{topic}(合成数据)", ""]
    for i in range(1, 151):
        lines.append(
            f"{n}.{i:03d} {topic}第 {i} 条:操作人员应按本条要求逐项确认设备状态,"
            f"发现异常立即停机并登记。"
        )
    return "\n".join(lines) + "\n"


C04_REPORT_PARTS = [f"checkup-part-{n}.md" for n in range(1, 5)]
_C04_FINDINGS = {
    1: "空腹血糖:6.4 mmol/L,偏高(参考范围 3.9-6.1 mmol/L)。",
    2: "体重指数(BMI):25.9,超重(参考范围 18.5-23.9)。",
    3: "甘油三酯:2.1 mmol/L,偏高(参考范围 0.45-1.7 mmol/L)。",
    4: "尿酸:445 μmol/L,偏高(参考范围 150-420 μmol/L)。",
}


# c04 第 1 轮可拿到的 4 份合计要小:模型会一次并行读完(c-base-1007),一轮盖满头尾就压不了。
_C04_REPORT_LINES = 155


def _checkup_part(n: int) -> str:
    lines = [f"# 王小雨 年度体检报告(虚构)第 {n} 部分", ""]
    for i in range(1, _C04_REPORT_LINES + 1):
        if i == _C04_REPORT_LINES // 2:
            lines.append(_C04_FINDINGS[n])
        lines.append(f"第 {n}-{i:03d} 项检查:结果在参考范围内,未见明显异常。")
    return "\n".join(lines) + "\n"


def _daily_log(title: str, entry: str, days: int) -> str:
    lines = [f"# 王小雨 {title}(虚构)", ""]
    for day in range(1, days + 1):
        lines.append(f"第 {day} 天:{entry.format(n=day % 5 + 1)}")
    return "\n".join(lines) + "\n"


def _exercise_log() -> str:
    return _daily_log(
        "近四个月运动记录",
        "步行约 {n} 千步,其余时间久坐办公;晚饭后偶尔散步,没有游泳、跑步等规律运动,膝盖无不适。",
        106,
    )


def _sleep_log() -> str:
    return _daily_log(
        "近四个月睡眠记录",
        "约 0 点 {n}5 分入睡,早上 7 点起床;睡前常看手机,夜里醒来一次,白天下午犯困。",
        117,
    )


def _diet_log() -> str:
    return _daily_log(
        "近四个月饮食记录",
        "早餐白粥配油条,午餐外卖盖饭,晚餐米饭两碗加红烧肉;下午喝一杯含糖奶茶(第 {n} 类口味)。",
        97,
    )


def _followup_notes() -> str:
    return _daily_log(
        "近四个月随访记录",
        "电话随访,客户自述无不适,血糖仪自测空腹 6.{n} mmol/L;提醒按时复查,客户表示知道了。",
        117,
    )


# B-175 P4(k01~k05):eval-kb 绑的三个知识库,顺序就是目录句柄 K1 / K2 / K3。描述原样填进控制台:
# k05 问的「年度体检」只在 hr 库的描述里提到,文档里没有(目录不是证据)。
KB_BASES = {
    "b140-kb-warranty": "家电各品类的保修期限、退换货与维修收费规定(合成数据)",
    "b140-kb-specs": "各型号家电的技术参数与待机能效分级(合成数据)",
    "b140-kb-hr": "员工差旅住宿、伙食补助、请假、报销流程与年度体检安排(合成数据)",
}

# 每个数只在一个库里出现(test_behavior_dataset 复核)。k03:型号 → 品类只在参数库,
# 品类 → 保修期只在保修库;k04:「A 级」在参数库是待机能效等级,在 hr 库是城市等级。
_KB_TEXTS: dict[str, dict[str, str]] = {
    "b140-kb-warranty": {
        "warranty-periods.md": (
            "# 家电保修期限(合成数据)\n\n"
            "整机保修期按品类计算,自购买发票日期起算。\n\n"
            "- 空气净化器:整机保修 18 个月,滤网属于耗材,不在保修范围内。\n"
            "- 除湿机:整机保修 26 个月,压缩机单独保修 74 个月。\n"
            "- 电水壶:整机保修 15 个月。\n"
            "- 落地扇:整机保修 13 个月。\n"
        ),
        "warranty-returns.md": (
            "# 退换货规定(合成数据)\n\n"
            "签收之日起 9 天内,商品无人为损坏且包装配件齐全的,可以无理由退货。\n"
            "签收后第 10 天至第 31 天内出现性能故障的,经检测确认后可以换同型号新机。\n"
            "超过 31 天的故障按保修处理,不再退换。\n"
        ),
        "warranty-repair-fees.md": (
            "# 保修期外维修收费(合成数据)\n\n"
            "保修期外的维修收取上门服务费 47 元每次,维修工时费每小时 63 元,更换的配件另行计价。\n"
            "保修期内的维修不收上门服务费和工时费。\n"
        ),
        "warranty-claims.md": (
            "# 保修申请(合成数据)\n\n"
            "申请保修需要提供购买发票或电子订单截图,以及产品机身上的序列号。\n"
            "序列号被涂改或撕毁的产品不予保修。受理后 3 个工作日内安排检测。\n"
        ),
        "warranty-exclusions.md": (
            "# 不在保修范围内的情形(合成数据)\n\n"
            "人为损坏、进水、跌落、使用非原装配件、自行拆修造成的故障,以及耗材的正常损耗,"
            "均不在保修范围内,按保修期外维修收费。\n"
        ),
    },
    "b140-kb-specs": {
        "specs-kx217.md": (
            "# KX-217 技术参数(合成数据)\n\n"
            "品类:空气净化器\n额定功率:46 W\n洁净空气量:383 立方米每小时\n"
            "建议使用面积:29 至 52 平方米\n"
        ),
        "specs-kx305.md": (
            "# KX-305 技术参数(合成数据)\n\n"
            "品类:除湿机\n额定功率:237 W\n日除湿量:21 升\n水箱容量:4.6 升\n"
        ),
        "specs-kx512.md": (
            "# KX-512 技术参数(合成数据)\n\n品类:电水壶\n额定功率:1730 W\n容量:1.7 升\n"
        ),
        "specs-kx640.md": (
            "# KX-640 技术参数(合成数据)\n\n品类:落地扇\n额定功率:38 W\n风速档位:5 档\n"
        ),
        "specs-standby-grades.md": (
            "# 待机能效分级(合成数据)\n\n"
            "按待机功率把整机分为三级:A 级待机功率不超过 0.37 W,B 级不超过 0.82 W,"
            "C 级不超过 1.45 W。目前在售的各型号均为 A 级。\n"
        ),
    },
    "b140-kb-hr": {
        "hr-travel-lodging.md": (
            "# 差旅住宿标准(合成数据)\n\n"
            "出差城市分为三级,住宿费按晚报销,超出部分由个人承担:\n\n"
            "- A 级城市:每晚不超过 735 元。\n"
            "- B 级城市:每晚不超过 468 元。\n"
            "- C 级城市:每晚不超过 356 元。\n"
        ),
        "hr-travel-meals.md": (
            "# 出差伙食补助(合成数据)\n\n"
            "出差期间按自然天数发放伙食补助,每天 97 元,不需要提供餐饮发票。\n"
        ),
        "hr-leave.md": (
            "# 请假制度(合成数据)\n\n"
            "入职满一年后每年享有带薪年假 6 天,满五年后每年 11 天。"
            "病假需要提供医院证明,事假需提前两个工作日申请。\n"
        ),
        "hr-reimbursement.md": (
            "# 报销流程(合成数据)\n\n"
            "出差结束后 14 天内提交报销单并附发票原件。"
            "单笔金额超过 2400 元的报销需要部门负责人签字。\n"
        ),
        "hr-overtime.md": (
            "# 加班与调休(合成数据)\n\n"
            "工作日加班可以按 1 比 1 调休,调休需在加班后 60 天内使用,过期作废。\n"
        ),
    },
}
KB_DOCS = {base: list(docs) for base, docs in _KB_TEXTS.items()}


def build() -> dict[str, str | bytes]:
    return {
        "report-300-lines.md": _report(),
        "sales.csv": _sales(),
        "regulation-60k.md": _regulation(),
        "product-notes.md": _product_notes(),
        "buggy-script.txt": _buggy_script(),
        "calc-args.txt": _calc_args(),
        "previous-plan.docx": _previous_plan(),
        "checkup-report.docx": _checkup_report(),
        **{name: _ledger(f) for f, name in enumerate(C01_FILES, start=1)},
        "dinner-brief.md": _dinner_brief(),
        **{name: _recipes(n) for n, name in enumerate(C02_RECIPES, start=1)},
        "family-picks.md": _family_picks(),
        **{name: _manual_part(n) for n, name in enumerate(C03_PARTS, start=1)},
        **{name: _checkup_part(n) for n, name in enumerate(C04_REPORT_PARTS, start=1)},
        "exercise-log.md": _exercise_log(),
        "sleep-log.md": _sleep_log(),
        "diet-log.md": _diet_log(),
        "followup-notes.md": _followup_notes(),
        **{
            f"kb/{base}/{doc}": text
            for base, docs in _KB_TEXTS.items()
            for doc, text in docs.items()
        },
    }


if __name__ == "__main__":
    for name, content in build().items():
        path = HERE / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        print(f"wrote {name}")
