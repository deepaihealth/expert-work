"""B-209 PR-2 (review H4) —— a real glm-ocr ``layout_parsing`` response, recorded
once (2026-10-22) for a synthetic landscape page, and the code that draws that
page. Zero tenant content: a few made-up lines and a bar chart.

The page is 1600 x 1200 px with the chart at x 1000..1500, y 300..800 — past
1000 on the x axis, so boxes in a 0..1000 normalized frame could not reach
it. ``RESPONSE`` is the vendor's answer for exactly this page as the
pipeline renders it (``PDF_PAGE_POINTS``); no credential is in it.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

PAGE_SIZE = (1600, 1200)
#: Where the chart is drawn on the page (pixels).
CHART_BOX = (1000, 300, 1500, 800)
#: The PDF page the picture fills: 576 x 432 pt renders at 200 DPI to
#: exactly 1600 x 1200 px (under the 2,000 px cap) — the image the pipeline sends.
PDF_PAGE_POINTS = (576, 432)

_LINES = (
    "Maintenance schedule for the demo unit",
    "1. Check the power cord before switching on.",
    "2. Wipe the housing with a dry cloth every week.",
    "3. Replace the filter when it is visibly dusty.",
    "4. Let the unit rest for 30 minutes after 8 hours.",
)


def page_image() -> bytes:
    """The recorded page, as PNG."""
    page = Image.new("RGB", PAGE_SIZE, (255, 255, 255))
    draw = ImageDraw.Draw(page)
    font = ImageFont.load_default(size=32)
    for row, line in enumerate(_LINES):
        draw.text((80, 120 + row * 70), line, fill=(0, 0, 0), font=font)
    x0, y0, x1, y1 = CHART_BOX
    draw.line((x0 + 40, y1 - 40, x1 - 20, y1 - 40), fill=(0, 0, 0), width=4)
    draw.line((x0 + 40, y0 + 20, x0 + 40, y1 - 40), fill=(0, 0, 0), width=4)
    for bar, value in enumerate((120, 260, 190, 330)):
        left = x0 + 80 + bar * 100
        draw.rectangle((left, y1 - 40 - value, left + 60, y1 - 40), fill=(40, 90, 200))
    out = io.BytesIO()
    page.save(out, format="PNG")
    return out.getvalue()


#: Recorded 2026-10-22 (glm-ocr, 265 tokens); ``md_results`` and ``layout_details`` as returned.
RESPONSE: dict[str, object] = {
    "created": 1792660533,
    "data_info": {"num_pages": 1, "pages": [{"height": 1200, "width": 1600}]},
    "id": "2026102217144238d72ac31ca24d4e",
    "layout_details": [
        [
            {
                "bbox_2d": [75, 122, 661, 162],
                "content": "Maintenance schedule for the demo unit",
                "height": 1200,
                "index": 0,
                "label": "text",
                "native_label": "text",
                "width": 1600,
            },
            {
                "bbox_2d": [76, 191, 732, 232],
                "content": "1. Check the power cord before switching on.",
                "height": 1200,
                "index": 1,
                "label": "text",
                "native_label": "text",
                "width": 1600,
            },
            {
                "bbox_2d": [74, 261, 767, 303],
                "content": "2. Wipe the housing with a dry cloth every week.",
                "height": 1200,
                "index": 2,
                "label": "text",
                "native_label": "text",
                "width": 1600,
            },
            {
                "bbox_2d": [75, 332, 683, 372],
                "content": "3. Replace the filter when it is visibly dusty.",
                "height": 1200,
                "index": 3,
                "label": "text",
                "native_label": "text",
                "width": 1600,
            },
            {
                "bbox_2d": [75, 402, 741, 441],
                "content": "4. Let the unit rest for 30 minutes after 8 hours.",
                "height": 1200,
                "index": 4,
                "label": "text",
                "native_label": "text",
                "width": 1600,
            },
            {
                "bbox_2d": [1029, 312, 1482, 769],
                "height": 1200,
                "index": 5,
                "label": "image",
                "native_label": "image",
                "width": 1600,
            },
        ]
    ],
    "layout_visualization": [],
    "md_results": "Maintenance schedule for the demo unit\n"
    "\n"
    "1. Check the power cord before switching on.\n"
    "\n"
    "2. Wipe the housing with a dry cloth every week.\n"
    "\n"
    "3. Replace the filter when it is visibly dusty.\n"
    "\n"
    "4. Let the unit rest for 30 minutes after 8 hours.\n"
    "\n"
    "![](page=0,bbox=[1029, 312, 1482, 769])",
    "model": "glm-ocr",
    "request_id": "2026102217144238d72ac31ca24d4e",
    "usage": {"completion_tokens": 65, "prompt_tokens": 200, "total_tokens": 265},
}
