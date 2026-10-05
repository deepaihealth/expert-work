"""生成 B-140 开局文件(全部合成数据)。改了这里要重跑,并把生成的文件一起提交:

uv run --no-sync python tools/eval/datasets/behavior/fixtures/make_fixtures.py
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).parent

# g02:三处过时内容在第 12 / 140 / 288 行(用例的 except_lines 与之对应)。
_SPECIAL = {
    12: "012. 数据截至 2026 年 6 月,以下各项按当月巡检记录整理。",
    140: "140. 本季度注册用户数为 987 人,较上季度持平。",
    288: "288. 本报告负责人:张工,如有疑问请联系负责人。",
}


def _report() -> str:
    lines = ["# 设备巡检报告(合成数据)"]
    for i in range(2, 301):
        lines.append(_SPECIAL.get(i, f"{i:03d}. 第 {i} 项设备巡检结果正常,无需处理。"))
    return "\n".join(lines) + "\n"


def _sales() -> str:
    # g08:A 组均值 104.5,B 组 209.0,C 组 35.5。
    rows = ["region,amount"]
    rows += [f"A,{v}" for v in range(100, 110)]
    rows += [f"B,{v}" for v in range(200, 220, 2)]
    rows += [f"C,{v}" for v in range(31, 41)]
    return "\n".join(rows) + "\n"


def build() -> dict[str, str]:
    return {"report-300-lines.md": _report(), "sales.csv": _sales()}


if __name__ == "__main__":
    for name, text in build().items():
        (HERE / name).write_text(text, encoding="utf-8")
        print(f"wrote {name}")
