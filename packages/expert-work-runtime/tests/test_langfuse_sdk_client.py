"""Unit tests for the Langfuse SDK adapter — Stream HX-7 (§ 8.4-PR1).

A fake SDK object is injected (no real Langfuse instance; CI has no
credentials) — these prove the protocol→SDK mapping and the factory's
degrade-to-recording behaviour.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from expert_work.runtime.middleware import (
    LangfuseClient,
    LangfuseSdkClient,
    RecordingLangfuseClient,
    make_langfuse_client,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.ended = False

    def update(self, **kwargs: Any) -> _FakeGeneration:
        self.updates.append(kwargs)
        return self

    def end(self) -> _FakeGeneration:
        self.ended = True
        return self


class _FakeSdk:
    def __init__(self) -> None:
        self.generations: list[dict[str, Any]] = []
        self.created: list[_FakeGeneration] = []
        self.flushed = 0
        self.shutdowns = 0

    def start_observation(self, **kwargs: Any) -> _FakeGeneration:
        self.generations.append(kwargs)
        generation = _FakeGeneration()
        self.created.append(generation)
        return generation

    def flush(self) -> None:
        self.flushed += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


# ---------------------------------------------------------------------------
# Contract with the INSTALLED langfuse SDK
# ---------------------------------------------------------------------------
#
# The fakes above are what let a major SDK upgrade slip through: #1233
# (2026-08-20) bumped langfuse 3.15 → 4.14, which removed
# ``Langfuse.start_generation``. Every adapter test kept passing against the
# fake, while production raised AttributeError on every LLM call — swallowed
# by the middleware's fail-soft guard as ``langfuse.start_span_failed`` —
# and Langfuse received nothing for six weeks. These two tests run the
# adapter against the real class, so the next API change fails CI instead.


def test_adapter_only_calls_methods_the_installed_sdk_has() -> None:
    from unittest.mock import create_autospec

    from langfuse import Langfuse
    from langfuse._client.span import LangfuseGeneration

    sdk = create_autospec(Langfuse, instance=True)
    sdk.start_observation.return_value = create_autospec(LangfuseGeneration, instance=True)

    span = LangfuseSdkClient(sdk).start_span(
        name="agent", input=[{"role": "user", "content": "hi"}], metadata={"model": "glm-5.3"}
    )
    span.record_output("answer")
    span.record_usage({"input_tokens": 10, "output_tokens": 3})
    span.record_error(RuntimeError("provider down"))
    span.end()

    sdk.start_observation.assert_called_once()
    assert sdk.start_observation.call_args.kwargs["as_type"] == "generation"


def test_adapter_runs_against_a_real_langfuse_instance() -> None:
    """A real client with export switched off — no network, but every call goes
    through the SDK's own signatures and return types."""
    from langfuse import Langfuse

    sdk = Langfuse(
        public_key="pk-lf-contract-test",
        secret_key="sk-lf-contract-test",
        host="http://127.0.0.1:9",
        tracing_enabled=False,
    )
    span = LangfuseSdkClient(sdk).start_span(name="agent", input="x", metadata={"model": "m"})
    span.record_output("y")
    span.record_usage({"input_tokens": 1})
    span.record_error(RuntimeError("e"))
    span.end()


# ---------------------------------------------------------------------------
# Adapter mapping
# ---------------------------------------------------------------------------


def test_start_span_maps_to_generation_with_model() -> None:
    sdk = _FakeSdk()
    client = LangfuseSdkClient(sdk)

    span = client.start_span(
        name="my-agent",
        input=[{"role": "user", "content": "hi"}],
        metadata={"model": "qwen-max", "tenant_id": "t-1", "run_id": "r-1"},
    )

    assert isinstance(client, LangfuseClient)  # protocol conformance
    assert sdk.generations == [
        {
            "name": "my-agent",
            "as_type": "generation",
            "input": [{"role": "user", "content": "hi"}],
            "metadata": {"model": "qwen-max", "tenant_id": "t-1", "run_id": "r-1"},
            "model": "qwen-max",
        }
    ]
    span.end()
    assert sdk.created[0].ended is True


def test_start_span_without_model_passes_none() -> None:
    sdk = _FakeSdk()
    LangfuseSdkClient(sdk).start_span(name="n", input=None, metadata=None)
    assert sdk.generations[0]["model"] is None
    assert sdk.generations[0]["metadata"] == {}


def test_record_output_and_usage_map_to_update() -> None:
    sdk = _FakeSdk()
    span = LangfuseSdkClient(sdk).start_span(name="n", input="x", metadata={})

    span.record_output("the answer")
    span.record_usage({"input_tokens": 10, "output_tokens": 3})

    generation = sdk.created[0]
    assert generation.updates[0] == {"output": "the answer"}
    assert generation.updates[1] == {"usage_details": {"input_tokens": 10, "output_tokens": 3}}


def test_record_error_marks_level_error() -> None:
    sdk = _FakeSdk()
    span = LangfuseSdkClient(sdk).start_span(name="n", input="x", metadata={})

    span.record_error(RuntimeError("provider down"))

    assert sdk.created[0].updates == [
        {"level": "ERROR", "status_message": "RuntimeError: provider down"}
    ]


def test_flush_and_shutdown_delegate() -> None:
    sdk = _FakeSdk()
    client = LangfuseSdkClient(sdk)
    client.flush()
    client.shutdown()
    assert sdk.flushed == 1
    assert sdk.shutdowns == 1


# ---------------------------------------------------------------------------
# Factory resolution (Mini-ADR HX-G3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "public", "secret"),
    [
        (None, None, None),
        ("https://langfuse.local", None, "sk"),
        ("https://langfuse.local", "pk", None),
        (None, "pk", "sk"),
        ("", "pk", "sk"),
    ],
)
def test_factory_incomplete_settings_degrade_to_recording(
    host: str | None, public: str | None, secret: str | None
) -> None:
    client = make_langfuse_client(host=host, public_key=public, secret_key=secret)
    assert isinstance(client, RecordingLangfuseClient)


def test_factory_import_failure_degrades_to_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simulate a broken install: ``import langfuse`` must fail inside the
    # factory. A None entry in sys.modules raises ImportError on import.
    monkeypatch.setitem(sys.modules, "langfuse", None)
    client = make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")
    assert isinstance(client, RecordingLangfuseClient)


def test_factory_complete_settings_build_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: list[dict[str, Any]] = []

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    import types

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _FakeLangfuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)

    client = make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    assert isinstance(client, LangfuseSdkClient)
    assert len(constructed) == 1
    kwargs = constructed[0]
    # PII masking defaults on (Mini-ADR OBS-L1, decision 4 — fail-safe).
    mask = kwargs.pop("mask")
    assert callable(mask)
    assert kwargs == {
        "public_key": "pk",
        "secret_key": "sk",
        "host": "https://langfuse.local",
        "tracing_enabled": True,
    }


def _capture_langfuse_kwargs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Install a fake ``langfuse.Langfuse`` that records its constructor kwargs."""
    constructed: list[dict[str, Any]] = []

    class _FakeLangfuse:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)

    import types

    fake_module = types.ModuleType("langfuse")
    fake_module.Langfuse = _FakeLangfuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    return constructed


def test_factory_default_mask_redacts_pii_in_nested_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default mask (Mini-ADR OBS-L1) must scrub conversational PII out of
    the nested message shapes Langfuse ingests — before they hit ClickHouse."""
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    mask = constructed[0]["mask"]
    masked = mask(
        data={
            "messages": [{"role": "user", "content": "email me at alice@example.com"}],
            "model": "qwen-max",
        }
    )
    assert "alice@example.com" not in masked["messages"][0]["content"]
    assert masked["model"] == "qwen-max"  # clean leaf untouched


def test_factory_default_mask_redacts_secrets_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(host="https://langfuse.local", public_key="pk", secret_key="sk")

    mask = constructed[0]["mask"]
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in mask(data="key sk-ABCDEFGHIJKLMNOPQRSTUVWX")


def test_factory_masking_disabled_passes_no_mask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch: explicit opt-out drops the mask kwarg entirely."""
    constructed = _capture_langfuse_kwargs(monkeypatch)
    make_langfuse_client(
        host="https://langfuse.local",
        public_key="pk",
        secret_key="sk",
        pii_masking_enabled=False,
    )
    assert "mask" not in constructed[0]
