"""B-286 —— object-store config from settings + ``secret://`` key refs (Stream J.6).

Moved here from ``control_plane.runtime`` (which still re-exports it) so the
retention-cleanup job builds the same object store as the control plane.
"""

from __future__ import annotations

from typing import Literal

from expert_work.runtime.secret_store import SecretStore, parse_secret_ref
from expert_work.runtime.storage.factory import ObjectStoreBackend, S3CompatibleConfig


async def resolve_object_store_config(
    *,
    backend: ObjectStoreBackend,
    endpoint_url: str | None,
    region: str,
    bucket: str,
    access_key_ref: str | None,
    secret_key_ref: str | None,
    secret_store: SecretStore,
    addressing_style: Literal["path", "virtual", "auto"] = "path",
) -> S3CompatibleConfig | None:
    """Build the S3 config for ``make_object_store`` (Stream J.6).

    ``None`` for the in-memory backend (no config needed). For
    ``s3-compatible`` the endpoint URL + both ``secret://`` key
    references are required; the keys are resolved through the
    SecretStore. Missing fields fail fast with a clear error.

    ``addressing_style`` defaults to ``"path"`` (MinIO local dev);
    callers must pass ``Settings.object_store_addressing_style`` through
    so OSS prod can request ``"virtual"`` (W0 real-bucket finding: OSS
    rejects path-style addressing).
    """
    if backend != "s3-compatible":
        return None
    if not (endpoint_url and access_key_ref and secret_key_ref):
        msg = (
            "object_store_backend='s3-compatible' requires object_store_endpoint_url "
            "+ object_store_access_key_ref + object_store_secret_key_ref"
        )
        raise RuntimeError(msg)
    return S3CompatibleConfig(
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key=await secret_store.get(parse_secret_ref(access_key_ref)),
        secret_key=await secret_store.get(parse_secret_ref(secret_key_ref)),
        addressing_style=addressing_style,
    )
