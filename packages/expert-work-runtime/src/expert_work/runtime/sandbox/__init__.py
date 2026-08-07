"""Sandbox runtime support — Stream F.3.

Builds the hardened ``docker run`` argv for ``exec_python`` sandbox
containers; the dev (``runc``) vs prod (``runsc`` / gVisor) split is a
single config knob via :func:`make_sandbox_runtime_provider`.
"""

from expert_work.runtime.sandbox.runtime_provider import (
    DEFAULT_EGRESS_NETWORK as DEFAULT_EGRESS_NETWORK,
)
from expert_work.runtime.sandbox.runtime_provider import (
    DEFAULT_RESOURCE_LIMITS as DEFAULT_RESOURCE_LIMITS,
)
from expert_work.runtime.sandbox.runtime_provider import (
    SANDBOX_AGENT_GID as SANDBOX_AGENT_GID,
)
from expert_work.runtime.sandbox.runtime_provider import (
    SANDBOX_AGENT_UID as SANDBOX_AGENT_UID,
)
from expert_work.runtime.sandbox.runtime_provider import (
    SandboxOciRuntime as SandboxOciRuntime,
)
from expert_work.runtime.sandbox.runtime_provider import (
    SandboxResourceLimits as SandboxResourceLimits,
)
from expert_work.runtime.sandbox.runtime_provider import (
    SandboxRuntimeProvider as SandboxRuntimeProvider,
)
from expert_work.runtime.sandbox.runtime_provider import (
    make_sandbox_runtime_provider as make_sandbox_runtime_provider,
)

__all__ = [
    "DEFAULT_EGRESS_NETWORK",
    "DEFAULT_RESOURCE_LIMITS",
    "SANDBOX_AGENT_GID",
    "SANDBOX_AGENT_UID",
    "SandboxOciRuntime",
    "SandboxResourceLimits",
    "SandboxRuntimeProvider",
    "make_sandbox_runtime_provider",
]
