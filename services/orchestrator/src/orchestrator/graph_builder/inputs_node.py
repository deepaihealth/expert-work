"""B-61 §4.2 —— run-start 节点:把本轮声明变量落成 ``inputs.json``,再在沙箱里预拉。

位置与 ``workspace_ingest`` 同层,理由见 spec §4.2:run 启动的 config 组装有四处
(``api/runs.py`` / ``run_queue_worker.py`` / ``trigger_firing.py`` / ``orphan_sweep.py``),
在那一层做必漏;图是四条路都必经的单一入口。

**永不让 run 失败**:写文件或预拉的任何异常都只 warning,节点返回 ``{}``。模型仍可
按今天的老办法自己下载 —— 退化到 B-61 之前,不是退化到坏掉。

**没有声明变量的 agent 不装这个节点**——``agent_factory`` 的门(零副作用、零额外
acquire);本模块只管「已经决定要装」之后的事。节点体自己再收窄两层:本轮没真的
传值(可选变量没给)、或文档里没有任何 URL,都提前返回,一次 exec 都不起。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol import PromptVariableSpec
from orchestrator.graph_builder._config import cancellation_token, configurable_uuid
from orchestrator.graph_builder.memory import MemoryNode
from orchestrator.sse import PROMPT_INPUTS_KEY
from orchestrator.state import AgentState
from orchestrator.tools.file_ops import SandboxWorkspaceWriter
from orchestrator.tools.inputs_doc import (
    build_inputs_doc,
    inputs_abs_path,
    inputs_rel_path,
    iter_url_sites,
)
from orchestrator.tools.prefetch_script import script_source
from orchestrator.tools.registry import ToolContext
from orchestrator.tools.sandbox import SandboxOutcome, SandboxRuntime, run_in_sandbox

logger = logging.getLogger(__name__)

#: 预拉脚本的源码,模块导入时求值一次(它读的是仓库里的文件,不随 run 变)。
#:
#: **不能直接把整段源码送进沙箱再原样跑**:脚本自带
#: ``if __name__ == "__main__": raise SystemExit(main(sys.argv))`` 尾巴——用
#: ``python -``(stdin 送代码串)执行时 ``__name__`` 仍是 ``"__main__"``,这条尾巴
#: 会先于我们自己想要的调用触发,而且用的是宿主进程自己的 ``sys.argv``(该模式下
#: 就是 ``['-']``),``main`` 里 ``argv[1]`` 直接越界崩溃——实测复现(见
#: task-3-report)。裁掉这条尾巴,换成我们自己拼的显式调用,原脚本的其余定义
#: (``content_type_ok`` 等)不受影响。
_PREFETCH_SCRIPT_BODY = script_source().split('if __name__ == "__main__":', 1)[0]

#: 预拉这一次 exec 的墙钟上限(秒)。
#:
#: **必须显式给**:不给的话两个后端各自套自己的 30 秒默认,而脚本对**单个** URL
#: 就允许 ``prefetch_script.TIMEOUT_S`` = 30 秒、总字节预算 128 MiB —— 两个慢文件
#: 就到顶,exec 被杀。240 秒的取法:
#:
#: * 下限 —— 覆盖 8 个 URL 各自跑满 30 秒的最坏情况(声明变量里的 URL 是个位数,
#:   8 个是留了余量的估法);
#: * 上限 —— 低于 supervisor 侧 ``_MAX_EXEC_TIMEOUT_S`` = 300 的硬顶,所以这个值
#:   真的会被沙箱侧执行(``sandbox_supervisor/schemas.py`` 的
#:   ``timeout_s: Field(gt=0, le=300)`` —— 高于硬顶会被 supervisor 以 422 拒掉,
#:   不是被截断),orchestrator 的 HTTP 读超时(exec 截止 +
#:   ``_EXEC_HTTP_BUFFER_S``)也仍在硬顶之内;
#: * 不取满 300 —— 本节点跑在 run 启动路径上,用户在等,不该把整个上限都押进去。
#:
#: 被杀也不再是全丢:脚本每拉完一个 site 就原子改写一次 ``inputs.json``
#: (``prefetch_script._rewrite``),超时只损失还没拉到的那些。
_PREFETCH_TIMEOUT_S = 240


def _prefetch_code(path: str) -> str:
    """把预拉脚本的源码与调用入口拼成一段送进沙箱执行的 code 串。

    ``path``(``inputs_abs_path`` 给的沙箱内绝对路径)只用来定位 ``inputs.json``
    这份文件本身——它不是哪个变量的 ``local_path``,两者语义不同,不要混用。

    这里不用 ``str.format``:脚本源码里本来就有字典/f-string 字面量的花括号
    (如 ``_TYPE_EXACT = frozenset({...})``),对整段源码调用 ``.format()`` 会被
    这些花括号绊倒并抛 ``KeyError``——同样是实测复现的坑。只对我们自己拼的这一
    行短字符串做插值,不碰脚本正文。
    """
    return f"{_PREFETCH_SCRIPT_BODY}\nraise SystemExit(main(['prefetch', {path!r}]))\n"


def _parse_prefetch_report(stdout: str) -> list[dict[str, Any]]:
    """从脚本 stdout 里取出 ``{"prefetch": [...]}`` 报告;取不出就当没有报告。

    这里**必须**宽容:脚本可能被超时 SIGKILL(stdout 半截)、可能一个字都没来得及
    打、也可能是后端在前面多插了别的行。解析失败绝不能变成异常 —— 它在
    「永不让 run 失败」的那条链上,而且一条日志比整轮预拉重要得多的反面正是
    我们要避免的。
    """
    try:
        parsed = json.loads(stdout.strip() or "{}")
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    report = parsed.get("prefetch")
    if not isinstance(report, list):
        return []
    return [item for item in report if isinstance(item, dict)]


def _log_prefetch_outcome(outcome: SandboxOutcome, *, variable_names: list[str]) -> None:
    """把预拉结果落成一行日志。名字 / 命中 / 字节数,**不记值也不记 URL**。

    原事故(spec §一)最缺的就是这一行:事后没有任何办法区分「平台拉到了、模型
    没用」和「平台没拉到、模型只能手抄」。stderr 一律不记 —— 里面可能带着 URL。
    """
    report = _parse_prefetch_report(outcome.stdout)
    hit_names = sorted({str(item.get("variable")) for item in report if item.get("hit")})
    total_bytes = sum(int(item["bytes"]) for item in report if isinstance(item.get("bytes"), int))
    if outcome.exit_code != 0 or outcome.timed_out:
        # 脚本自己任何失败都以 0 退出,所以非 0 / 超时来自 exec 这一层(被杀、
        # 解释器起不来)——预拉这轮不完整,但 run 照跑。
        logger.warning(
            "inputs.prefetch_exec_failed",
            extra={
                "variable_names": variable_names,
                "prefetch_exit_code": outcome.exit_code,
                "prefetch_timed_out": outcome.timed_out,
                "prefetch_hit_names": hit_names,
            },
        )
        return
    logger.info(
        "inputs.prefetch_done",
        extra={
            "variable_names": variable_names,
            "prefetch_site_count": len(report),
            "prefetch_hit_count": len(hit_names),
            "prefetch_hit_names": hit_names,
            "prefetch_total_bytes": total_bytes,
        },
    )


def make_inputs_node(
    *, client: SandboxRuntime, variables: tuple[PromptVariableSpec, ...]
) -> MemoryNode:
    """构造 run-start 的 inputs 节点。

    ``client`` 是已经在 ``agent_factory`` 里绑好 egress / agent_key 的
    ``SandboxRuntime``(与 ``workspace_ingest`` 用的是同一个对象)——本节点自己
    不处理 agent_key,``_AgentKeyBindingClient`` 会在每次 ``exec`` 时透明地
    重新注入,与 ``exec_python`` / ``bash`` 走的是同一条通道。
    """

    async def inputs_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        token = cancellation_token(config)
        token.raise_if_cancelled()
        configurable = config.get("configurable") or {}
        # 委派出的子 run 不该有自己独立的 inputs.json——与 workspace_ingest 同一
        # 道闸(BUG-10 终审 F3 的同类考虑)。
        if configurable.get("child_run"):
            return {}
        run_id = configurable_uuid(config, "run_id")
        tenant_id = configurable_uuid(config, "tenant_id")
        if run_id is None or tenant_id is None:
            return {}
        raw_inputs = configurable.get(PROMPT_INPUTS_KEY) or {}
        try:
            doc = build_inputs_doc(run_id=run_id, variables=variables, inputs=raw_inputs)
            # 声明了变量但本轮一个都没传值(全是可选变量且未给)——没有内容可写,
            # 零副作用地跳过(与「没有声明变量的 agent 不装节点」同一原则,只是
            # 判定时机不同:那一层在 build 期,这一层在 run 期)。
            if doc is None or not doc["variables"]:
                return {}
            variable_names = sorted(doc["variables"])
        except (TypeError, ValueError):
            # build_inputs_doc 对每个声明变量的 value 提前 json.dumps 一次
            # (task-1「早失败」设计:不可序列化的值不该等到写文件那一步再炸),
            # 这条异常因此是真会发生的分支,不是理论上的。永不让 run 失败:与
            # 写文件/预拉失败同一口径降级。不记值——用声明的变量名(不是 doc
            # 里的,构造半途失败时 doc 拿不到;不知道具体是哪个变量的值不可
            # 序列化,所以报全部声明名而不是猜)。
            logger.warning(
                "inputs.build_failed",
                extra={"variable_names": sorted(v.name for v in variables)},
                exc_info=True,
            )
            return {}
        rel = inputs_rel_path(run_id)
        # B-61 §4.1 —— local_path 的落地约定是相对 /workspace;task-1 评审指出
        # 纯函数层验不出这条,enforcement 落在这里:inputs.json
        # 自己的写入目标必须是相对路径,绝不把绝对路径喂给 SandboxWorkspaceWriter
        # (它的 ``rel`` 参数按约定就是 workspace-relative)。用显式 if 而不是
        # ``assert``——``assert`` 在 ``-O`` 下会被整段裁掉,那样这条闸就形同
        # 虚设;且这里要的是「优雅降级」不是「抛出去」,与「节点永不让 run 失败」
        # 这条硬约束是同一件事,不能靠一个未被捕获的异常来保证。
        if rel.startswith("/"):
            logger.warning("inputs.rel_path_not_relative", extra={"variable_names": variable_names})
            return {}
        ctx = ToolContext(
            tenant_id=tenant_id,
            run_id=run_id,
            user_id=configurable_uuid(config, "user_id"),
            cancellation_token=token,
        )
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "inputs_materialize"):
            writer = SandboxWorkspaceWriter(client=client, ctx=ctx)
            try:
                await writer.write(rel=rel, content=json.dumps(doc, ensure_ascii=False))
            except Exception:
                # 永不让 run 失败:模型仍可按今天的老办法自己下载(退化到
                # B-61 之前,不是退化到坏掉)。不记 doc/值——只记变量名(spec
                # §六之 4 的口径,与 api/runs.py 的 prompt_var_names 同义)。
                logger.warning(
                    "inputs.write_failed",
                    extra={"variable_names": variable_names},
                    exc_info=True,
                )
                return {}
            if not iter_url_sites(doc):
                return {}
            try:
                outcome = await run_in_sandbox(
                    client,
                    code=_prefetch_code(inputs_abs_path(run_id)),
                    timeout_s=_PREFETCH_TIMEOUT_S,
                    ctx=ctx,
                    tool_label="inputs_prefetch",
                    fallback_thread_id="inputs_prefetch",
                )
                _log_prefetch_outcome(outcome, variable_names=variable_names)
            except Exception:
                # 同上:预拉失败只降级,不让 run 失败。
                logger.warning(
                    "inputs.prefetch_failed",
                    extra={"variable_names": variable_names},
                    exc_info=True,
                )
        return {}

    return inputs_node
