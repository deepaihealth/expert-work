"""B-292 —— 构建时没连上的 MCP 服务:在缓存池旁边记下来,退避重试。

平台 / 租户 / 用户 OAuth 池构建时,某个服务连不上(多是对方正在重启、还没就绪)
就跳过它、照常缓存。以前这个缺口要等失效事件或池 TTL(30 分钟)才补上;现在池
旁边记下缺了哪些服务,退避重拨:只拨缺的、在所有锁外拨,连上了再在锁内并进
**还是同一个**缓存池(拨号期间池被换掉 / 摘掉 / 过期就把拨出来的关掉)。

间隔从 60 秒起翻倍、上限池 TTL;下一次会落在池过期之后就停(那时整池重建本来就会
重拨所有服务)。所以一个池一辈子最多重试五次(60/120/240/480/960 秒),一直连不上
的服务(配置错、地址错)也只多拨这几次;不按异常类型区分「临时 / 永久」,因为连接
失败的形态太多(OSError、httpx 连接 / 5xx、MCP 握手超时、anyio 的 ExceptionGroup…),
把临时的错判成永久就又回到「藏 30 分钟」。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from orchestrator.tools.mcp import MCPServerPool


@dataclass(frozen=True)
class MissingServers[R]:
    """缓存池 ``pool`` 里缺的服务(名字 -> 重拨所需的登记 / 配置)和下一次重试的间隔。"""

    pool: MCPServerPool
    records: Mapping[str, R]
    delay: float


def next_retry_delay(delay: float, *, pool_age: float, ttl: float) -> float | None:
    """退避翻倍、上限 ``ttl``;下一次会落在池过期之后就返回 ``None``(不再排)。"""
    nxt = min(delay * 2, ttl)
    return nxt if pool_age + nxt < ttl else None
