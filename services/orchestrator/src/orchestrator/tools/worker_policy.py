"""B-122 —— 动态子智能体(worker)的工具边界:唯一的规则来源。

worker 此前继承父的全部工具,只剥 ``manage_task``;不该做的事全靠各 Agent 的
提示词自己写,而写提示词的人不一定知道 ``save_artifact`` / MCP 写工具是什么
(实证:ai-health-plan 的 worker 自己把中间稿登记成了交付物)。这里把边界收成
平台默认:worker 不往外交付、不永久改变 Agent 自己、不对外写。
见 ``docs/superpowers/specs/2026-09-27-worker-tool-policy-design.md``。

名单只在这里写一次;assembly / 控制面 / 提示词都从这里取。
"""

from __future__ import annotations

import os

from orchestrator.tools.skill_authoring import SKILL_AUTHORING_BUILTINS

#: 运维回滚阀(不是面向配置者的开关)。关掉后 worker 与父侧文案逐字节回到 B-122 之前。
WORKER_POLICY_ENV = "EXPERT_WORK_WORKER_TOOL_POLICY"
_OFF_VALUES = frozenset({"0", "false", "off", "no"})

#: A 类(往外交付:登记交付物、向人发起审批)+ B 类(永久改变 Agent 自己:
#: 技能 / 行为补丁 / 记忆 / 定时任务)。技能那一族直接复用它的来源集合。
WORKER_DENIED_BUILTINS: frozenset[str] = SKILL_AUTHORING_BUILTINS | frozenset(
    {"save_artifact", "ask_for_approval", "manage_task"}
)

#: C 类 —— worker 的 http 只许读。POST / PUT 这些就是对外写,与 MCP 写工具同性质。
READ_ONLY_HTTP_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})


def worker_policy_enabled() -> bool:
    """回滚阀:默认开;``0`` / ``false`` / ``off`` / ``no``(不分大小写)为关。"""
    return os.environ.get(WORKER_POLICY_ENV, "").strip().lower() not in _OFF_VALUES
