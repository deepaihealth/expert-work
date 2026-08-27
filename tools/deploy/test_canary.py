"""Unit tests for the release canary's pure helpers (X-14 P1).

The network flow itself is exercised for real by ``release.sh`` stage 6
against a live stack; here we pin the frame parsing / verdict logic, same
split as ``test_deploy.py`` vs ``test_deploy_integration.py``.
"""

from __future__ import annotations

from canary import (
    CANARY_ARTIFACT_NAME,
    CANARY_PROMPT,
    content_ok,
    download_params,
    expected_line,
    parse_end_frame,
    pick_artifact,
)

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
