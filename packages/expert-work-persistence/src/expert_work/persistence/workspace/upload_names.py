"""B-275 —— 换过名的上传在模型看到的那一行后面带上原名。

B-262 起同名、内容不同的上传存成 ``<原名>-<内容 sha256 前 8 位>``;模型只看到实际名的话,
按「交付件沿用附件名」写提示词的对接方会把后缀带进交付件。主 run 的
``[file attached: …]``(control-plane ``_file_attached_line``)与委派出去的子代种子里的附件
清单(orchestrator ``build_seed_content``)都在实际名后面接上 :func:`original_name_note`,
同一种形式、同一条安全规则,所以放在两边都依赖的这一层。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["original_name_note"]

#: 叶子名里不允许的字符 —— 与上传时净化文件名的字符集(control-plane ``_SAFE_STEM_RE``:
#: Unicode 字母 / 数字 / 下划线,加 ``.``、``-``)互补。空格、``/``、``\\``、括号、方括号、
#: 换行都在外面,所以接在行里的原名只有一种读法。
_UNSAFE_LEAF_CHAR: Final[re.Pattern[str]] = re.compile(r"[^\w.-]")


def _is_safe_leaf(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and _UNSAFE_LEAF_CHAR.search(name) is None


def original_name_note(stored: str, original: str | None) -> str:
    """``stored``(``uploads/<实际名>``)后面要接的原名说明:`` (original name: <原名>)``。

    没有原名、原名就是实际名,或原名不是平台写得出的叶子名时回空串 —— 那一行与 B-275 之前
    逐字相同。admin-ui ``turn/attachment_markers.ts`` 按这个形状把历史轮里的附件行摘出来,
    改这里要同步改那边。
    """
    if original is None or original == stored.rsplit("/", 1)[-1] or not _is_safe_leaf(original):
        return ""
    return f" (original name: {original})"
