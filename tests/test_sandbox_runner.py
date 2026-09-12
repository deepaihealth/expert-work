"""Unit tests for the sandbox runner — Stream F.2 (test matrix #44).

``infra/sandbox-image/runner.py`` is image code, not an installed package,
so it is loaded by path. The tests exercise the stdin / stdout JSON
protocol (STREAM-F-DESIGN § 4.2): happy path, non-zero exit, timeout, and
the malformed-request paths the supervisor must never have to special-case.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from types import ModuleType


def _load_runner() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "infra" / "sandbox-image" / "runner.py"
    spec = importlib.util.spec_from_file_location("expert_work_sandbox_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ---------- run_once ----------


def test_run_once_captures_stdout() -> None:
    result = runner.run_once("print(2 + 2)", 30)
    assert result["stdout"].strip() == "4"
    assert result["exit_code"] == 0
    assert result["timed_out"] is False


def test_run_once_nonzero_exit_on_exception() -> None:
    result = runner.run_once("raise ValueError('boom')", 30)
    assert result["exit_code"] != 0
    assert "ValueError" in result["stderr"]
    assert "boom" in result["stderr"]
    assert result["timed_out"] is False


def test_run_once_propagates_sys_exit_code() -> None:
    result = runner.run_once("import sys\nsys.exit(7)", 30)
    assert result["exit_code"] == 7
    assert result["timed_out"] is False


def test_run_once_timeout_sets_timed_out() -> None:
    result = runner.run_once("import time\ntime.sleep(30)", 1)
    assert result["timed_out"] is True
    assert result["exit_code"] == -1


def test_run_once_clamps_timeout_below_one() -> None:
    # timeout_s=0 would make subprocess.run raise immediately; the runner
    # clamps it up to 1 so a fast snippet still completes.
    result = runner.run_once("print('ok')", 0)
    assert result["stdout"].strip() == "ok"
    assert result["timed_out"] is False


def test_run_once_caps_oversized_output() -> None:
    # A snippet printing far more than MAX_OUTPUT_CHARS still yields a
    # bounded response (transport safety net).
    code = f"print('x' * {runner.MAX_OUTPUT_CHARS * 2})"
    result = runner.run_once(code, 30)
    stdout = result["stdout"]
    assert isinstance(stdout, str)
    assert len(stdout) <= runner.MAX_OUTPUT_CHARS + 64
    assert "truncated" in stdout


def test_run_once_child_flags_enable_user_site_and_safe_path() -> None:
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


def test_handle_request_non_int_timeout_falls_back_to_default() -> None:
    # A bool is an int subclass but must not be accepted as a timeout;
    # falling back to the default still runs the code successfully.
    result = runner.handle_request({"code": "print('hi')", "timeout_s": True})
    assert result["stdout"].strip() == "hi"
    assert result["exit_code"] == 0


# ---------- handle_line ----------


def test_handle_line_runs_valid_request() -> None:
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


def test_main_processes_multiple_lines_and_skips_blanks() -> None:
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


def test_main_sets_owner_only_umask_before_serving_requests() -> None:
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


def test_child_processes_inherit_the_owner_only_umask(tmp_path: Path) -> None:
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
# B-50 PR3b —— per-exec ``cwd``。
#
# 行业形状:E2B / Daytona / OpenAI 托管沙箱都把 ``cwd`` 做成**每次执行**的参数
# (OpenAI:"Each command in setup_commands has its own optional cwd parameter")。
# 我们的本地后端此前只有建容器时的 ``--workdir``,是 per-container —— 而热沙箱
# 按 (tenant, user) 复用,一个容器里跑着这个用户所有 agent 的 exec,表达不了
# per-agent。所以 ``cwd`` 走 exec 通道,与 ``envs`` 同一条路。
#
# ``cwd`` **不是隔离手段**,只决定相对路径从哪解析(OpenAI 的原话:"it does not
# confine the run to cwd")。真边界是四个文件工具(spec §5.3)。
# ---------------------------------------------------------------------------


def test_run_once_honours_cwd(tmp_path: Path) -> None:
    target = tmp_path / "agents" / "plan-aaaaaaaa"
    target.mkdir(parents=True)
    out = runner.run_once("import os; print(os.getcwd())", 10, None, str(target))
    assert out["exit_code"] == 0
    assert out["stdout"].strip() == os.path.realpath(target)


def test_run_once_without_cwd_keeps_the_runners_own(tmp_path: Path) -> None:
    """不传 = 今天的行为一字不改(旧 orchestrator + 新镜像的偏斜方向)。"""
    out = runner.run_once("import os; print(os.getcwd())", 10, None, None)
    assert out["stdout"].strip() == os.getcwd()


def test_run_once_creates_a_missing_cwd(tmp_path: Path) -> None:
    """目录不存在就**建出来**,不是退回默认 cwd。

    agent 目录在有人往里写之前根本不存在,而 ``acquire`` 建不了它 —— 温沙箱是
    不带 agent 身份被认领的(池按 ``(tenant, user)`` 键),第一次 exec 才是最早
    知道目录名的时刻。静默退回用户根 = 正是 B-50 要治的那个病,还没有信号。
    """
    target = tmp_path / "agents" / "plan-aaaaaaaa"
    out = runner.run_once("import os; print(os.getcwd())", 10, None, str(target))

    assert out["exit_code"] == 0
    assert out["stdout"].strip() == os.path.realpath(target)
    assert target.is_dir()


def test_run_once_reports_a_cwd_it_cannot_create(tmp_path: Path) -> None:
    """建不出来(路上挡着一个文件)要报错 —— 只有「不存在」才自动建。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    out = runner.run_once("print(1)", 10, None, str(blocker / "under"))

    assert out["exit_code"] != 0
    assert "cwd" in out["stderr"]


def test_handle_request_passes_cwd_through(tmp_path: Path) -> None:
    target = tmp_path / "w"
    target.mkdir()
    out = runner.handle_request(
        {"code": "import os; print(os.getcwd())", "timeout_s": 10, "cwd": str(target)}
    )
    assert out["stdout"].strip() == os.path.realpath(target)


def test_handle_request_rejects_non_string_cwd(tmp_path: Path) -> None:
    """和 ``envs`` 同一个口径:类型不对就当没给,不要拿它去拼路径。"""
    out = runner.handle_request({"code": "import os; print(os.getcwd())", "cwd": 42})
    assert out["stdout"].strip() == os.getcwd()
