"""清空镜像水位 —— 逼 sweep 重扫一遍,把存量脚手架行的 ``hidden`` 算出来(B-73 ①)。

``0156`` 是 expand-only 的:新列对存量行只有默认值 ``false``,而默认值**不是真值**。
真值在检查点的 ``additional_kwargs`` 里,只有 ``TranscriptMirrorSweep`` 重扫那条线程
时才会被算出来并覆盖(``hidden`` 是唯一在 ON CONFLICT 时也写的列)。

问题出在 sweep 的工作队列:它选的是「没有水位行 **或** 有新活动」的线程,所以一条
**一直没人说话的老线程永远不会被重选**,那些行就永远停在 ``false``。

``0156`` 的注释说「本迁移之前的行里,唯一可能是脚手架的就是 B-67 的段,而 B-67 还没
上过生产」—— **这句是错的,本迁移就是来修它的**。写隐藏消息的源头有三个,另外两个
早就在生产上跑:

* CM-1 的 ``<recovery-advisory>``(``graph_builder`` 注入,#497)
* 循环检测的 ``<system-reminder>``(``loop_detection`` 中间件,#1072)

测试环境实测(2026-09-18,``0156`` 落地后):B-67 段 50 行 / 33 线程、CM-1 advisory
**183 行 / 111 线程**、循环检测 2 行 / 2 线程 —— 后两类生产上照样有。

**为什么清水位,而不是直接 UPDATE 存量行**:按 ``content`` 前缀去匹配是猜,而检查点
的 ``additional_kwargs`` 是真相。清掉水位让 sweep 照原件重算一遍,判据和写入侧同源。
代价可量:sweep 每 60 秒一批 200 个线程,一万个线程约 50 分钟收敛;窗口期内镜像行
一行不少(只删水位表),搜索照常工作。

Revision ID: 0157_thread_mirror_resweep
Revises: 0156_thread_message_hidden
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0157_thread_mirror_resweep"
down_revision: str | Sequence[str] | None = "0156_thread_message_hidden"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    # 只删水位,不碰 ``thread_message`` —— 镜像是审计要看的忠实记录,重扫会按
    # ``(thread_id, seq)`` 幂等地覆盖回去,中间不存在缺行的窗口。
    op.execute("DELETE FROM thread_message_sync")


def downgrade() -> None:
    """无可回滚 —— 水位是派生状态,sweep 会自己重建。"""
