""":class:`SandboxInstanceStore` —— ``AgentSandboxClient`` 依赖的 ``sandbox_instance`` 表契约。

从 :mod:`orchestrator.tools.agent_sandbox` **原样**搬出(全分支终审点名的
"最明显的一刀"):那个模块已经顶到仓内 800 行的单文件硬上限,而这个
Protocol 是里面最独立的一块——它一行可执行代码都没有,通篇是契约
docstring,搬走之后 ``agent_sandbox.py`` 少掉的都是与客户端实现逻辑无关
的文字。这次搬家**不改任何一个方法的签名或语义**,连 docstring 都逐字
照搬;本轮的行为修复各自在后续提交里。

两个生产实现在 :mod:`expert_work.persistence.sandbox_instance_store`
(``SqlSandboxInstanceStore`` / ``InMemorySandboxInstanceStore``)——
结构化子类型,不做形式继承(persistence 包不能反向依赖 orchestrator,
见该模块自己的 docstring)。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class SandboxInstanceStore(Protocol):
    """``sandbox_instance`` 表上 ``AgentSandboxClient`` 需要的操作。

    ``exec``(Task 8)最终没再加新方法——四契约点靠既有的
    :meth:`get_container_id`/``_attach`` 就够。``reap``(Task 9)加了
    :meth:`list_active`。

    独立审查(Important-1/2,task-9-report.md 有完整记录)追加两个方法:
    :meth:`touch_and_get_container_id`(``exec`` 用,读 container_id 的同时
    推进 ``last_used_at``,空闲判定不再退化成"多久以前 acquire 的")、
    :meth:`list_stuck_creating`(``reap(force=True)`` 用,清掉编排进程死于
    ``claim_warm``/``set_container_id`` 两次写之间留下的孤儿——
    :meth:`list_active` 两种模式都故意不返回这类行,见其 docstring)。
    """

    async def claim_warm(
        self, *, tenant_id: UUID, user_id: UUID, sandbox_id: UUID
    ) -> tuple[UUID, str] | None:
        """占 ``(tenant, user)`` 的热会话坑(spec § 6.2 CAS)。

        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` 的封装:

        * 占到 → 返回 ``None``,调用方负责建沙箱并回填 :meth:`set_container_id`。
        * 没占到、赢家已就绪(``container_id`` 非空)→ 返回
          ``(赢家那一行的 sandbox_id, container_id)`` 二元组 —— **不是**只
          返回 container_id。调用方(``acquire``)本次调用开头自己铸的
          ``uuid4()`` 从未插入任何行;如果 ``acquire`` 复用热会话成功后仍
          返回那个自铸 id,后续任何 ``destroy(sandbox_id=<那个 id>)`` 都会
          静默 no-op(``get_container_id`` 查不到、``mark_destroyed`` 的
          ``WHERE id=...`` 影响 0 行,两处都不报错)——沙箱杀不掉,热会话槽
          位也放不出来。返回赢家的真实行 id 让 ``acquire`` 能直接复用它,
          不需要再多打一次库。
        * 没占到、赢家还在创建中(``container_id`` 仍是 NULL)且这行**还没
          超过** ``_STUCK_CREATE_TTL_S`` → 允许实现 raise(两个生产实现都
          如此)。E2B 冷启实测 35-40s(见探针报告),这不是罕见边界窗口,
          调用方必须能收到一个明确错误,而不是悄悄再建一个沙箱(破坏"同时
          只有一个热沙箱"的不变式)或者在后续 :meth:`set_container_id` 时
          对着一个从未插入的行操作。
        * 没占到、``container_id`` 仍是 NULL、但这行**已经超过**
          ``_STUCK_CREATE_TTL_S`` → 实现必须**接管**它(清掉那行、重试这次
          claim),不能 raise。全分支终审 Critical-1:上一条那个 raise 如果
          不设时限,一次时机不巧的进程死亡(pod OOM-kill / 驱逐 / 滚动更新
          ——多副本部署上的日常事件,波 1 验收期间已经真实发生过一次)就会
          让那个 ``(tenant, user)`` **永久**失去沙箱能力——``claim_warm``
          提交的行只会被后来的 ``set_container_id`` 回填,进程死在两次写之
          间就没有任何处理器还会跑,而仓内没有任何自动路径能解开它
          (``acquire`` 的"重连失败则重建"分支拿不到返回值走不到、
          ``_create_and_track`` 的 unwind 只在同进程有效、
          :meth:`list_stuck_creating` 只有 ``reap(force=True)`` 会调)。
          5 分钟阈值约为实测冷启 35-40s 的 8 倍,不会跟一次合法的在途
          ``acquire`` 抢行。行的年龄无从判断时(时间戳缺失)不接管。
        """

    async def create_ephemeral(self, *, tenant_id: UUID, sandbox_id: UUID) -> None:
        """Task 10 实测发现的缺口(task-10-report.md):不带 ``user_id`` 的
        临时沙箱从不经过 :meth:`claim_warm`,过去没有方法给它做初始
        INSERT——``acquire`` 结尾的 :meth:`set_container_id` 因此是对从未
        插入的行做 UPDATE(此前零测试覆盖)。必须在 ``_create()`` 之前调用,
        理由同 :meth:`claim_warm`;migration 0141 排除 ``user_id IS NULL``,
        不需要 CAS,单纯 ``ON CONFLICT DO NOTHING``。
        """

    async def set_container_id(self, *, sandbox_id: UUID, container_id: str) -> None:
        """回填 E2B sandbox id。只应在调用方本次自己创建/重建了沙箱时调用
        ——connect 到一个早已就绪的热会话时,该行的 container_id 已经是对的,
        重复回填是浪费。

        再审 Important-2 —— 这一条从"允许实现各行其是"收紧成硬契约:
        **那一行不存在、或已经是终态(销毁)时,实现必须 raise,不许静默
        no-op**。此前 SQL 实现是裸 ``UPDATE ... WHERE id``(0 行照样提交、
        照样返回),内存实现是 ``KeyError`` —— 同一个输入两个后端走相反分支,
        而 C-1 的接管与 ``acquire`` 重建路径都让"行不在/已销毁"变成**可达**
        输入,不是理论边界。

        为什么选 raise 而不是选"两边都静默":这次写不是幂等记账,它是两段式
        占坑的后半段——那一行本身就是 CAS 的凭据。静默的话 ``acquire`` 会返
        回一个哪儿都不存在的 ``sandbox_id``,而它的 microVM 已经在平台上跑
        着:``destroy`` 找不到它(``get_container_id`` 返 ``None``)、
        ``reap`` 找不到它(``list_active`` 按行找)、``list_stuck_creating``
        也找不到它,只能等 20 分钟平台超时,期间这个 run 的下一次 ``exec``
        报一句莫名其妙的 "has no recorded container id"。raise 则让
        ``AgentSandboxClient`` 那层 Important-6 守卫做它本来该做的事:kill
        掉没人记得的沙箱、让出槽位、冒泡一个可重试的错误。

        调用方必须处理这个异常。今天唯一的生产调用点是 ``acquire`` 的
        post-create 块,它本来就在 ``try`` 里(Important-6),归一成
        ``SandboxSupervisorError``。
        """

    async def mark_destroyed(self, *, sandbox_id: UUID, reason: str) -> None:
        """标记销毁并让出热会话坑。

        再审 Important-1:这是**唯一**的"让出 0141 槽位"操作。曾经还有一个
        按 ``(tenant, user)`` 定位的 ``drop_warm``,已经删掉——C-1 的接管让
        "槽位当前的主人不是我"成为一个**可达**状态(接管之前不可能:
        ``claim_warm`` 对所有后来者都 raise,槽位主人恒等于插入它的那个调用
        方),而按坐标定位的删除删的是"此刻占着槽位的那一行",不是"我插的
        那一行"——调用方 A 被接管之后再走清理,删掉的就是接管者 B 那行健康
        的活行(B 的 microVM 随即从 :meth:`list_active` 消失,周期 reap 再也
        看不见它)。按 ``sandbox_id`` 定位严格更好:清理点至少指名道姓说出它要
        拆哪一行,而 0141 的部分唯一索引键在 ``state`` / ``destroyed_at`` /
        ``user_id`` 上,本方法两个都写,腾槽位的效果与按坐标删完全一样。

        **但"按 id 就一定是自己那行"并不成立**,别照这句话推论:
        ``acquire`` 的热会话重连失败分支拆的是 ``winner_id`` —— 那行是别人
        插的,而且 ``claim_warm`` 会把同一个赢家元组发给每一个输家,所以 A
        的 ``connect`` 只是暂时抽风时,它照样会拆掉正在被 B 使用的那一行。
        这个洞在改成按 id 之前就在(按坐标定位只会更糟),本轮没修;
        :meth:`touch_and_get_container_id` 的谓词收紧之后,B 的下一次
        ``exec`` 至少会响亮地报错而不是继续在一个已销毁的行上跑。

        实现必须"先到先得":终态行不再被第二个清理点重新盖章。见
        ``SqlSandboxInstanceStore.mark_destroyed`` 的 docstring —— 按 id
        让位把重复调用正好放在了接管路径上。
        """

    async def get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """读某行的 E2B sandbox id;行不存在或未回填返 ``None``。"""

    async def is_warm_session(self, *, sandbox_id: UUID) -> bool:
        """这一行是不是一个**该保温的**用户级热会话?

        全分支终审 Important-1:``AgentSandboxClient.release`` 要按这个判定
        分流,与本地 supervisor 的 ``release`` 逐条对齐——它的条件是
        ``record is not None and record.user_id is not None and record.state
        is IN_USE``(``supervisor.py:356-364``),三条全中才保温,否则
        ``destroy``。这里返回的就是那三条的合取:行存在、``user_id`` 非空、
        ``state='IN_USE'`` 且未销毁。

        ``get_container_id`` 顶不了这个班:它对"行不存在"和"行在但
        ``container_id`` 没回填"都返回 ``None``,更关键的是它压根不看
        ``user_id``——而"有没有 user_id"正是保温与销毁的分界线。
        """

    async def touch_and_get_container_id(self, *, sandbox_id: UUID) -> str | None:
        """独立审查 Important-2:跟 :meth:`get_container_id` 一样的读语义
        (行不存在或未回填返 ``None``),但同一次往返里把该行的
        ``last_used_at`` 一并推进到当前时间——:meth:`AgentSandboxClient.exec`
        用,替代"先读 container_id 再单独一次 UPDATE last_used_at"两次
        往返(``exec`` 已经在热路径上,不该多打一次库)。``destroy`` 之类
        只需要读的调用方仍然用 :meth:`get_container_id`。
        """

    async def list_active(self, *, only_idle: bool) -> list[tuple[UUID, str]]:
        """列出活跃(``state='IN_USE'``、未销毁、``container_id`` 已回填)的
        热会话行,返回 ``(sandbox_id, container_id)``——``reap`` 用。

        ``only_idle=True``(常规清扫,``force=False``)只返回超过空闲 TTL
        的行——判定口径与本地 docker-supervisor 自己的 reaper 相同(见
        ``sandbox_supervisor/store.py`` 的 ``_session_idle``):以
        ``last_used_at`` 为准(``exec`` 每次成功 attach 都会推进,见
        :meth:`touch_and_get_container_id`),缺失(还没 exec 过)退回
        ``acquired_at``;两者都缺失的行不返回。``only_idle=False``
        (``force`` 路径)返回全部活跃行,不看时间戳——运维强制拆除与
        M0→M1 Gate E2E 依赖这个确定性语义。

        还在创建中(``container_id`` 仍是 NULL)的行两种模式都不返回:E2B
        冷启 35-40s 的窗口里可能有另一路 ``acquire()`` 正在往这行写
        ``container_id``,此时 ``reap`` 抢着连一个还没写完的行既连不上、
        也没什么可 kill 的;那类行**正常**的清理由 ``_create_and_track``
        自己的失败分支(``_unwind_slot`` → :meth:`mark_destroyed`)负责,
        不是 ``list_active``/``reap`` 常规路径的职责。

        独立审查 Important-1 追加:上一句的"正常"两个字是关键——
        ``_create_and_track`` 的 ``except`` 只在 ``_create()`` **在同一个
        协程里同步抛异常**时才跑得到。如果编排进程是在 ``claim_warm``
        提交行之后、``set_container_id`` 完成之前**被杀掉**的(pod
        OOM-kill / 驱逐 / 滚动更新——按本系统的多副本生产设计,这些都是
        预期会发生的事件,不是理论边界),没有任何异常处理器会运行,那行
        永久停在 ``IN_USE`` + ``container_id=NULL``,``list_active`` 两种
        模式都会永远跳过它——这正是 Task 7 修过的"孤儿 CAS 行卡死
        ``(tenant, user)``"同一类故障,只是走了 Task 7 覆盖不到的"进程死于
        两次写之间"路径。全仓没有任何周期性清道夫会碰这类行(只有
        ``user_purge.py`` 的显式删用户路径会捎带清掉)。这类真正的孤儿由
        :meth:`list_stuck_creating` 负责,只在 ``force=True`` 时由 ``reap``
        调用——见该方法的 docstring。
        """

    async def list_stuck_creating(self) -> list[UUID]:
        """独立审查 Important-1:列出卡在"创建中"、且已经远超合法冷启窗口
        的 ``IN_USE`` 行——编排进程在 ``claim_warm`` 提交行之后、
        ``set_container_id`` 回填之前死掉(pod OOM-kill / 驱逐 / 滚动更新)
        留下的孤儿,不是正常还在 35-40s 冷启窗口内、随时可能被
        ``set_container_id`` 补完的行。判定阈值是
        :data:`~expert_work.persistence.sandbox_instance_store._STUCK_CREATE_TTL_S`
        (数分钟量级,留足余量不会跟合法冷启窗口混淆)。

        只应该在 ``force=True`` 的 ``reap`` 里调用——常规的 ``only_idle=True``
        空闲清扫不该碰"看起来还在创建"的行,那会跟一个正在合法冷启中的
        ``acquire()`` 抢同一行。返回值只有 ``sandbox_id``,没有
        ``container_id``(这类行压根没有对应的 E2B 容器)——调用方不该对
        返回的 id 尝试 ``connect``/``kill``,直接 ``mark_destroyed`` 即可。
        """
