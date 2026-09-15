"""Unit tests for the sandbox runner — Stream F.2 (test matrix #44).

``infra/sandbox-image/runner.py`` is image code, not an installed package,
so it is loaded by path. The tests exercise the stdin / stdout JSON
protocol (STREAM-F-DESIGN § 4.2): happy path, non-zero exit, timeout, and
the malformed-request paths the supervisor must never have to special-case.

B-60 —— 每次 exec 的子进程真实 argv 是 ``unshare -Urm … sh -c <exec-view 脚本>``,
它要的两样东西在这里**一样都没有**:CI host / 开发机 macOS 给不了非特权 user
namespace,``/mnt/workspace`` 也不存在。所以进程语义那一类用例(捕获 stdout、
异常退出码、超时、输出截断、子进程旗标、umask 继承……)都挂 ``direct_exec``
fixture:它把 ``runner._exec_argv`` 换成**真函数产出的 argv 去掉命名空间前缀**
的那一段,``subprocess.run`` 这条管线一字不改地照跑。argv 本身的形状由下面
``_exec_argv`` 那一节直接钉真函数,命名空间里的挂载行为由 docker 起真镜像的
``services/orchestrator/tests/test_sandbox_runtime_contract.py`` 覆盖。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "infra" / "sandbox-image" / "runner.py"
    spec = importlib.util.spec_from_file_location("expert_work_sandbox_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()

#: 真的那个 argv 拼装函数。模块加载时就抓在手里,免得 fixture / 录制器互相套上去。
_REAL_EXEC_ARGV = runner._exec_argv

#: ``_EXEC_VIEW_SCRIPT`` 的 ``$0``;它后面一个是 ``$1``(bind 源),再后面是解释器。
_ARGV0 = "ew-exec-view"


def _root_slot(argv: list[str]) -> str:
    """argv 里被 exec-view 脚本当 ``$1``(bind 源)读走的那一格。"""
    return argv[argv.index(_ARGV0) + 1]


@pytest.fixture
def direct_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """把子进程 argv 剥成裸 ``python -c``,好让进程语义用例在本机跑得起来。

    刻意**不**写成 ``[sys.executable, "-E", "-P", "-c", code]`` 这样的手抄常量:
    那样 ``test_run_once_child_flags_enable_user_site_and_safe_path`` 断言的旗标
    就成了 fixture 自己塞进去的,runner.py 把 ``-P`` 删掉它照样绿 —— 修复自带的
    测试给坏版本发合格证。这里取真 ``_exec_argv`` 的产出,只砍掉
    ``unshare … sh -c <script> ew-exec-view <root>`` 这段包装,解释器和旗标仍旧
    是 runner.py 自己拼的那几个。
    """

    def _without_the_namespace_wrapper(code: str, agent_root: str | None) -> list[str]:
        argv = _REAL_EXEC_ARGV(code, agent_root)
        return argv[argv.index(_ARGV0) + 2 :]

    monkeypatch.setattr(runner, "_exec_argv", _without_the_namespace_wrapper)


# ---------- run_once ----------


def test_run_once_captures_stdout(direct_exec: None) -> None:
    result = runner.run_once("print(2 + 2)", 30)
    assert result["stdout"].strip() == "4"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_once_nonzero_exit_on_exception(direct_exec: None) -> None:
    result = runner.run_once("raise ValueError('boom')", 30)
    assert result["exit_code"] != 0
    assert "ValueError" in result["stderr"]
    assert "boom" in result["stderr"]
    assert result["timed_out"] is False


def test_run_once_propagates_sys_exit_code(direct_exec: None) -> None:
    result = runner.run_once("import sys\nsys.exit(7)", 30)
    assert result["exit_code"] == 7
    assert result["timed_out"] is False


def test_run_once_timeout_sets_timed_out(direct_exec: None) -> None:
    result = runner.run_once("import time\ntime.sleep(30)", 1)
    assert result["timed_out"] is True
    assert result["exit_code"] == -1


def test_run_once_clamps_timeout_below_one(direct_exec: None) -> None:
    # timeout_s=0 would make subprocess.run raise immediately; the runner
    # clamps it up to 1 so a fast snippet still completes.
    result = runner.run_once("print('ok')", 0)
    assert result["stdout"].strip() == "ok"
    assert result["timed_out"] is False


def test_run_once_caps_oversized_output(direct_exec: None) -> None:
    # A snippet printing far more than MAX_OUTPUT_CHARS still yields a
    # bounded response (transport safety net).
    code = f"print('x' * {runner.MAX_OUTPUT_CHARS * 2})"
    result = runner.run_once(code, 30)
    stdout = result["stdout"]
    assert isinstance(stdout, str)
    assert len(stdout) <= runner.MAX_OUTPUT_CHARS + 64
    assert "truncated" in stdout


def test_run_once_child_flags_enable_user_site_and_safe_path(direct_exec: None) -> None:
    # PR-C — the child must run `-E -P`, NOT `-I`: `-I` implies `-s`, which
    # kicks the user site out of sys.path and silently breaks the image's
    # PIP_USER=1 on-demand install flow (installs succeed, imports fail).
    result = runner.run_once(
        "import sys; print(sys.flags.no_user_site, sys.flags.safe_path, "
        "sys.flags.ignore_environment, sys.flags.isolated)",
        10,
    )
    assert result["exit_code"] == 0
    # no_user_site=0 (user site ON), safe_path=True (-P; this flag is a bool,
    # unlike the others), ignore_environment=1 (-E), isolated=0 (not -I).
    assert result["stdout"].strip() == "0 True 1 0"


# ---------- handle_request ----------


def test_handle_request_missing_code_is_error() -> None:
    result = runner.handle_request({"timeout_s": 5})
    assert result["exit_code"] == -1
    assert "code" in result["stderr"]
    assert result["timed_out"] is False


def test_handle_request_non_int_timeout_falls_back_to_default(direct_exec: None) -> None:
    # A bool is an int subclass but must not be accepted as a timeout;
    # falling back to the default still runs the code successfully.
    result = runner.handle_request({"code": "print('hi')", "timeout_s": True})
    assert result["stdout"].strip() == "hi"
    assert result["exit_code"] == 0


# ---------- handle_line ----------


def test_handle_line_runs_valid_request(direct_exec: None) -> None:
    result = runner.handle_line('{"code": "print(1 + 1)", "timeout_s": 10}')
    assert result["stdout"].strip() == "2"
    assert result["exit_code"] == 0


def test_handle_line_invalid_json_is_error() -> None:
    result = runner.handle_line("not json at all")
    assert result["exit_code"] == -1
    assert "invalid JSON" in result["stderr"]


def test_handle_line_non_object_is_error() -> None:
    result = runner.handle_line("42")
    assert result["exit_code"] == -1
    assert "JSON object" in result["stderr"]


# ---------- main loop ----------


def test_main_emits_readiness_line_first() -> None:
    stdout = io.StringIO()
    runner.main(stdin=io.StringIO(""), stdout=stdout)

    first = json.loads(stdout.getvalue().splitlines()[0])
    assert first == {"ready": True}


def test_main_processes_multiple_lines_and_skips_blanks(direct_exec: None) -> None:
    stdin = io.StringIO(
        '{"code": "print(10)"}\n'
        "\n"  # blank line — skipped, no response emitted
        '{"code": "print(20)"}\n'
    )
    stdout = io.StringIO()
    runner.main(stdin=stdin, stdout=stdout)

    lines = stdout.getvalue().splitlines()
    # Line 0 is the readiness line; the two requests follow.
    assert json.loads(lines[0]) == {"ready": True}
    responses = [json.loads(line) for line in lines[1:]]
    assert len(responses) == 2
    assert responses[0]["stdout"].strip() == "10"
    assert responses[1]["stdout"].strip() == "20"


def test_main_emits_error_response_for_bad_line() -> None:
    stdout = io.StringIO()
    runner.main(stdin=io.StringIO("{bad}\n"), stdout=stdout)

    lines = stdout.getvalue().splitlines()
    assert json.loads(lines[0]) == {"ready": True}
    response = json.loads(lines[1])
    assert response["exit_code"] == -1
    assert "invalid JSON" in response["stderr"]


# ---------- umask (owner-only, workspace-gid-sharing design § 六) ----------


def test_main_sets_owner_only_umask_before_serving_requests(direct_exec: None) -> None:
    """``main()`` must set the process umask to ``0o077`` before it ever
    serves a request — every later ``run_once`` child inherits whatever
    umask is in effect at fork/exec time. ``0o077`` clears every
    group/other bit, matching ``NasWorkspaceStore._DIR_MODE``/
    ``_LEAF_FILE_MODE`` (``0o700``/``0o600``) — the owner-only mode this
    workspace targets now that control-plane and this sandbox's agent run
    as the same uid (see ``main()``'s own docstring for the full history:
    this used to be ``os.umask(0)``, added for a cross-uid write conflict
    that no longer exists after the uid-unification direction change).
    ``os.umask`` has no "peek" call; the only portable way to *read* the
    current value without a side effect is the round-trip idiom used here
    (set, read back what it returns, restore) — hence saving/restoring the
    real process umask around the assertion.
    """
    saved = os.umask(0)
    os.umask(saved)
    try:
        runner.main(stdin=io.StringIO(""), stdout=io.StringIO())
        assert os.umask(0) == 0o077
    finally:
        os.umask(saved)


def test_child_processes_inherit_the_owner_only_umask(direct_exec: None, tmp_path: Path) -> None:
    """End-to-end proof the umask override actually reaches child processes,
    not just that ``os.umask(0o077)`` was called: after ``main()`` runs,
    code executed via ``run_once`` (a *real* ``subprocess.run`` child,
    exactly the mechanism the submitted code's own ``mkdir``/``open`` calls
    go through) must produce a nested directory and file with the
    owner-only modes ``0o700``/``0o600`` — not the ``0o755``/``0o644`` the
    sandbox's default umask (commonly ``0o022``) would otherwise leave,
    and not the fully-permissive ``0o777``/``0o666`` this mechanism used to
    force back when it was still ``os.umask(0)`` (see ``main()``'s
    docstring for that history — a cross-uid write conflict that no longer
    exists after the uid-unification direction change, workspace-gid-
    sharing design § 六).

    This tightening only holds end-to-end together with
    ``sandbox_supervisor.docker_client._AUX_CONTAINER_HARDENING_ARGS``
    carrying ``--cap-add DAC_OVERRIDE`` — without it, the supervisor's
    root-but-capability-stripped aux containers can no longer read/write/
    delete these now-owner-only files. That half is exercised in
    ``services/sandbox-supervisor``'s own tests, not here — this module has
    no visibility into the supervisor's docker invocations.
    """
    saved = os.umask(0)
    os.umask(saved)
    try:
        runner.main(stdin=io.StringIO(""), stdout=io.StringIO())
        nested = tmp_path / "reports" / "nested"
        leaf = nested / "out.txt"
        code = f"import os\nos.makedirs({str(nested)!r})\nopen({str(leaf)!r}, 'w').close()\n"

        result = runner.run_once(code, 30)

        assert result["exit_code"] == 0, result["stderr"]
        dir_mode = (tmp_path / "reports").stat().st_mode & 0o777
        leaf_mode = leaf.stat().st_mode & 0o777
        assert dir_mode == 0o700, f"directory mode {oct(dir_mode)} — umask was not inherited"
        assert leaf_mode == 0o600, f"file mode {oct(leaf_mode)} — umask was not inherited"
    finally:
        os.umask(saved)


# ---------------------------------------------------------------------------
# B-60 —— per-exec 的私有 ``/workspace``。
#
# B-50 PR3b 的 per-exec ``cwd`` 参数(``subprocess.run(cwd=...)``)在这一版被
# ``agent_root`` 顶掉了:``cwd`` 只决定相对路径从哪解析,绝对路径照样能走出去
# (OpenAI 的原话:"it does not confine the run to cwd");``agent_root`` 是拿去
# bind 的挂载源,子进程自己的 mount 命名空间里 ``/workspace`` **就是**那个目录,
# 别的 agent 的目录压根不在那儿。
#
# 命名空间里的事(``mkdir -p "$root"``、shared/ 只读挂入、tmpfs 盖掉
# ``/mnt/workspace``、挂不上就非零退出)这里一条都验不了——本机没有非特权 user
# namespace 也没有 ``/mnt/workspace``。那些由 docker 起真镜像的
# ``services/orchestrator/tests/test_sandbox_runtime_contract.py`` 覆盖
# (``test_exec_view_is_the_agents_own_directory`` 等)。这一节只钉 runner.py
# **这一侧**说得清的两件事:argv 的形状,和 ``handle_request`` 怎么把
# ``agent_root`` 递下去。
# ---------------------------------------------------------------------------


def test_exec_argv_binds_the_agent_root() -> None:
    """绑了 agent 就把它的目录送进 ``$1``,后面紧跟解释器与代码。"""
    argv = runner._exec_argv("print(1)", "/mnt/workspace/agents/a")

    assert argv == [
        "unshare",
        "-Urm",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        runner._EXEC_VIEW_SCRIPT,
        "ew-exec-view",
        "/mnt/workspace/agents/a",
        sys.executable,
        "-E",
        "-P",
        "-c",
        "print(1)",
    ]


@pytest.mark.parametrize("agent_root", [None, ""])
def test_exec_argv_unbound_leaves_the_root_slot_empty(agent_root: str | None) -> None:
    """未绑 agent(``None`` 或空串)= ``$1`` 是空串,脚本 bind 整个用户根。

    这一格**不能**省掉:脚本靠位置参数读 ``$1``,少一格会让解释器路径顶到
    ``$1`` 上去。
    """
    argv = runner._exec_argv("print(1)", agent_root)

    assert argv == [
        "unshare",
        "-Urm",
        "--propagation",
        "private",
        "--",
        "sh",
        "-c",
        runner._EXEC_VIEW_SCRIPT,
        "ew-exec-view",
        "",
        sys.executable,
        "-E",
        "-P",
        "-c",
        "print(1)",
    ]


def _recording_exec_argv(seen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """记下真 ``_exec_argv`` 拼出的 argv,再换成一个不进命名空间的空跑子进程。"""

    def _record(code: str, agent_root: str | None) -> list[str]:
        seen.append(_REAL_EXEC_ARGV(code, agent_root))
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(runner, "_exec_argv", _record)


def test_handle_request_passes_agent_root_through(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    _recording_exec_argv(seen, monkeypatch)

    runner.handle_request(
        {
            "code": "print(1)",
            "timeout_s": 10,
            "agent_root": "/mnt/workspace/agents/plan-aaaaaaaa",
        }
    )

    assert len(seen) == 1
    assert _root_slot(seen[0]) == "/mnt/workspace/agents/plan-aaaaaaaa"


@pytest.mark.parametrize("bad_root", [42, True, ["/mnt/workspace/agents/a"], "", None])
def test_handle_request_treats_an_unusable_agent_root_as_unbound(
    bad_root: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """和 ``envs`` 同一个口径:类型不对就当没给,不要拿它去拼挂载源。

    钉的是 ``handle_request`` 今天真实的实现(``isinstance(raw_root, str) and
    raw_root``,否则 ``None``):**不报错**,退回未绑——``$1`` 是空串,
    ``/workspace`` 是整个用户根。空串与 JSON ``null``(= 请求根本没带这个键)
    走的是同一条分支,一起钉住。
    """
    seen: list[list[str]] = []
    _recording_exec_argv(seen, monkeypatch)

    result = runner.handle_request({"code": "print(1)", "agent_root": bad_root})

    assert result["exit_code"] == 0, result["stderr"]
    assert len(seen) == 1
    assert _root_slot(seen[0]) == "", f"agent_root={bad_root!r} 没有退回未绑"


# ``test_run_once_creates_a_missing_cwd`` / ``test_run_once_reports_a_cwd_it_cannot_create``
# 在 B-60 里删掉了,没有等价替代:目录是不是被建出来、建不出来会不会 fail closed,
# 现在都是 ``_EXEC_VIEW_SCRIPT`` 里 ``set -eu`` + ``mkdir -p "$root"`` 的事,发生在
# 子进程的命名空间里,本机的 ``run_once`` 够不着(``unshare`` 起不来,更没有
# ``/mnt/workspace``)。这两条由 docker 起真镜像的
# ``services/orchestrator/tests/test_sandbox_runtime_contract.py`` 的
# ``test_exec_view_is_the_agents_own_directory`` 及其 fail-closed 设计覆盖。
