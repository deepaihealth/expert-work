"""SSE stream bridge — decouples orchestrator producer from API consumer.

Algorithm pattern borrowed from bytedance/deer-flow runtime/stream_bridge/* @
``813d3c94``; module-level config coupling dropped, ``run_id`` typed as ``UUID``.
"""

from expert_work.runtime.stream_bridge.base import (
    END_SENTINEL as END_SENTINEL,
)
from expert_work.runtime.stream_bridge.base import (
    HEARTBEAT_FRAME as HEARTBEAT_FRAME,
)
from expert_work.runtime.stream_bridge.base import (
    HEARTBEAT_SENTINEL as HEARTBEAT_SENTINEL,
)
from expert_work.runtime.stream_bridge.base import (
    OutputHeartbeat as OutputHeartbeat,
)
from expert_work.runtime.stream_bridge.base import (
    StreamBridge as StreamBridge,
)
from expert_work.runtime.stream_bridge.base import (
    StreamEvent as StreamEvent,
)
from expert_work.runtime.stream_bridge.base import (
    is_end as is_end,
)
from expert_work.runtime.stream_bridge.factory import (
    StreamBridgeBackend as StreamBridgeBackend,
)
from expert_work.runtime.stream_bridge.factory import (
    make_stream_bridge as make_stream_bridge,
)
from expert_work.runtime.stream_bridge.memory import (
    InMemoryStreamBridge as InMemoryStreamBridge,
)

__all__ = [
    "END_SENTINEL",
    "HEARTBEAT_FRAME",
    "HEARTBEAT_SENTINEL",
    "InMemoryStreamBridge",
    "OutputHeartbeat",
    "StreamBridge",
    "StreamBridgeBackend",
    "StreamEvent",
    "is_end",
    "make_stream_bridge",
]
