"""Unit tests for ``make_object_store`` (without hitting a real S3 endpoint)."""

from __future__ import annotations

import pytest
from botocore.config import Config as BotoConfig

from expert_work.runtime.storage import (
    InMemoryObjectStore,
    S3CompatibleConfig,
    make_object_store,
)


@pytest.mark.asyncio
async def test_memory_backend_yields_in_memory_store() -> None:
    async with make_object_store("memory") as store:
        assert isinstance(store, InMemoryObjectStore)


@pytest.mark.asyncio
async def test_s3_backend_requires_config() -> None:
    with pytest.raises(ValueError, match="requires an S3CompatibleConfig"):
        async with make_object_store("s3-compatible"):
            pass


@pytest.mark.asyncio
async def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="unknown object_store backend"):
        async with make_object_store("blob-store"):  # type: ignore[arg-type]
            pass


def test_config_is_frozen_dataclass() -> None:
    config = S3CompatibleConfig(
        endpoint_url="http://localhost:9000",
        region="us-east-1",
        bucket="b",
        access_key="ak",
        secret_key="sk",
    )
    # frozen=True → assignment raises FrozenInstanceError (a subclass of
    # AttributeError, which is what dataclasses raises).
    with pytest.raises(AttributeError):
        config.bucket = "other"  # type: ignore[misc]


def test_config_addressing_style_defaults_to_path() -> None:
    """Default stays ``"path"`` — MinIO local dev keeps working unchanged."""
    config = S3CompatibleConfig(
        endpoint_url="http://localhost:9000",
        region="us-east-1",
        bucket="b",
        access_key="ak",
        secret_key="sk",
    )
    assert config.addressing_style == "path"


class _FakeClientCM:
    """Stands in for ``session.create_client(...)``'s async context manager."""

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Captures the ``BotoConfig`` + endpoint passed to ``create_client``."""

    def __init__(self) -> None:
        self.captured_kwargs: dict[str, object] = {}

    def create_client(self, service_name: str, **kwargs: object) -> _FakeClientCM:
        assert service_name == "s3"
        self.captured_kwargs = kwargs
        return _FakeClientCM()


@pytest.mark.asyncio
async def test_s3_backend_wires_virtual_addressing_and_checksum_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSS needs explicit virtual-hosted addressing (W0: OSS rejects
    path-style with ``SecondLevelDomainForbidden``) + ``when_required``
    checksum behavior (W0: OSS rejects the default streaming checksum
    trailer)."""
    fake_session = _FakeSession()
    monkeypatch.setattr("aiobotocore.session.get_session", lambda: fake_session)

    config = S3CompatibleConfig(
        endpoint_url="https://oss-cn-hangzhou.aliyuncs.com",
        region="cn-hangzhou",
        bucket="b",
        access_key="ak",
        secret_key="sk",
        addressing_style="virtual",
    )
    async with make_object_store("s3-compatible", config):
        pass

    boto_config = fake_session.captured_kwargs["config"]
    assert isinstance(boto_config, BotoConfig)
    assert boto_config.s3 == {"addressing_style": "virtual"}
    assert boto_config.request_checksum_calculation == "when_required"
    assert boto_config.response_checksum_validation == "when_required"


@pytest.mark.asyncio
async def test_s3_backend_wires_path_addressing_for_minio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = _FakeSession()
    monkeypatch.setattr("aiobotocore.session.get_session", lambda: fake_session)

    config = S3CompatibleConfig(
        endpoint_url="http://localhost:9000",
        region="us-east-1",
        bucket="b",
        access_key="ak",
        secret_key="sk",
        addressing_style="path",
    )
    async with make_object_store("s3-compatible", config):
        pass

    boto_config = fake_session.captured_kwargs["config"]
    assert isinstance(boto_config, BotoConfig)
    assert boto_config.s3 == {"addressing_style": "path"}
