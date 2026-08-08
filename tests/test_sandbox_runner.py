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


# ---------- umask (originally for a cross-uid write conflict, Task 4 review) ----------


def test_main_sets_permissive_umask_before_serving_requests() -> None:
    """``main()`` must set the process umask to 0 before it ever serves a
    request — every later ``run_once`` child inherits whatever umask is in
    effect at fork/exec time (see ``main()``'s own docstring: the reason
    this was originally added no longer holds after the uid-unification
    direction change, but the mechanism itself is unchanged and still a
    safe superset — kept as-is pending a live-cluster pass, not removed).
    ``os.umask`` has no "peek" call; the only portable way to *read* the
    current value without a side effect is the round-trip idiom used here
    (set, read back what it returns, restore) — hence saving/restoring the
    real process umask around the assertion.
    """
    saved = os.umask(0)
    os.umask(saved)
    try:
        runner.main(stdin=io.StringIO(""), stdout=io.StringIO())
        assert os.umask(0) == 0
    finally:
        os.umask(saved)


def test_child_processes_inherit_the_permissive_umask(tmp_path: Path) -> None:
    """End-to-end proof the umask override actually reaches child processes,
    not just that ``os.umask(0)`` was called: after ``main()`` runs, code
    executed via ``run_once`` (a *real* ``subprocess.run`` child, exactly
    the mechanism the submitted code's own ``mkdir``/``open`` calls go
    through) must produce a nested directory and file with **unmasked**
    modes — ``0o777``/``0o666`` — not the ``0o755``/``0o644`` a default
    umask (commonly ``0o022``) would otherwise leave.

    Those masked modes are what originally let the cross-uid gap this
    mechanism was built for hide: with a *different* uid on each side
    (control-plane vs. this sandbox's agent, pre-direction-change),
    ``read``/``list`` still worked at ``0o755``, so nothing broke until
    control-plane tried to delete or overwrite a file the agent created.
    After the uid-unification direction change control-plane and this
    sandbox's agent share one uid, so that specific gap no longer exists —
    see ``main()``'s docstring for why the mechanism itself stays (a safe,
    no-longer-minimal superset) rather than being narrowed in this task.
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
        assert dir_mode == 0o777, f"directory mode {oct(dir_mode)} — umask was not inherited"
        assert leaf_mode == 0o666, f"file mode {oct(leaf_mode)} — umask was not inherited"
    finally:
        os.umask(saved)
