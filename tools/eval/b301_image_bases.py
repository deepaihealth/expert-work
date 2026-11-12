"""B-301 PR 2 体检图集的 33 张底图:11 类 x 3 张,每张一个关键字段与一句问题(``@base`` 登记)。

所有文字是这里自己写的(店名、人名、编号都是编的);照片参数 ``None`` = 干净的截图 / 图表 / 表格。
斜拍类的 ``view_deg`` 是拍摄偏角 20 / 30 / 45 度。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np
from b301_image_draw import RGB, BaseSpec, Box, Canvas, Flat, font, seed_of, seven_segment
from PIL import Image, ImageDraw

Render = Callable[[], Flat]
#: 登记顺序就是图集顺序。
REGISTRY: list[tuple[BaseSpec, Render, Mapping[str, float] | None]] = []
_PHOTO: Mapping[str, float] = {"view_deg": 6.0, "tilt_deg": 2.0}


def base(
    image_class: str,
    base_id: str,
    key: str,
    kind: str,
    script: str,
    question: str,
    effects: Mapping[str, float] | None = None,
) -> Callable[[Render], Render]:
    def register(render: Render) -> Render:
        spec = BaseSpec(image_class, base_id, key, question, kind, script)
        REGISTRY.append((spec, render, effects))
        return render

    return register


def _rows(
    c: Canvas,
    rows: Sequence[tuple[str, str]],
    *,
    y: int,
    gap: int,
    size: int,
    value_x: int,
    key_label: str,
    face: str = "cjk",
) -> None:
    for index, (label, value) in enumerate(rows):
        top = y + index * gap
        c.text((70, top), label, face=face, size=size, fill=(110, 110, 115))
        c.text((value_x, top), value, face=face, size=size, key=label == key_label)


# --------------------------------------------------------------------------- 界面截图


@base(
    "ui",
    "ui-1",
    "DD20261103-4471",
    "code",
    "zh",
    "截图里的订单编号是什么?按图中原样只写出编号,不要其它文字。",
)
def ui_1() -> Flat:
    c = Canvas((900, 1600), (245, 246, 248))
    c.draw.rectangle((0, 0, 900, 70), fill=(30, 30, 30))
    c.text((40, 18), "9:41", face="sans-bold", size=30, fill=(255, 255, 255))
    c.draw.rectangle((0, 70, 900, 170), fill=(255, 255, 255))
    c.text((450, 120), "订单详情", size=40, anchor="mm")
    c.draw.rounded_rectangle((30, 210, 870, 900), 24, fill=(255, 255, 255))
    rows = [
        ("订单状态", "已发货"),
        ("下单时间", "2026-11-03 14:22"),
        ("订单编号", "DD20261103-4471"),
        ("收货人", "周*明"),
        ("实付金额", "¥86.50"),
    ]
    _rows(c, rows, y=260, gap=120, size=34, value_x=300, key_label="订单编号")
    c.draw.rounded_rectangle((60, 1400, 840, 1500), 50, fill=(255, 92, 60))
    c.text((450, 1450), "联系客服", size=38, fill=(255, 255, 255), anchor="mm")
    return c.flat()


@base("ui", "ui-2", "Harbor_Guest_5G", "name", "en",
      "What is the network name shown in this settings screen? "
      "Reply with the name only, exactly as written.")  # fmt: skip
def ui_2() -> Flat:
    c = Canvas((900, 1600), (242, 242, 247))
    c.text((40, 20), "9:41", face="sans-bold", size=30)
    c.text((40, 110), "Wi-Fi", face="sans-bold", size=56)
    c.draw.rounded_rectangle((30, 220, 870, 330), 20, fill=(255, 255, 255))
    c.text((70, 255), "Wi-Fi", face="sans", size=36)
    c.draw.rounded_rectangle((720, 250, 830, 300), 25, fill=(52, 199, 89))
    c.draw.rounded_rectangle((30, 380, 870, 900), 20, fill=(255, 255, 255))
    rows = [
        ("Network name", "Harbor_Guest_5G"),
        ("Security", "WPA3 Personal"),
        ("IP address", "192.168.31.24"),
        ("Router", "192.168.31.1"),
    ]
    _rows(c, rows, y=420, gap=120, size=32, value_x=420, key_label="Network name", face="sans")
    return c.flat()


@base("ui", "ui-3", "明天上午九点在三号会议室开会", "sentence", "zh",
      "截图里提到开会的那条消息原文是什么?只写出那条消息,不要其它文字。")  # fmt: skip
def ui_3() -> Flat:
    c = Canvas((900, 1600), (237, 237, 237))
    c.draw.rectangle((0, 0, 900, 150), fill=(247, 247, 247))
    c.text((450, 95), "项目群(5)", size=36, anchor="mm")
    bubbles = [
        (False, "大家好,下周的材料我已经发到邮箱"),
        (True, "收到,谢谢"),
        (False, "明天上午九点在三号会议室开会"),
        (True, "好的"),
    ]
    top = 230
    for mine, words in bubbles:
        width = int(font("cjk", 32).getlength(words)) + 60
        left = 900 - 60 - width if mine else 60
        fill = (149, 236, 105) if mine else (255, 255, 255)
        c.draw.rounded_rectangle((left, top, left + width, top + 90), 16, fill=fill)
        c.text((left + 30, top + 24), words, size=32, key="开会" in words)
        top += 160
    return c.flat()


# --------------------------------------------------------------------------- 单据(小字)


def _receipt(
    lines: Sequence[tuple[str, str]],
    header: str,
    footer: Sequence[str],
    *,
    total: str | None = None,
) -> Canvas:
    """竖长小票,小字(22 像素):整张约 2.4 百万像素,转过后读的 150 万像素副本里字更小(§11 #4)。"""
    c = Canvas((900, 1700), (250, 249, 244))
    c.text((450, 80), header, size=40, anchor="mm", key=header.startswith("青禾"))
    top = 170
    for left, right in lines:
        c.text((60, top), left, size=22)
        if right:
            c.text((840, top), right, size=22, anchor="ra")
        top += 46
    if total is not None:
        c.draw.line((60, top + 6, 840, top + 6), fill=(120, 120, 120), width=2)
        c.text((60, top + 30), "合计", size=30)
        c.text((840, top + 30), total, face="sans-bold", size=30, anchor="ra", key=True)
        top += 110
    for line in footer:
        c.text((60, top), line, size=20, fill=(90, 90, 90))
        top += 40
    return c


@base("receipt", "rc-1", "128.46", "number", "zh", "小票上的合计金额是多少?只写数字。",
      {"view_deg": 8.0, "tilt_deg": -3.0, "blur": 1.2})  # fmt: skip
def receipt_1() -> Flat:
    items = [
        ("纯牛奶 250ml\u00d712", "49.90"),
        ("全麦面包", "12.80"),
        ("鸡蛋 30枚", "26.50"),
        ("苹果 1.2kg", "15.48"),
        ("西红柿 0.8kg", "6.32"),
        ("酸奶 8连杯", "13.90"),
        ("纸巾 3包", "3.56"),
    ]
    total = sum(round(float(price) * 100) for _, price in items) / 100
    lines = [("商品", "金额"), *items, ("件数 7", "")]
    footer = ["收银员 03  机号 2", "谢谢惠顾,欢迎再次光临"]
    return _receipt(lines, "惠邻生活超市", footer, total=f"{total:.2f}").flat()


@base("receipt", "rc-2", "2026-03-17", "date", "en",
      "What date is printed on this receipt? Reply with the date only, exactly as printed.",
      {"view_deg": 5.0, "tilt_deg": 2.5, "blur": 1.2})  # fmt: skip
def receipt_2() -> Flat:
    c = Canvas((900, 1700), (250, 249, 244))
    c.text((450, 80), "GREENLEAF MARKET", face="sans-bold", size=40, anchor="mm")
    c.text((450, 140), "41 Linden Road", face="mono", size=24, anchor="mm")
    c.text((60, 210), "Date:", face="mono", size=26)
    c.text((200, 210), "2026-03-17", face="mono", size=26, key=True)
    c.text((520, 210), "18:42", face="mono", size=26)
    top = 290
    for name, price in [("Bananas", "2.40"), ("Rice 2kg", "6.95"), ("Tea", "4.20")]:
        c.text((60, top), name, face="mono", size=24)
        c.text((840, top), price, face="mono", size=24, anchor="ra")
        top += 50
    c.text((60, top + 30), "TOTAL", face="mono", size=26)
    c.text((840, top + 30), "13.55", face="mono", size=26, anchor="ra")
    c.text((60, top + 110), "Thank you for shopping with us", face="mono", size=22)
    return c.flat()


@base("receipt", "rc-3", "青禾便利店(南湖店)", "name", "zh",
      "小票顶部的店名是什么?按原样只写店名。",
      {"view_deg": 10.0, "tilt_deg": 1.5, "blur": 1.2})  # fmt: skip
def receipt_3() -> Flat:
    lines = [("饭团 金枪鱼", "7.50"), ("矿泉水 550ml", "2.00"), ("关东煮 3串", "9.00")]
    footer = ["合计 18.50", "交易时间 2026-10-21 08:15"]
    return _receipt(lines, "青禾便利店(南湖店)", footer).flat()


# --------------------------------------------------------------------------- 包装


@base("package", "pk-1", "2027-05-08", "date", "zh",
      "包装上写的保质期到哪一天?按图中原样只写日期。", _PHOTO)  # fmt: skip
def package_1() -> Flat:
    c = Canvas((1500, 1100), (232, 92, 64))
    c.draw.rectangle((0, 760, 1500, 1100), fill=(250, 240, 220))
    c.text((80, 120), "手工鸡蛋卷", size=110, fill=(255, 255, 255))
    c.text((80, 300), "香脆可口 · 独立小包装", size=48, fill=(255, 236, 200))
    c.text((80, 800), "生产日期 2026-11-08", size=40)
    c.text((80, 880), "保质期至", size=40)
    c.text((80 + int(font("cjk", 40).getlength("保质期至 ")), 880), "2027-05-08", size=40, key=True)
    c.text((900, 800), "贮存:阴凉干燥处", size=34)
    return c.flat()


@base("package", "pk-2", "Oat Crunch Granola", "name", "en",
      "What is the product name on this package? Reply with the name only.", _PHOTO)  # fmt: skip
def package_2() -> Flat:
    c = Canvas((1500, 1100), (60, 110, 70))
    cream = (250, 245, 230)
    c.text((750, 300), "Oat Crunch Granola", face="sans-bold", size=110, fill=cream, anchor="mm",
           key=True)  # fmt: skip
    c.text((750, 450), "Honey & Almond  ·  Whole Grain", face="sans", size=48,
           fill=(230, 220, 190), anchor="mm")  # fmt: skip
    c.draw.ellipse((600, 560, 900, 860), fill=(214, 170, 90))
    c.text((750, 960), "NET WT 375 g", face="sans", size=44, fill=cream, anchor="mm")
    return c.flat()


@base("package", "pk-3", "450g", "number", "zh", "包装上的净含量是多少?只写数值和单位。", _PHOTO)
def package_3() -> Flat:
    c = Canvas((1500, 1100), (245, 236, 210))
    c.text((80, 100), "山野黑豆", size=120, fill=(40, 40, 40))
    c.text((80, 300), "非转基因 · 颗粒饱满", size=48, fill=(120, 80, 40))
    c.text((80, 820), "净含量", size=52)
    c.text((300, 820), "450g", face="sans-bold", size=56, key=True)
    c.text((800, 820), "产地:黑龙江", size=44)
    return c.flat()


# --------------------------------------------------------------------------- 竖排中文


@base("vertical", "vt-1", "山前小路通向旧书院", "sentence", "zh",
      "这幅竖排文字的第一列(最右边一列)写的是什么?只写这一列的文字。",
      {"view_deg": 4.0, "tilt_deg": 1.0})  # fmt: skip
def vertical_1() -> Flat:
    c = Canvas((1100, 1500), (246, 240, 226))
    c.vertical(880, 140, "山前小路通向旧书院", size=90, key=True)
    c.vertical(700, 140, "溪水绕过石桥", size=90)
    c.vertical(520, 140, "秋日清晨", size=90)
    return c.flat()


@base("vertical", "vt-2", "林记老茶铺", "name", "zh", "竖排招牌上的店名是什么?只写店名。",
      {"view_deg": 8.0, "tilt_deg": -2.0})  # fmt: skip
def vertical_2() -> Flat:
    c = Canvas((900, 1500), (110, 40, 30))
    c.draw.rectangle((280, 80, 620, 1420), fill=(30, 20, 15))
    c.vertical(450, 180, "林记老茶铺", size=190, fill=(230, 190, 90), key=True)
    return c.flat()


@base("vertical", "vt-3", "本店四月十二日起暂停营业", "sentence", "zh",
      "竖排告示里写的哪一天起暂停营业?只写出含日期的那一列原文。",
      {"view_deg": 5.0, "tilt_deg": 2.0})  # fmt: skip
def vertical_3() -> Flat:
    c = Canvas((1000, 1500), (255, 255, 255))
    c.draw.rectangle((40, 40, 960, 1460), outline=(180, 30, 30), width=8)
    c.vertical(760, 160, "本店四月十二日起暂停营业", size=82, key=True)
    c.vertical(580, 160, "敬请谅解", size=82)
    c.vertical(300, 900, "店主", size=60)
    return c.flat()


# --------------------------------------------------------------------------- 斜拍 20 / 30 / 45 度


@base("oblique", "ob-1", "Riverside Terminal", "name", "en",
      "What destination name is on this sign? Reply with the name only.",
      {"view_deg": 20.0, "tilt_deg": 4.0})  # fmt: skip
def oblique_1() -> Flat:
    c = Canvas((1600, 800), (20, 70, 140))
    white = (255, 255, 255)
    c.text((80, 80), "Platform 7", face="sans", size=80, fill=white)
    c.text((80, 300), "Riverside Terminal", face="sans-bold", size=150, fill=white, key=True)
    c.text((80, 620), "Departs every 15 min", face="sans", size=60, fill=(200, 220, 255))
    return c.flat()


@base("oblique", "ob-2", "停车场入口在东侧", "sentence", "zh",
      "告示牌上关于停车场入口的那句话原文是什么?只写这一句。",
      {"view_deg": 30.0, "tilt_deg": -4.0})  # fmt: skip
def oblique_2() -> Flat:
    c = Canvas((1600, 900), (250, 250, 250))
    c.draw.rectangle((30, 30, 1570, 870), outline=(0, 90, 60), width=14)
    c.text((800, 280), "停车场入口在东侧", size=150, fill=(0, 90, 60), anchor="mm", key=True)
    c.text((800, 600), "请勿在此停留", size=100, fill=(60, 60, 60), anchor="mm")
    return c.flat()


@base("oblique", "ob-3", "36.80", "number", "zh", "价签上的价格数字是多少?只写数字。",
      {"view_deg": 45.0, "tilt_deg": 3.0})  # fmt: skip
def oblique_3() -> Flat:
    c = Canvas((1400, 1000), (255, 222, 40))
    c.text((80, 80), "特价  有机番茄", size=100)
    c.text((80, 330), "36.80", face="sans-bold", size=300, key=True)
    c.text((1000, 520), "元/盒", size=90)
    c.text((80, 820), "规格 500g", size=60)
    return c.flat()


# --------------------------------------------------------------------------- 图表


def _axes(c: Canvas, box: Box) -> None:
    left, top, right, bottom = box
    c.draw.line((left, top, left, bottom), fill=(60, 60, 60), width=3)
    c.draw.line((left, bottom, right, bottom), fill=(60, 60, 60), width=3)


@base("chart", "ch-1", "Monthly Active Users 2026", "sentence", "en",
      "What is the chart title? Reply with the title only, exactly as written.")  # fmt: skip
def chart_1() -> Flat:
    c = Canvas((1600, 1100))
    c.text((800, 70), "Monthly Active Users 2026", face="sans-bold", size=56, anchor="mm",
           key=True)  # fmt: skip
    _axes(c, (160, 160, 1520, 960))
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun"]
    for index, (month, value) in enumerate(zip(months, [38, 42, 47, 51, 58, 63], strict=True)):
        x = 230 + index * 210
        c.draw.rectangle((x, 960 - value * 12, x + 120, 960), fill=(66, 133, 244))
        c.text((x + 60, 985), month, face="sans", size=34, anchor="ma")
        c.text((x + 60, 960 - value * 12 - 45), f"{value}k", face="sans", size=30, anchor="ma")
    return c.flat()


@base("chart", "ch-2", "87.5", "number", "zh", "折线图上标注的峰值是多少?只写数字。")
def chart_2() -> Flat:
    c = Canvas((1600, 1100))
    c.text((800, 70), "近八周到店人数", size=50, anchor="mm")
    _axes(c, (160, 160, 1520, 960))
    values = [52.0, 61.5, 58.0, 70.5, 87.5, 76.0, 69.0, 72.5]
    points = [(220 + i * 175, 960 - int(v * 8.5)) for i, v in enumerate(values)]
    c.draw.line(points, fill=(219, 68, 55), width=6)
    for x, y in points:
        c.draw.ellipse((x - 10, y - 10, x + 10, y + 10), fill=(219, 68, 55))
    peak_x, peak_y = points[4]
    c.text((peak_x, peak_y - 90), "峰值", size=36, anchor="ma")
    c.text((peak_x, peak_y - 50), "87.5", face="sans-bold", size=36, anchor="ma", key=True)
    for i in range(8):
        c.text((220 + i * 175, 985), f"第{i + 1}周", size=30, anchor="ma")
    return c.flat()


@base("chart", "ch-3", "42%", "number", "zh", "饼图里华东区的占比是多少?只写百分数。")
def chart_3() -> Flat:
    c = Canvas((1600, 1100))
    c.text((800, 70), "各区域销售占比", size=50, anchor="mm")
    parts: list[tuple[str, int, RGB]] = [
        ("华东区", 42, (66, 133, 244)),
        ("华南区", 27, (219, 68, 55)),
        ("华北区", 19, (244, 180, 0)),
        ("西部", 12, (15, 157, 88)),
    ]
    start = -90.0
    for _name, share, colour in parts:
        end = start + share * 3.6
        c.draw.pieslice((150, 200, 850, 900), start, end, fill=colour)
        start = end
    for index, (name, share, colour) in enumerate(parts):
        top = 330 + index * 120
        c.draw.rectangle((980, top, 1030, top + 50), fill=colour)
        c.text((1060, top), name, size=44)
        c.text((1300, top), f"{share}%", face="sans-bold", size=44, key=name == "华东区")
    return c.flat()


# --------------------------------------------------------------------------- 文档照片


@base("document", "dc-1", "关于调整办公区开放时间的通知", "sentence", "zh",
      "这份文件的标题是什么?只写标题原文。",
      {"view_deg": 9.0, "tilt_deg": -2.0, "blur": 1.3})  # fmt: skip
def document_1() -> Flat:
    c = Canvas((1500, 2000))
    c.text((750, 160), "关于调整办公区开放时间的通知", size=58, anchor="mm", key=True)
    body = [
        "各部门:",
        "  为配合楼宇设备检修,自下周一起办公区开放时间调整为",
        "工作日上午七点半至晚上九点,周末暂不开放。检修期间",
        "电梯分时段停用,请提前安排外出与来访。",
        "  如有加班需要,请提前一天向行政部登记,登记后凭工牌",
        "进入。给大家带来的不便,敬请谅解。",
    ]
    for index, line in enumerate(body):
        c.text((120, 320 + index * 70), line, size=40)
    c.text((1380, 900), "行政部", size=40, anchor="ra")
    c.text((1380, 970), "2026年11月10日", size=40, anchor="ra")
    return c.flat()


@base("document", "dc-2", "Margaret Ellison", "name", "en",
      "Who signed this letter? Reply with the full name only.",
      {"view_deg": 7.0, "tilt_deg": 2.0, "blur": 1.3})  # fmt: skip
def document_2() -> Flat:
    c = Canvas((1500, 2000))
    lines = [
        "Dear Mr. Okafor,",
        "",
        "Thank you for hosting our team last week. The site visit",
        "answered most of our questions, and we will send the revised",
        "schedule before the end of the month.",
        "",
        "Kind regards,",
    ]
    for index, line in enumerate(lines):
        c.text((140, 200 + index * 70), line, face="sans", size=42)
    c.text((140, 760), "Margaret Ellison", face="sans-bold", size=46, key=True)
    c.text((140, 830), "Project Coordinator", face="sans", size=38)
    return c.flat()


@base("document", "dc-3", "HT-2026-0915-07", "code", "zh", "合同编号是什么?按原样只写编号。",
      {"view_deg": 11.0, "tilt_deg": 1.0, "blur": 1.3})  # fmt: skip
def document_3() -> Flat:
    c = Canvas((1500, 2000))
    c.text((750, 150), "设备租赁合同", size=64, anchor="mm")
    c.text((120, 300), "合同编号:", size=42)
    c.text((340, 300), "HT-2026-0915-07", face="sans-bold", size=42, key=True)
    rows = [
        "出租方:安澜机电有限公司",
        "承租方:星河物业服务中心",
        "租赁期限:十二个月",
        "租金按季度支付",
    ]
    for index, line in enumerate(rows):
        c.text((120, 400 + index * 80), line, size=40)
    for y in range(820, 1900, 70):
        c.draw.line((120, y, 1380, y), fill=(225, 225, 225), width=2)
    return c.flat()


# --------------------------------------------------------------------------- 手写(逐字抖动的模拟)


def _handwriting(lines: Sequence[str], *, size: int, seed: int, face: str) -> Flat:
    """横格纸、蓝黑墨水,每个字各自歪一点、上下错一点、大小不一;第一行是关键字段所在的行。"""
    rng = np.random.default_rng(seed)
    c = Canvas((1500, 1000), (252, 250, 240))
    for y in range(160, 1000, 110):
        c.draw.line((0, y, 1500, y), fill=(170, 200, 230), width=2)
    for row, line in enumerate(lines):
        x, base_y = 110, 250 + row * 220
        for ch in line:
            if ch == " ":
                x += size // 2
                continue
            glyph_size = int(size * rng.uniform(0.9, 1.1))
            fnt = font(face, glyph_size)
            tile = Image.new("L", (glyph_size * 2, glyph_size * 2), 0)
            ImageDraw.Draw(tile).text((glyph_size // 2, glyph_size // 3), ch, font=fnt, fill=255)
            tile = tile.rotate(float(rng.uniform(-8, 8)), resample=Image.Resampling.BICUBIC)
            ink = Image.new("RGB", tile.size, (25, 40, 110))
            y = base_y + int(rng.uniform(-6, 6)) - glyph_size // 3
            c.image.paste(ink, (x - glyph_size // 2, y), tile)
            x += int(fnt.getlength(ch) * rng.uniform(0.95, 1.12))
        c.texts.append((line, face))
        if row == 0:
            c.key_box = (110, base_y - size // 2, x, base_y + size)
    return c.flat()


@base("handwriting", "hw-1", "周五前把报告交给王老师", "sentence", "zh",
      "这张手写便条写的是什么?只写便条原文。", {"view_deg": 5.0, "tilt_deg": -2.0})  # fmt: skip
def handwriting_1() -> Flat:
    return _handwriting(["周五前把报告交给王老师"], size=78, seed=seed_of("hw-1"), face="cjk")


@base("handwriting", "hw-2", "4:30 pm", "number", "en",
      "What time does the handwritten note say to meet? Reply with the time only, as written.",
      {"view_deg": 6.0, "tilt_deg": 2.0})  # fmt: skip
def handwriting_2() -> Flat:
    return _handwriting(["Meet at 4:30 pm"], size=84, seed=seed_of("hw-2"), face="sans")


@base("handwriting", "hw-3", "128/82", "number", "zh", "手写记录里血压是多少?只写数值。",
      {"view_deg": 4.0, "tilt_deg": 1.5})  # fmt: skip
def handwriting_3() -> Flat:
    return _handwriting(["血压 128/82", "心率 76"], size=80, seed=seed_of("hw-3"), face="cjk")


# --------------------------------------------------------------------------- 标签 / 铭牌


@base("label", "lb-1", "KX-450B", "code", "zh", "铭牌上的型号是什么?按原样只写型号。",
      {"view_deg": 8.0, "tilt_deg": 2.0, "noise": 6.0})  # fmt: skip
def label_1() -> Flat:
    c = Canvas((1400, 900), (200, 204, 208))
    c.draw.rectangle((30, 30, 1370, 870), outline=(90, 95, 100), width=10)
    c.text((700, 110), "安澜机电 · 空气压缩机", size=54, anchor="mm")
    rows = [
        ("型号", "KX-450B"),
        ("额定电压", "220V~ 50Hz"),
        ("额定功率", "1.5kW"),
        ("出厂日期", "2025-06"),
    ]
    for index, (label, value) in enumerate(rows):
        top = 230 + index * 140
        c.text((120, top), label, size=48)
        c.text((520, top), value, face="sans-bold", size=50, key=label == "型号")
    return c.flat()


@base("label", "lb-2", "7731-0428", "code", "en",
      "What is the serial number on this label? Reply with the number only.",
      {"view_deg": 6.0, "tilt_deg": -3.0, "noise": 6.0})  # fmt: skip
def label_2() -> Flat:
    c = Canvas((1400, 800), (235, 235, 235))
    c.text((80, 90), "MODEL  TR-9 Thermostat", face="sans-bold", size=58)
    c.text((80, 260), "Serial No.", face="sans", size=56)
    c.text((440, 260), "7731-0428", face="mono", size=62, key=True)
    c.text((80, 430), "Input 24 V AC   Made in 2025", face="sans", size=46)
    c.draw.rectangle((80, 560, 900, 700), fill=(0, 0, 0))
    for i in range(0, 820, 14):
        c.draw.rectangle((80 + i, 560, 80 + i + (i % 3) + 3, 700), fill=(255, 255, 255))
    return c.flat()


@base("label", "lb-3", "每日两次,每次一片", "sentence", "zh",
      "标签上的用法用量是怎么写的?只写冒号后面那句。", _PHOTO)  # fmt: skip
def label_3() -> Flat:
    c = Canvas((1400, 900), (255, 255, 255))
    c.draw.rectangle((0, 0, 1400, 160), fill=(0, 120, 190))
    c.text((700, 80), "维生素C咀嚼片", size=72, fill=(255, 255, 255), anchor="mm")
    c.text((100, 260), "用法用量:", size=52)
    c.text((380, 260), "每日两次,每次一片", size=52, key=True)
    c.text((100, 400), "规格:每片1.0g  贮藏:密封", size=46)
    c.text((100, 520), "本品不能代替药物", size=46)
    return c.flat()


# --------------------------------------------------------------------------- 表格


def _table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    title: str,
    face: str,
    key: tuple[int, int],
) -> Flat:
    c = Canvas((1600, 1200))
    c.text((800, 80), title, face="sans-bold" if face == "sans" else "cjk", size=52, anchor="mm")
    width, top, height = 1440 // len(headers), 180, 110
    for r, cells in enumerate([headers, *rows]):
        y = top + r * height
        if r == 0:
            c.draw.rectangle((80, y, 1520, y + height), fill=(230, 236, 245))
        for col, cell in enumerate(cells):
            x = 80 + col * width
            c.draw.rectangle((x, y, x + width, y + height), outline=(120, 120, 120), width=2)
            c.text((x + 24, y + 32), cell, face=face, size=40, key=(r - 1, col) == key)
    return c.flat()


@base("table", "tb-1", "5.52", "number", "zh", "表格中总胆固醇的结果是多少?只写数字。")
def table_1() -> Flat:
    rows = [
        ["白细胞计数", "6.21", "3.5-9.5", "10^9/L"],
        ["血红蛋白", "142", "130-175", "g/L"],
        ["总胆固醇", "5.52", "<5.18", "mmol/L"],
        ["甘油三酯", "1.36", "<1.70", "mmol/L"],
        ["空腹血糖", "6.4", "3.9-6.1", "mmol/L"],
    ]
    headers = ["项目", "结果", "参考范围", "单位"]
    return _table(headers, rows, title="检验结果", face="cjk", key=(2, 1))


@base("table", "tb-2", "Room 204", "name", "en",
      "In the table, which room is listed for Thursday? Reply with the room only.")  # fmt: skip
def table_2() -> Flat:
    rows = [
        ["Monday", "Room 112", "Ms. Patel"],
        ["Tuesday", "Room 118", "Mr. Novak"],
        ["Wednesday", "Lab 3", "Ms. Patel"],
        ["Thursday", "Room 204", "Dr. Lindqvist"],
        ["Friday", "Hall B", "Mr. Novak"],
    ]
    headers = ["Day", "Room", "Teacher"]
    return _table(headers, rows, title="Weekly Schedule", face="sans", key=(3, 1))


@base("table", "tb-3", "23.50", "number", "zh", "表格里打印纸的单价是多少?只写数字。")
def table_3() -> Flat:
    rows = [
        ["中性笔", "0.5mm", "2.80", "20"],
        ["打印纸", "A4 500张", "23.50", "6"],
        ["文件夹", "蓝色", "4.60", "12"],
        ["订书钉", "24/6", "3.20", "5"],
    ]
    headers = ["品名", "规格", "单价", "数量"]
    return _table(headers, rows, title="办公用品采购单", face="cjk", key=(1, 2))


# --------------------------------------------------------------------------- 数字显示屏(七段码)


def _display(
    text: str, *, unit: str, colours: tuple[RGB, RGB, RGB, RGB | None], label: str
) -> Flat:
    """``colours`` = (机身, 屏幕底色, 点亮的笔段, 没点亮的淡笔段 / ``None``)。"""
    body, screen, on, off = colours
    w, h = 1600, 1200
    c = Canvas((w, h), (210, 210, 205))
    c.draw.rounded_rectangle((w // 8, h // 6, w * 7 // 8, h * 5 // 6), 60, fill=body)
    c.draw.rectangle((w // 4, h // 3, w * 3 // 4, h * 3 // 5), fill=screen)
    key_box = seven_segment(c, w // 4 + 60, h // 3 + 50, text, height=int(h * 0.2), on=on, off=off)
    c.text((w * 3 // 4 - 30, h * 3 // 5 - 70), unit, face="sans-bold", size=46, fill=on,
           anchor="ra")  # fmt: skip
    c.text((w // 2, h * 3 // 4), label, face="sans-bold", size=48, fill=(235, 235, 235),
           anchor="mm")  # fmt: skip
    c.key_box = key_box
    return c.flat()


@base("display", "ds-1", "69.1", "number", "zh", "显示屏上的读数是多少?只写数字。",
      {"view_deg": 12.0, "tilt_deg": 3.0, "blur": 1.6})  # fmt: skip
def display_1() -> Flat:
    colours = ((40, 40, 44), (160, 175, 150), (25, 30, 25), (148, 162, 140))
    return _display("69.1", unit="kg", colours=colours, label="SCALE")


@base("display", "ds-2", "08:47", "number", "en",
      "What time does the clock display show? Reply with the time only.",
      {"view_deg": 8.0, "tilt_deg": -2.0, "blur": 1.4})  # fmt: skip
def display_2() -> Flat:
    colours = ((25, 25, 28), (10, 10, 10), (255, 60, 40), None)
    return _display("08:47", unit="AM", colours=colours, label="ALARM")


@base("display", "ds-3", "37.6", "number", "zh", "体温计显示的温度数字是多少?只写数字。",
      {"view_deg": 10.0, "tilt_deg": 2.0, "blur": 1.5})  # fmt: skip
def display_3() -> Flat:
    colours = ((230, 232, 236), (190, 205, 200), (20, 25, 25), (178, 192, 188))
    return _display("37.6", unit="C", colours=colours, label="TEMP")
