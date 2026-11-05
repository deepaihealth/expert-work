"""B-259 —— 改一项 MCP 配置时,池里其它项的长连接留给下一次构建复用。

平台池 / 租户池都是「一个缓存键一整池」。以前改其中任何一项(哪怕只是改说明)
都把整池关掉,所有正在跑的 run 在每一项上都报 "not started"。现在只处理改到的那
一项(退役或硬停),整池照样从缓存摘掉(下一次构建按新目录重建),但其它项的客户端
连同建它时的连接指纹一起「暂存」在这里;下一次构建读到同一项、指纹也没变,就直接
复用这个长连接,不重连。指纹变了(或这一项没了)的暂存客户端在下一次缓存时退役。

不变式:某个键有缓存池时,它的暂存为空(缓存那一刻把暂存全部认领或退役);只有
「摘掉缓存池 → 下一次缓存」之间才有暂存。调用方的锁保护这里的所有状态。
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass

from orchestrator.tools.mcp import MCPClient


@dataclass(frozen=True)
class KeptClient:
    """池里一个客户端 + 建它时的连接指纹 + 建它的时间(池 TTL 用的时钟)。"""

    client: MCPClient
    fingerprint: object
    born_at: float


class KeptMcpClients[K: Hashable]:
    """每个缓存键:当前缓存池的成员表,和摘掉缓存池后暂存待复用的客户端。"""

    def __init__(self) -> None:
        self._members: dict[K, dict[str, KeptClient]] = {}
        self._carry: dict[K, dict[str, KeptClient]] = {}

    def peek(self, key: K) -> dict[str, KeptClient]:
        """构建开始时看一眼暂存(不拿走:这次构建作废时它们还得留着)。"""
        return dict(self._carry.get(key, {}))

    def cached(self, key: K, members: Mapping[str, KeptClient]) -> list[tuple[str, MCPClient]]:
        """新池进缓存:记下成员;暂存里没被它复用的 ``(名字, 客户端)`` 交回给调用方退役。"""
        self._members[key] = dict(members)
        carry = self._carry.pop(key, {})
        return [
            (name, kept.client)
            for name, kept in carry.items()
            if name not in members or members[name].client is not kept.client
        ]

    def adopted(self, key: K, members: Mapping[str, KeptClient]) -> None:
        """不进缓存的最后一次构建复用了的暂存客户端,从此归那个池(随它退役)。"""
        carry = self._carry.get(key, {})
        for name, kept in members.items():
            if name in carry and carry[name].client is kept.client:
                del carry[name]

    def dropped(self, key: K) -> None:
        """缓存池整体退役 / 关闭了(TTL、整池失效):成员表跟着清掉。"""
        self._members.pop(key, None)

    def switch(self, key: K, name: str) -> KeptClient | None:
        """``name`` 变了(调用方已把缓存池摘掉):成员转进暂存;返回 ``name`` 自己的
        客户端(交调用方退役或硬停),其它项留在暂存里等复用。"""
        carry = self._carry.setdefault(key, {})
        carry.update(self._members.pop(key, {}))
        hit = carry.pop(name, None)
        if not carry:
            del self._carry[key]
        return hit

    def pop_key(self, key: K) -> list[MCPClient]:
        """整键失效:暂存的客户端交回给调用方硬停。"""
        self._members.pop(key, None)
        return [kept.client for kept in self._carry.pop(key, {}).values()]

    def pop_all(self) -> list[MCPClient]:
        """关机:所有暂存的客户端交回给调用方硬停。"""
        self._members.clear()
        clients = [kept.client for carry in self._carry.values() for kept in carry.values()]
        self._carry.clear()
        return clients
