"""B-61 Task 3 —— run-start inputs 节点。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from expert_work.protocol import PromptVariableSpec
from orchestrator.graph_builder.inputs_node import _prefetch_code, make_inputs_node


class _FakeRuntime:
    """记下每次 exec 的 code。

    ``acquire``/``release`` 是最小可用桩 —— 真实 ``SandboxRuntime`` 的
    ``exec`` 需要先 ``acquire`` 拿 ``sandbox_id``(见
    ``SandboxWorkspaceWriter.write`` / ``run_in_sandbox`` 走的就是这条通道);
    这里只回一个假 id,不做租户/并发校验,断言只看 ``execs``。
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.execs: list[str] = []

    async def acquire(self, **kwargs: Any) -> UUID:
        return uuid4()

    async def exec(self, *, code: str, **kwargs: Any) -> Any:
        self.execs.append(code)
        # SandboxWorkspaceWriter.write() 解析 stdout 上的 JSON envelope
        # (parse_envelope/_raise_for_error);预拉那次调用不看 stdout,这个
        # 通用「成功」envelope 对两种调用都成立。
        return type(
            "R", (), {"stdout": '{"ok": true}', "stderr": "", "exit_code": 0, "timed_out": False}
        )()

    async def release(self, **kwargs: Any) -> None:
        return None


def _config(run_id: Any, tenant_id: Any, user_id: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {
        "configurable": {
            "tenant_id": str(tenant_id),
            "run_id": str(run_id),
            "user_id": str(user_id),
            "prompt_inputs": inputs,
        }
    }


@pytest.mark.asyncio
async def test_no_inputs_means_no_exec_at_all() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="a"),))
    await node({}, _config(uuid4(), uuid4(), uuid4(), {}))
    assert runtime.execs == []


@pytest.mark.asyncio
async def test_writes_inputs_json_then_runs_the_prefetch_script() -> None:
    runtime = _FakeRuntime()
    run_id = uuid4()
    node = make_inputs_node(
        client=runtime,
        variables=(PromptVariableSpec(name="org_logo"),),
    )
    await node({}, _config(run_id, uuid4(), uuid4(), {"org_logo": "https://x/a.jpg"}))
    assert len(runtime.execs) == 2, "一次写文件、一次跑预拉"
    write_code, prefetch_code = runtime.execs
    assert f"inputs/{run_id}/inputs.json" in write_code
    assert "def content_type_ok" in prefetch_code


@pytest.mark.asyncio
async def test_no_url_means_no_prefetch_exec() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="code"),))
    await node({}, _config(uuid4(), uuid4(), uuid4(), {"code": "PRJ001"}))
    assert len(runtime.execs) == 1, "没有 URL 就不必起预拉"


@pytest.mark.asyncio
async def test_a_failing_sandbox_never_fails_the_run() -> None:
    class _Boom(_FakeRuntime):
        async def exec(self, *, code: str, **kwargs: Any) -> Any:
            raise RuntimeError("sandbox create failed: 504")

    node = make_inputs_node(client=_Boom(), variables=(PromptVariableSpec(name="a"),))
    assert await node({}, _config(uuid4(), uuid4(), uuid4(), {"a": "x"})) == {}


@pytest.mark.asyncio
async def test_a_failing_prefetch_never_fails_the_run_even_when_write_succeeded() -> None:
    """``_a_failing_sandbox_never_fails_the_run`` 用的是不含 URL 的变量,写文件
    那一跳的失败早早短路,从没真的走到预拉那个 ``except``——这里专门只让预拉
    那次 ``exec`` 炸,写文件那次照常成功,钉住预拉分支自己的降级。"""

    class _BoomOnSecondExec(_FakeRuntime):
        async def exec(self, *, code: str, **kwargs: Any) -> Any:
            if len(self.execs) == 1:
                raise RuntimeError("sandbox exec failed: 504")
            return await super().exec(code=code, **kwargs)

    runtime = _BoomOnSecondExec()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="org_logo"),))
    result = await node({}, _config(uuid4(), uuid4(), uuid4(), {"org_logo": "https://x/a.jpg"}))
    assert result == {}
    assert len(runtime.execs) == 1, "写文件成功;预拉那次炸了但不重试、不再产生 exec"


@pytest.mark.asyncio
async def test_child_runs_skip() -> None:
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="a"),))
    config = _config(uuid4(), uuid4(), uuid4(), {"a": "x"})
    config["configurable"]["child_run"] = True
    await node({}, config)
    assert runtime.execs == []


@pytest.mark.asyncio
async def test_a_failing_acquire_never_fails_the_run() -> None:
    """写文件那一跳的 ``acquire`` 本身失败(配额满 / supervisor 不可达)也要
    降级,不是只有 ``exec`` 失败才降级。"""

    class _NoAcquire(_FakeRuntime):
        async def acquire(self, **kwargs: Any) -> UUID:
            raise RuntimeError("supervisor unreachable")

    node = make_inputs_node(client=_NoAcquire(), variables=(PromptVariableSpec(name="a"),))
    assert await node({}, _config(uuid4(), uuid4(), uuid4(), {"a": "x"})) == {}


@pytest.mark.asyncio
async def test_local_path_write_target_is_never_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-61 §4.1:``local_path``/inputs.json 自身的落盘路径永远相对 ``/workspace``。

    task-1 评审已指出纯函数层验不出这条(``with_local_path`` 只是照抄调用方给的
    字符串);enforcement 落在这一层——万一 ``inputs_rel_path`` 哪天被改坏返回了
    绝对路径,节点必须整体放弃,不能把绝对路径喂给 ``SandboxWorkspaceWriter``。
    """
    import orchestrator.graph_builder.inputs_node as inputs_node_module

    run_id = uuid4()
    monkeypatch.setattr(
        inputs_node_module,
        "inputs_rel_path",
        lambda _run_id: f"/workspace/inputs/{run_id}/inputs.json",
    )
    runtime = _FakeRuntime()
    node = make_inputs_node(client=runtime, variables=(PromptVariableSpec(name="org_logo"),))
    result = await node({}, _config(run_id, uuid4(), uuid4(), {"org_logo": "https://x/a.jpg"}))
    assert result == {}
    assert runtime.execs == [], "绝对路径必须被挡在写沙箱之前"


def test_prefetch_code_actually_runs_under_a_real_interpreter() -> None:
    """``_FakeRuntime`` 只记 code 串,从不真的执行它——``_prefetch_code`` 拼出来
    的代码是否真能跑,假 runtime 结构上验不出来(参见
    [[verify-where-it-can-fail]])。这里用真的子进程、真的 ``python -``
    (与生产的送法一致:整段代码当 stdin 喂给解释器)跑一遍。

    钉的两个真实坑(都是实测发现,不是凭空假设):

    1. 直接对整段脚本源码调用 ``str.format()`` 会被脚本自己的字典/f-string
       花括号绊倒,抛 ``KeyError``。
    2. 脚本自带 ``if __name__ == "__main__": raise SystemExit(main(sys.argv))``
       尾巴;``python -`` 执行时 ``__name__`` 仍是 ``"__main__"``,这条尾巴会先
       于我们自己想要的调用触发,且用宿主进程自己的 ``sys.argv``——``main`` 里
       ``argv[1]`` 直接越界崩溃。
    """
    with tempfile.TemporaryDirectory() as tmp:
        inputs_path = Path(tmp) / "inputs" / "run123" / "inputs.json"
        inputs_path.parent.mkdir(parents=True)
        inputs_path.write_text(
            json.dumps(
                {"run_id": "run123", "variables": {"code": {"value": "PRJ001", "trusted": True}}}
            ),
            encoding="utf-8",
        )
        code = _prefetch_code(str(inputs_path))
        proc = subprocess.run(
            [sys.executable, "-"], input=code, capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, proc.stderr
        report = json.loads(proc.stdout)
        assert report == {"prefetch": []}, "没有 URL,预拉报告应为空列表"
        # 文件被脚本原样写回(没有 URL 站点,内容不变);走完全程没有半路崩溃。
        assert json.loads(inputs_path.read_text(encoding="utf-8"))["run_id"] == "run123"


def test_every_run_entry_reaches_the_configurable_key() -> None:
    """四个 run 入口都调 run_agent,而 PROMPT_INPUTS_KEY 只在 run_agent 里加一次。

    这条挡的是「规矩写一处漏三处」:哪天有人把 configurable 的组装挪回入口层,
    这条测试会红。
    """
    import inspect

    from orchestrator import sse

    source = inspect.getsource(sse.run_agent)
    assert source.count("PROMPT_INPUTS_KEY") == 1
