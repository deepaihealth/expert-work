"""Unit tests for the release canary's pure helpers (X-14 P1).

The network flow itself is exercised for real by ``release.sh`` stage 6
against a live stack; here we pin the frame parsing / verdict logic, same
split as ``test_deploy.py`` vs ``test_deploy_integration.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import canary
import httpx
import pytest
from canary import (
    CANARY_ARTIFACT_NAME,
    CANARY_PROMPT,
    FAIL_NO_SANDBOX,
    FAIL_NOT_FINISHED,
    FAIL_WRONG_RESULT,
    SANDBOX_ACQUIRE_MARKERS,
    content_ok,
    download_params,
    expected_line,
    failure_kind,
    parse_end_frame,
    pick_artifact,
    sandbox_acquire_failures,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SANDBOX_TOOLS = _REPO_ROOT / "services" / "orchestrator" / "src" / "orchestrator" / "tools"

# --------------------------------------------------------------------------- expected_line


def test_expected_line_is_deterministic() -> None:
    # 6683 * 9109 = 60875447 — the digits the sandbox must produce.
    assert expected_line() == "CANARY_OK 60875447"


def test_prompt_names_the_artifact_and_the_code() -> None:
    """The prompt must dictate the exact artifact name and the exact code —
    the assertions downstream depend on both."""
    assert CANARY_ARTIFACT_NAME in CANARY_PROMPT
    assert 'print("CANARY_OK " + str(6683 * 9109))' in CANARY_PROMPT


# --------------------------------------------------------------------------- parse_end_frame


def _frame(event: str, data: str, *, event_id: str | None = None) -> list[str]:
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.extend([f"event: {event}", f"data: {data}", ""])
    return lines


def test_parse_end_frame_returns_payload() -> None:
    lines = _frame("message_delta", '{"text": "hi"}') + _frame(
        "end",
        '{"status":"success","run_id":"r1","artifacts":[{"name":"a.txt","version":1}]}',
        event_id="42",
    )
    payload = parse_end_frame(lines)
    assert payload is not None
    assert payload["status"] == "success"
    assert payload["artifacts"][0]["name"] == "a.txt"


def test_parse_end_frame_none_when_absent() -> None:
    assert parse_end_frame(_frame("message_delta", '{"text": "hi"}')) is None
    assert parse_end_frame([]) is None


def test_parse_end_frame_none_on_malformed_json() -> None:
    assert parse_end_frame(_frame("end", "{not json")) is None


def test_parse_end_frame_none_on_non_object_payload() -> None:
    assert parse_end_frame(_frame("end", '"success"')) is None


def test_parse_end_frame_takes_first_end_frame() -> None:
    lines = _frame("end", '{"status":"success"}') + _frame("end", '{"status":"error"}')
    payload = parse_end_frame(lines)
    assert payload is not None
    assert payload["status"] == "success"


# --------------------------------------------------------------------------- pick_artifact


def test_pick_artifact_prefers_the_instructed_name() -> None:
    arts = [
        {"name": "other.bin", "version": 3},
        {"name": CANARY_ARTIFACT_NAME, "version": 1},
    ]
    picked = pick_artifact(arts)
    assert picked is not None
    assert picked["name"] == CANARY_ARTIFACT_NAME


def test_pick_artifact_falls_back_to_first_wellformed() -> None:
    arts = ["junk", {"no_name": True}, {"name": "report.md", "version": 2}]
    picked = pick_artifact(arts)
    assert picked is not None
    assert picked["name"] == "report.md"


def test_pick_artifact_none_when_empty_or_malformed() -> None:
    assert pick_artifact([]) is None
    assert pick_artifact(["junk", {"name": ""}]) is None


# --------------------------------------------------------------------------- content_ok


def test_content_ok_accepts_exact_line_with_noise() -> None:
    assert content_ok(b"CANARY_OK 60875447\n")
    assert content_ok(b"prefix\nCANARY_OK 60875447\nsuffix")


def test_content_ok_rejects_wrong_or_reformatted_digits() -> None:
    assert not content_ok(b"")
    assert not content_ok(b"CANARY_OK 60875448")
    assert not content_ok(b"CANARY_OK 60,875,447")
    assert not content_ok(b"canary_ok 60875447")


# --------------------------------------------------------------------------- download_params


def test_download_params_carries_version_gate() -> None:
    params = download_params({"name": "a.txt", "version": 4}, user_id="canary:release")
    assert params == {"user_id": "canary:release", "name": "a.txt", "version": 4}


def test_download_params_omits_non_int_version() -> None:
    params = download_params({"name": "a.txt", "version": "4"}, user_id="u")
    assert params == {"user_id": "u", "name": "a.txt"}


# --------------------------------------------------------------- B-87 ② no sandbox vs wrong result


def _tool_step(*messages: dict[str, Any]) -> list[str]:
    return _frame("updates", json.dumps({"tools": {"messages": list(messages)}}))


def _tool_msg(name: str, content: str, *, status: str = "error") -> dict[str, Any]:
    return {"type": "tool", "name": name, "content": content, "status": status}


def _datamarked(text: str) -> str:
    """The wire form of a tool result (docs-site sse-events.md「工具结果文本的还原」):
    fenced, and every whitespace run becomes U+2581 plus a space."""
    marked = text.replace(" ", "\u2581 ")
    return f"«UNTRUSTED nonce=8f3a2c1e»\n{marked}\n«/UNTRUSTED nonce=8f3a2c1e»"


_CREATE_FAILED = "[tool error] SandboxSupervisorError: sandbox create failed: 503 pool exhausted"


def test_acquire_failure_in_a_tool_result_is_found() -> None:
    lines = _frame("updates", '{"memory_recall": null}') + _tool_step(
        _tool_msg("exec_python", _CREATE_FAILED)
    )
    assert sandbox_acquire_failures(lines) == [f"exec_python: {_CREATE_FAILED}"]


def test_acquire_failure_is_found_through_the_datamarking() -> None:
    failures = sandbox_acquire_failures(_tool_step(_tool_msg("bash", _datamarked(_CREATE_FAILED))))
    assert failures == [f"bash: {_CREATE_FAILED}"]


def test_claim_timeout_counts_as_no_sandbox() -> None:
    msg = "[tool error] SandboxClaimTimeoutError: warm sandbox still being created"
    assert sandbox_acquire_failures(_tool_step(_tool_msg("exec_python", msg))) != []


def test_exec_failure_inside_a_sandbox_is_not_no_sandbox() -> None:
    """The run HAD a sandbox; the code inside it broke. That is "wrong result"."""
    msg = (
        "[tool error] SandboxSupervisorError: sandbox exec failed after 3.1s "
        "(timeout=60s): ReadError: "
    )
    lines = _tool_step(
        _tool_msg("exec_python", msg),
        _tool_msg("exec_python", "stdout: CANARY_OK 1", status="success"),
    )
    assert sandbox_acquire_failures(lines) == []


def test_successful_or_non_tool_messages_are_ignored() -> None:
    lines = _tool_step(
        _tool_msg("exec_python", _CREATE_FAILED, status="success"),
        {"type": "ai", "content": _CREATE_FAILED},
    ) + _frame("updates", "{not json")
    assert sandbox_acquire_failures(lines) == []


def test_failure_kind_prefers_no_sandbox() -> None:
    assert failure_kind(finished=True, sandbox_failures=["x"]) == FAIL_NO_SANDBOX
    assert failure_kind(finished=False, sandbox_failures=["x"]) == FAIL_NO_SANDBOX
    assert failure_kind(finished=False, sandbox_failures=[]) == FAIL_NOT_FINISHED
    assert failure_kind(finished=True, sandbox_failures=[]) == FAIL_WRONG_RESULT


def test_every_marker_is_still_raised_by_the_sandbox_code() -> None:
    """A reworded error message would silently turn "no sandbox" back into a
    plain "wrong result" — the canary runs in the pod without importing the
    orchestrator, so the strings are copies and this pins them to the source."""
    source = "".join(
        (_SANDBOX_TOOLS / name).read_text(encoding="utf-8")
        for name in ("agent_sandbox.py", "sandbox.py")
    )
    assert [m for m in SANDBOX_ACQUIRE_MARKERS if m not in source] == []


# ------------------------------------------------------------------------ verdict line, end to end


def _sse(*frames: list[str]) -> bytes:
    return "\n".join(line for frame in frames for line in frame).encode()


def _run_canary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stream: bytes
) -> tuple[int, str]:
    """``canary.main()`` against a canned SSE stream (artifact download → 404)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, content=stream)
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def client_with_mock_transport(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(canary.httpx, "AsyncClient", client_with_mock_transport)
    monkeypatch.setenv("EXPERT_WORK_CANARY_API_KEY", "test-key")
    rc = canary.main()
    return rc, capsys.readouterr().out


def test_canary_reports_no_sandbox(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _sse(
        _tool_step(_tool_msg("exec_python", _CREATE_FAILED)),
        _frame("end", '{"status":"success","run_id":"r1","artifacts":[]}'),
    )
    rc, out = _run_canary(monkeypatch, capsys, stream)
    assert rc == 1
    assert "RESULT: FAIL (no sandbox)" in out
    assert f"exec_python: {_CREATE_FAILED}" in out


def test_canary_reports_wrong_result_when_the_sandbox_was_fine(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stream = _sse(
        _tool_step(_tool_msg("exec_python", "stdout: CANARY_OK 1", status="success")),
        _frame("end", '{"status":"success","run_id":"r1","artifacts":[]}'),
    )
    rc, out = _run_canary(monkeypatch, capsys, stream)
    assert rc == 1
    assert "RESULT: FAIL (wrong result)" in out
    assert "could not get a sandbox" not in out


def test_canary_reports_a_run_that_never_ended(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _run_canary(monkeypatch, capsys, _sse(_frame("updates", '{"memory_recall": null}')))
    assert rc == 1
    assert "RESULT: FAIL (run did not finish)" in out
