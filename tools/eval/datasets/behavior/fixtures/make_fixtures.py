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
    }


if __name__ == "__main__":
    for name, content in build().items():
        path = HERE / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        print(f"wrote {name}")
