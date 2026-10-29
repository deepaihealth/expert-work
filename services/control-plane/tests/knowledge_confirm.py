"""B-222 PR-2 —— an upload, re-ingest or 「补识别」 is estimated first and only
ingested once the user confirms the estimate. :func:`settle` is what tests that
used to upload and ``drain()`` the runner call instead."""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from control_plane.knowledge.ingestion import KnowledgeIngestionRunner


async def confirm_all(
    client: AsyncClient, base: str = "kb", *, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """「全部确认」 as the console does it: the base's waiting estimates,
    confirmed with the count and total the list reports (duplicates left as
    they are). Returns the summary it confirmed."""
    listed = await client.get(f"/v1/knowledge/bases/{base}/documents", headers=headers)
    assert listed.status_code == 200, listed.text
    summary: dict[str, Any] = listed.json()["estimate_summary"]
    if summary["awaiting"]:
        confirmed = await client.post(
            f"/v1/knowledge/bases/{base}/documents/confirm",
            json={
                "all": True,
                "expected_count": summary["awaiting"],
                "expected_total_micros": summary["total_micros"],
                "expected_fingerprint": summary["fingerprint"],
            },
            headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text
    return summary


async def settle(
    client: AsyncClient,
    runner: KnowledgeIngestionRunner,
    base: str = "kb",
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """Run the estimates (drain), confirm every one waiting in ``base``, run
    the paid ingests (drain again)."""
    await runner.drain()
    await confirm_all(client, base, headers=headers)
    await runner.drain()
