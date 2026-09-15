"""Lenient read-back for manifests **we** wrote into JSONB (B-61, spec §七).

写入严格、读回宽容:YAML / 接口进来的配置照旧 ``extra="forbid"`` 挡错字,而从我们
自己库里读回来的 ``spec_json`` 多一个不认识的键 —— 新版本写的、回滚后的旧版本读的
—— 不该让整个 agent(或整个模板目录)起不来。严格该管的是**人写的输入**,不是
**我们自己写出去又读回来的数据**。

这条一次性根治整类问题:此后任何新增 spec 字段都不再有回滚坑。放在这里而不是某个
store 模块里,是因为**每一处**读回我们自己存的 manifest 的地方都要走它 —— 少接一处,
回滚就沿那一条没人记得检查的路径继续坏。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from pydantic import ValidationError

from expert_work.protocol import AgentSpec

logger = logging.getLogger("expert_work.persistence.stored_spec")


def _drop_key_at(payload: dict[str, Any], loc: tuple[int | str, ...]) -> str | None:
    """Delete the key ``loc`` points at; return the path deleted, or ``None``.

    ``loc`` 来自 pydantic 的错误项。判别式联合会在里面多插一段标签
    (``('spec','tools',0,'mcp','future_key')`` 的那个 ``'mcp'``),原始 JSON 里
    没有这一层,所以走不通的路径段直接跳过。
    """
    cursor: Any = payload
    walked: list[str] = []
    for part in loc[:-1]:
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif (
            isinstance(cursor, list)
            and isinstance(part, int)
            and -len(cursor) <= part < len(cursor)
        ):
            cursor = cursor[part]
        else:
            continue  # 联合标签那一段,不是真实路径。
        walked.append(str(part))
    key = loc[-1]
    if not isinstance(cursor, dict) or key not in cursor:
        return None
    del cursor[key]
    walked.append(str(key))
    return ".".join(walked)


def load_stored_spec(payload: dict[str, Any]) -> AgentSpec:
    """Validate a manifest **we wrote ourselves**, ignoring keys we don't know.

    只在 ``extra_forbidden`` 这一种失败上剔键重试:无条件剔键会把真正的数据
    损坏一起吞掉,那比要修的问题更糟。夹杂了别的错误也原样抛 —— 那一行并没有
    被救回来,报的必须是完整的原因。

    代价(review M-4):回滚窗口里读回来的是**剔完键**的 ``AgentSpec``;如果这
    期间又编辑并保存了这个 agent(``update_spec``),写回去的是这个剔过键的
    对象,被剔掉的那个键就永久从线上行里消失了 —— 再升回新版本也拿不回来,
    只能翻 ``agent_spec_revision`` 历史。``publish_draft``(``agent_spec/sql.py``)
    搬的是 ``row.draft_spec_json`` 原始 JSON,不经过这里,不受影响。
    """
    try:
        return AgentSpec.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        if not all(e["type"] == "extra_forbidden" for e in errors):
            raise
        cleaned = deepcopy(payload)
        dropped = [path for e in errors if (path := _drop_key_at(cleaned, tuple(e["loc"])))]
        if not dropped:
            raise
        # 只记键名 —— 值里出现过客户的真实姓名,不进日志。
        logger.warning(
            "ignoring %d unknown key(s) in stored manifest spec_json: %s",
            len(dropped),
            ", ".join(sorted(dropped)),
        )
        return AgentSpec.model_validate(cleaned)
