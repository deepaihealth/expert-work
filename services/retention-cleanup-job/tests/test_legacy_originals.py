"""B-281 —— 迁移后留在 agent 根的上传原件:一次性清理的谓词(:func:`plan`)与文件那一半
(:func:`verify` / :func:`unlink_verified`)。

文件一律用 control-plane 自己的 ``thread_workspace_path`` 摆:老视图 = agent 根的原件,对话视图 =
迁移复制出来的副本,都在真实位置上。真库那一半见 ``test_legacy_originals_integration.py``。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest

from control_plane.api._workspace_shared import thread_workspace_path
from expert_work.persistence import InMemoryThreadMetaStore
from expert_work.protocol import ThreadMeta
from retention_cleanup_job.legacy_originals import (
    AuditedUpload,
    LegacyOriginalsTally,
    LiveUpload,
    Original,
    Verified,
    plan,
    unlink_verified,
    verify,
)

_BODY = b"%PDF-1.4 customer report"
_REF = "uploads/report.pdf"

Layout = Literal["conversation", "agent_root"]


class _World:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.tenant = uuid4()
        self.user = uuid4()
        self.threads = InMemoryThreadMetaStore()
        self.rows: list[LiveUpload] = []
        self.audits: list[AuditedUpload] = []
        self.gone: set[UUID] = set()

    async def thread(
        self,
        *,
        layout: Layout = "conversation",
        agent: str | None = "support-bot",
        user: UUID | None = None,
    ) -> ThreadMeta:
        tid = uuid4()
        await self.threads.create(
            thread_id=tid,
            tenant_id=self.tenant,
            created_by="u",
            user_id=user or self.user,
            agent_name=agent,
        )
        meta = self.threads._rows[tid].model_copy(update={"workspace_layout": layout})
        self.threads._rows[tid] = meta
        return meta

    def _write(self, rel: str, data: bytes) -> Path:
        target = self.root / str(self.tenant) / str(self.user) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def original(self, meta: ThreadMeta, data: bytes = _BODY, ref: str = _REF) -> Path:
        """The agent-root file the old view landed (where the old conversation uploaded)."""
        old = meta.model_copy(update={"workspace_layout": "agent_root"})
        return self._write(thread_workspace_path(ref, old), data)

    def copy(self, meta: ThreadMeta, data: bytes = _BODY, ref: str = _REF) -> Path:
        """Where the migration copied it (the conversation's own ``work/uploads/``)."""
        return self._write(thread_workspace_path(ref, meta), data)

    def register(
        self, meta: ThreadMeta, data: bytes = _BODY, ref: str = _REF, sha256: str | None = "auto"
    ) -> LiveUpload:
        row = LiveUpload(
            id=uuid4(),
            tenant_id=self.tenant,
            user_id=self.user,
            thread_id=meta.thread_id,
            ref=ref,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest() if sha256 == "auto" else sha256,
        )
        self.rows.append(row)
        return row

    def audit(
        self, meta: ThreadMeta, data: bytes = _BODY, ref: str = _REF, sha256: str | None = "auto"
    ) -> None:
        """A ``DOCUMENT_UPLOAD`` audit row only (a console upload / copy before B-281)."""
        self.audits.append(
            AuditedUpload(
                tenant_id=meta.tenant_id,
                thread_id=meta.thread_id,
                ref=ref,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest() if sha256 == "auto" else sha256,
            )
        )

    def metas(self) -> dict[tuple[UUID, UUID], ThreadMeta | None]:
        out: dict[tuple[UUID, UUID], ThreadMeta | None] = {
            (self.tenant, tid): meta for tid, meta in self.threads._rows.items()
        }
        for tid in self.gone:
            out[(self.tenant, tid)] = None
        return out

    def plan(self) -> tuple[list[Original], Tally]:
        tally = LegacyOriginalsTally()
        return plan(self.rows, self.audits, self.metas(), tally), tally

    def release(self, *, unlink: bool) -> list[tuple[str, int]]:
        originals, _ = self.plan()
        return [_release(str(self.root), o, unlink=unlink) for o in originals]


Tally = LegacyOriginalsTally


def _release(root: str, original: Original, *, unlink: bool) -> tuple[str, int]:
    """The file half the way the cleanup runs it: verify, then (``unlink``) delete on the same
    open handles."""
    checked = verify(root, original)
    if not isinstance(checked, Verified):
        return checked, 0
    try:
        if not unlink:
            return "deletable", checked.size
        outcome = unlink_verified(checked)
        return outcome, checked.size if outcome == "deleted" else 0
    finally:
        checked.opened.close()


@pytest.fixture
def world(tmp_path: Path) -> _World:
    return _World(tmp_path)


async def _migrated(world: _World, data: bytes = _BODY) -> tuple[ThreadMeta, Path, Path]:
    """An old conversation that uploaded ``report.pdf`` and was then migrated."""
    meta = await world.thread(layout="conversation")
    original = world.original(meta, data)
    copy = world.copy(meta, data)
    world.register(meta, data)
    return meta, original, copy


# ---------------------------------------------------------------------------
# The happy path: a byte-identical duplicate goes; the copy stays.
# ---------------------------------------------------------------------------


async def test_a_migrated_original_is_deletable_and_dry_run_touches_nothing(world: _World) -> None:
    _meta, original, copy = await _migrated(world)

    assert world.release(unlink=False) == [("deletable", len(_BODY))]
    assert original.read_bytes() == _BODY  # dry run deletes nothing

    assert world.release(unlink=True) == [("deleted", len(_BODY))]
    assert not original.exists()
    assert copy.read_bytes() == _BODY
    # Only the one file, never its directory (other conversations' uploads live there).
    assert original.parent.is_dir()


async def test_a_pre_sha256_row_is_compared_by_content(world: _World) -> None:
    """Rows from before B-271 have no sha256: the copy must still equal the original byte for
    byte (sha256 of both files), not just in size."""
    meta = await world.thread()
    original = world.original(meta)
    world.copy(meta)
    world.register(meta, sha256=None)

    assert world.release(unlink=True) == [("deleted", len(_BODY))]
    assert not original.exists()


@pytest.mark.parametrize(
    ("size_bytes", "sha256"),
    [(len(_BODY), None), (None, hashlib.sha256(_BODY).hexdigest()), (None, None)],
    ids=["audit-without-sha256", "audit-without-size", "audit-without-both"],
)
async def test_a_reference_missing_size_or_sha256_next_to_a_full_one(
    world: _World, size_bytes: int | None, sha256: str | None
) -> None:
    """A registered upload and an older audit row of the same file (no sha256 / size, written
    before those were recorded) name the same copy with ``None`` next to a value: grouping
    them must not compare ``None`` with ``str`` / ``int`` (live test 97.7 crashed on 16 of
    27 candidates)."""
    meta, original, _ = await _migrated(world)
    world.audits.append(
        AuditedUpload(
            tenant_id=meta.tenant_id,
            thread_id=meta.thread_id,
            ref=_REF,
            size_bytes=size_bytes,
            sha256=sha256,
        )
    )

    originals, tally = world.plan()
    assert tally.checked == 1
    assert len(originals) == 1 and len(originals[0].copies) == 2
    assert world.release(unlink=True) == [("deleted", len(_BODY))]
    assert not original.exists()


async def test_two_migrated_conversations_sharing_one_original(world: _World) -> None:
    """Two old conversations of one agent uploaded the same bytes under the same name (one file
    in the shared ``uploads/``); both migrated → the original goes once."""
    a, original, _ = await _migrated(world)
    b = await world.thread()
    world.copy(b)
    world.register(b)

    originals, tally = world.plan()
    assert len(originals) == 1 and len(originals[0].copies) == 2
    assert tally.checked == 1
    assert world.release(unlink=True) == [("deleted", len(_BODY))]
    assert not original.exists()
    assert a.thread_id != b.thread_id


# ---------------------------------------------------------------------------
# Rules 1-3 (registry + audit): anything that may still use the original keeps it.
# ---------------------------------------------------------------------------


async def test_a_conversation_still_in_the_old_view_keeps_the_original(world: _World) -> None:
    await _migrated(world)
    old = await world.thread(layout="agent_root")
    world.register(old)  # same name, same bytes → resolves to the very same file

    originals, tally = world.plan()
    assert originals == []
    assert (tally.checked, tally.kept_old_view_row) == (1, 1)


async def test_an_old_view_conversation_of_another_agent_does_not_keep_it(world: _World) -> None:
    """Same name under another agent is another file (``agents/<other>/uploads/``)."""
    await _migrated(world)
    other = await world.thread(layout="agent_root", agent="other-bot")
    world.register(other)

    originals, tally = world.plan()
    assert len(originals) == 1
    assert tally.kept_old_view_row == 0


async def test_a_row_whose_conversation_is_gone_keeps_the_original(world: _World) -> None:
    """Can't tell where a deleted conversation's file was → keep (same name, any agent)."""
    await _migrated(world)
    lost = await world.thread(agent="other-bot")
    world.register(lost)
    world.gone.add(lost.thread_id)

    originals, tally = world.plan()
    assert originals == []
    assert tally.kept_session_gone == 1


async def test_an_old_view_console_upload_keeps_the_original(world: _World) -> None:
    """Before B-281 a console upload (or a cross-session copy) into an old-view conversation had
    only an audit row: it shares the file. Whose workspace it landed in isn't in the audit, so an
    old-view conversation of any user of the same agent keeps it."""
    await _migrated(world)
    old = await world.thread(layout="agent_root", user=uuid4())
    world.audit(old)

    originals, tally = world.plan()
    assert originals == []
    assert tally.kept_unregistered_upload == 1


async def test_an_unmigrated_conversation_view_console_upload_keeps_the_original(
    world: _World,
) -> None:
    """A conversation-view conversation opened before B-262 P1 and not migrated yet: its console
    upload is only in the agent root (audit row only, no copy of its own) — deleting the original
    would lose it before its migration copies it."""
    _meta, original, _ = await _migrated(world)
    unmigrated = await world.thread()
    world.audit(unmigrated)

    assert world.release(unlink=True) == [("copy_missing", 0)]
    assert original.exists()


async def test_a_conversation_view_console_upload_with_its_own_copy_does_not_keep_it(
    world: _World,
) -> None:
    """After B-262 P1 a console upload landed in the conversation's own directory: it has its
    copy, so it is not using the original."""
    _meta, original, _ = await _migrated(world)
    newer = await world.thread()
    world.copy(newer)
    world.audit(newer)

    assert world.release(unlink=True) == [("deleted", len(_BODY))]
    assert not original.exists()


async def test_an_audited_upload_of_other_bytes_keeps_the_original(world: _World) -> None:
    """An audit row under the same name recording other bytes (an overwrite before B-262 P0): the
    original isn't a duplicate of everything that named it — keep."""
    _meta, original, _ = await _migrated(world)
    other = await world.thread()
    world.copy(other)
    world.audit(other, b"%PDF-1.4 customer rEport")

    assert world.release(unlink=True) == [("original_differs", 0)]
    assert original.exists()


async def test_audit_rows_of_a_purged_conversation_are_not_references(world: _World) -> None:
    await _migrated(world)
    purged = await world.thread(layout="agent_root")
    world.audit(purged)
    world.gone.add(purged.thread_id)

    originals, tally = world.plan()
    assert len(originals) == 1
    assert tally.kept_unregistered_upload == 0


async def test_an_audit_only_original_is_never_a_candidate(world: _World) -> None:
    """Only originals some live registry row points at are considered (rule 1)."""
    meta = await world.thread()
    world.original(meta)
    world.copy(meta)
    world.audit(meta)

    originals, tally = world.plan()
    assert originals == []
    assert tally.checked == 0


async def test_unbound_conversations_have_no_agent_root_original(world: _World) -> None:
    meta = await world.thread(agent=None)
    world.copy(meta)
    world.register(meta)

    originals, tally = world.plan()
    assert originals == []
    assert tally.checked == 0


# ---------------------------------------------------------------------------
# Rules 4-5 (files): the copy must be there and identical; the original must be a plain
# file with the same bytes.
# ---------------------------------------------------------------------------


async def test_no_original_is_the_common_case(world: _World) -> None:
    """Conversations opened after B-262 P1 never had an agent-root original."""
    meta = await world.thread()
    world.copy(meta)
    world.register(meta)

    assert world.release(unlink=True) == [("no_original", 0)]


async def test_a_missing_copy_keeps_the_original(world: _World) -> None:
    meta = await world.thread()
    original = world.original(meta)
    world.register(meta)  # flipped to the conversation view, but nothing was copied

    assert world.release(unlink=True) == [("copy_missing", 0)]
    assert original.exists()


async def test_a_copy_changed_since_upload_keeps_the_original(world: _World) -> None:
    meta = await world.thread()
    original = world.original(meta)
    world.copy(meta, b"%PDF-1.4 customer rEport")  # same size, other bytes
    world.register(meta)

    assert world.release(unlink=True) == [("copy_changed", 0)]
    assert original.exists()


async def test_an_original_overwritten_by_other_bytes_is_kept(world: _World) -> None:
    """Before B-262 P0 a same-name upload overwrote the shared file: the original is now someone
    else's bytes — not a duplicate of this conversation's copy."""
    meta = await world.thread()
    original = world.original(meta, b"%PDF-1.4 customer rEport")  # same size
    world.copy(meta)
    world.register(meta)

    assert world.release(unlink=True) == [("original_differs", 0)]
    assert original.exists()
    original.write_bytes(b"short")  # other size
    assert world.release(unlink=True) == [("original_differs", 0)]
    assert original.exists()


async def test_copies_with_different_bytes_keep_the_original(world: _World) -> None:
    """Two conversations registered the name with different bytes: the original can equal at
    most one of them, so it is not a duplicate of "the" upload — keep it."""
    await _migrated(world)
    other = b"%PDF-1.4 customer rEport"
    b = await world.thread()
    world.copy(b, other)
    world.register(b, other)

    [(outcome, _)] = world.release(unlink=True)
    assert outcome == "original_differs"


async def test_a_symlinked_original_is_never_followed(world: _World, tmp_path: Path) -> None:
    meta = await world.thread()
    world.copy(meta)
    world.register(meta)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(_BODY)
    link = world.original(meta)
    link.unlink()
    link.symlink_to(outside)

    assert world.release(unlink=True) == [("unsafe", 0)]
    assert outside.read_bytes() == _BODY
    assert link.is_symlink()


async def test_a_symlinked_directory_on_the_way_is_never_followed(
    world: _World, tmp_path: Path
) -> None:
    meta = await world.thread()
    world.copy(meta)
    world.register(meta)
    original = world.original(meta)
    elsewhere = tmp_path / "elsewhere"
    original.parent.rename(elsewhere)
    os.symlink(elsewhere, original.parent)

    assert world.release(unlink=True) == [("unsafe", 0)]
    assert (elsewhere / original.name).read_bytes() == _BODY


def test_the_summary_line_names_no_file_tenant_or_user() -> None:
    tally = LegacyOriginalsTally(checked=3, deletable=1, bytes=24, no_original=2)
    line = tally.line(apply=False)
    assert line.startswith("legacy_originals mode=dry-run checked=3 deletable=1 deleted=0 bytes=24")
    assert "report" not in line and "uploads" not in line


async def test_an_original_replaced_after_verification_is_not_deleted(world: _World) -> None:
    """The unlink happens on the handles opened during verification and only if the name still
    points at that inode: a file swapped in meanwhile (by a sandbox in an old-view conversation)
    stays."""
    _meta, original, _ = await _migrated(world)
    [candidate], _ = world.plan()
    checked = verify(str(world.root), candidate)
    assert isinstance(checked, Verified)
    try:
        replacement = original.with_name("swap.tmp")
        replacement.write_bytes(b"someone else's file")
        replacement.replace(original)

        assert unlink_verified(checked) == "original_differs"
    finally:
        checked.opened.close()
    assert original.read_bytes() == b"someone else's file"
