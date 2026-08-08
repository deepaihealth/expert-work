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
    SANDBOX_AGENTS_ROOT as SANDBOX_AGENTS_ROOT,
)
from expert_work.persistence.workspace.layout import (
    SANDBOX_SKILLS_ROOT as SANDBOX_SKILLS_ROOT,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_DIR_MODE as WORKSPACE_DIR_MODE,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_FILE_MODE as WORKSPACE_FILE_MODE,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_RESERVED_PREFIXES as WORKSPACE_RESERVED_PREFIXES,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_SHARED_GID as WORKSPACE_SHARED_GID,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_SKILLS_DIR as WORKSPACE_SKILLS_DIR,
)
from expert_work.persistence.workspace.layout import (
    WORKSPACE_UPLOADS_DIR as WORKSPACE_UPLOADS_DIR,
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
    "SANDBOX_AGENTS_ROOT",
    "SANDBOX_SKILLS_ROOT",
    "WORKSPACE_DIR_MODE",
    "WORKSPACE_FILE_MODE",
    "WORKSPACE_RESERVED_PREFIXES",
    "WORKSPACE_SHARED_GID",
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
    "is_reserved_workspace_path",
    "workspace_volume_name",
]
