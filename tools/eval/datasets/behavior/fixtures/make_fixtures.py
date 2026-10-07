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
_CATEGORIES = ["办公用品", "清洁用品", "饮用水", "打印耗材", "茶歇点心", "劳保用品", "绿植租摆", "快递费"]
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


_PROTEINS = ["鸡胸肉", "牛里脊", "北豆腐", "虾仁", "鲈鱼", "猪里脊", "鸡蛋", "鸭腿", "带鱼", "龙利鱼", "鸡腿肉", "牛腩"]
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
_C03_TOPICS = ["开机前检查", "日常点检", "润滑保养", "电气安全", "故障排查", "备件管理", "停机封存", "交接记录"]


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


def _checkup_part(n: int) -> str:
    lines = [f"# 王小雨 年度体检报告(虚构)第 {n} 部分", ""]
    for i in range(1, 251):
        if i == 125:
            lines.append(_C04_FINDINGS[n])
        lines.append(f"第 {n}-{i:03d} 项检查:结果在参考范围内,未见明显异常。")
    return "\n".join(lines) + "\n"


def _daily_log(title: str, entry: str) -> str:
    lines = [f"# 王小雨 {title}(虚构)", ""]
    for day in range(1, 121):
        lines.append(f"第 {day} 天:{entry.format(n=day % 5 + 1)}")
    return "\n".join(lines) + "\n"


def _exercise_log() -> str:
    return _daily_log(
        "近四个月运动记录",
        "步行约 {n} 千步,其余时间久坐办公;晚饭后偶尔散步,没有游泳、跑步等规律运动,膝盖无不适。",
    )


def _sleep_log() -> str:
    return _daily_log(
        "近四个月睡眠记录",
        "约 0 点 {n}5 分入睡,早上 7 点起床;睡前常看手机,夜里醒来一次,白天下午犯困。",
    )


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
    }


if __name__ == "__main__":
    for name, content in build().items():
        path = HERE / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        print(f"wrote {name}")
