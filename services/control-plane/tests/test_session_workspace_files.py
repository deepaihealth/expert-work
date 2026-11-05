"""B-236 review 7 —— 会话的文件清单:对话视图只列本对话的 ``work/``,不列原样未改的开场副本。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from control_plane.api.sessions import _session_files
from expert_work.protocol import ThreadMeta
from expert_work.protocol.agent_key import sanitize_agent_key
from orchestrator.tools.conversation_seed import seed_conversation_sync
from orchestrator.tools.nas_workspace_store import NasWorkspaceStore


def _meta(tenant: object, user: object, thread: object, layout: str) -> ThreadMeta:
    return ThreadMeta.model_validate(
        {
            "thread_id": thread,
            "tenant_id": tenant,
            "user_id": user,
            "created_by": "u",
            "agent_name": "plan-agent",
            "workspace_layout": layout,
        }
    )


async def test_a_conversation_session_lists_only_its_own_outputs(tmp_path: Path) -> None:
    tenant, user, conv, other = uuid4(), uuid4(), uuid4(), uuid4()
    key = sanitize_agent_key("plan-agent")
    agent = tmp_path / str(tenant) / str(user) / "agents" / key
    (agent / "tpl").mkdir(parents=True)
    (agent / "tpl" / "t.md").write_text("t")
    store = NasWorkspaceStore(root=str(tmp_path))
    for c in (conv, other):
        seed_conversation_sync(store, tenant, user, key, c)
    (agent / "threads" / str(conv) / "work" / "report.docx").write_text("r")
    (agent / "threads" / str(other) / "work" / "theirs.docx").write_text("x")

    files = await _session_files(store, _meta(tenant, user, conv, "conversation"), conv, tenant)

    assert files == [{"path": f"threads/{conv}/work/report.docx", "size": 1}]


async def test_a_legacy_session_still_lists_the_user_root(tmp_path: Path) -> None:
    tenant, user, thread = uuid4(), uuid4(), uuid4()
    root = tmp_path / str(tenant) / str(user)
    root.mkdir(parents=True)
    (root / "a.md").write_text("a")
    store = NasWorkspaceStore(root=str(tmp_path))

    files = await _session_files(store, _meta(tenant, user, thread, "agent_root"), thread, tenant)

    assert files == [{"path": "a.md", "size": 1}]
