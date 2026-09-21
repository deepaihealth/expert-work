"""Per-user persistent-workspace repository — Stream J.15.

Registers the docker named volume backing each ``(tenant_id, user_id)``
pair's ``/workspace``. The volume outlives the ephemeral sandbox
containers that mount it. See ``docs/streams/STREAM-J-DESIGN.md`` § 9.
"""

from expert_work.persistence.workspace.base import (
    UserWorkspaceStore as UserWorkspaceStore,
)
from expert_work.persistence.workspace.base import (
    WorkspaceNotFoundError as WorkspaceNotFoundError,
)
from expert_work.persistence.workspace.base import (
    workspace_volume_name as workspace_volume_name,
)
from expert_work.persistence.workspace.dlq import (
    InMemoryVolumeBackupDLQ as InMemoryVolumeBackupDLQ,
)
from expert_work.persistence.workspace.dlq import (
    SqlVolumeBackupDLQ as SqlVolumeBackupDLQ,
)
from expert_work.persistence.workspace.dlq import (
    VolumeBackupDLQ as VolumeBackupDLQ,
)
from expert_work.persistence.workspace.dlq import (
    VolumeDLQRow as VolumeDLQRow,
)
from expert_work.persistence.workspace.dlq import (
    VolumeOpKind as VolumeOpKind,
)
from expert_work.persistence.workspace.layout import (
    RENDERED_FIGURE_DIR as RENDERED_FIGURE_DIR,
)
from expert_work.persistence.workspace.layout import (
    RENDERED_FIGURE_PAGE_STEM as RENDERED_FIGURE_PAGE_STEM,
)
from expert_work.persistence.workspace.layout import (
    RENDERED_FIGURE_SHA_HEX_LEN as RENDERED_FIGURE_SHA_HEX_LEN,
)
from expert_work.persistence.workspace.layout import (
    RENDERED_FIGURE_UNIT_PREFIX as RENDERED_FIGURE_UNIT_PREFIX,
)
from expert_work.persistence.workspace.layout import (
    SANDBOX_AGENTS_ROOT as SANDBOX_AGENTS_ROOT,
)
from expert_work.persistence.workspace.layout import (
    SANDBOX_SKILLS_ROOT as SANDBOX_SKILLS_ROOT,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_AGENTS_DIR as WORKSPACE_AGENTS_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_DELETE_PROTECTED_PREFIXES as WORKSPACE_DELETE_PROTECTED_PREFIXES,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_INPUTS_DIR as WORKSPACE_INPUTS_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_OVERFLOW_DIR as WORKSPACE_OVERFLOW_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_RESERVED_PREFIXES as WORKSPACE_RESERVED_PREFIXES,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_SHARED_DIR as WORKSPACE_SHARED_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_SKILLS_DIR as WORKSPACE_SKILLS_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_UPLOADS_DIR as WORKSPACE_UPLOADS_DIR,
)
from expert_work.persistence.workspace.layout import (
    is_delete_protected_workspace_path as is_delete_protected_workspace_path,
)
from expert_work.persistence.workspace.layout import (
    is_rendered_figure_rel as is_rendered_figure_rel,
)
from expert_work.persistence.workspace.layout import (
    is_reserved_workspace_path as is_reserved_workspace_path,
)
from expert_work.persistence.workspace.memory import (
    InMemoryUserWorkspaceStore as InMemoryUserWorkspaceStore,
)
from expert_work.persistence.workspace.sql import (
    SqlUserWorkspaceStore as SqlUserWorkspaceStore,
)

__all__ = [
    "RENDERED_FIGURE_DIR",
    "RENDERED_FIGURE_PAGE_STEM",
    "RENDERED_FIGURE_SHA_HEX_LEN",
    "RENDERED_FIGURE_UNIT_PREFIX",
    "SANDBOX_AGENTS_ROOT",
    "SANDBOX_SKILLS_ROOT",
    "WORKSPACE_AGENTS_DIR",
    "WORKSPACE_DELETE_PROTECTED_PREFIXES",
    "WORKSPACE_INPUTS_DIR",
    "WORKSPACE_OVERFLOW_DIR",
    "WORKSPACE_RESERVED_PREFIXES",
    "WORKSPACE_SHARED_DIR",
    "WORKSPACE_SKILLS_DIR",
    "WORKSPACE_UPLOADS_DIR",
    "InMemoryUserWorkspaceStore",
    "InMemoryVolumeBackupDLQ",
    "SqlUserWorkspaceStore",
    "SqlVolumeBackupDLQ",
    "UserWorkspaceStore",
    "VolumeBackupDLQ",
    "VolumeDLQRow",
    "VolumeOpKind",
    "WorkspaceNotFoundError",
    "is_delete_protected_workspace_path",
    "is_rendered_figure_rel",
    "is_reserved_workspace_path",
    "workspace_volume_name",
]
