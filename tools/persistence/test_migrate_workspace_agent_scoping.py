"""B-50 Task 11 —— 存量工作区搬迁脚本。

纯 ``tmp_path`` 文件树,零 docker、零库(照
``services/orchestrator/tests/test_nas_workspace_store.py`` 的姿势):
``plan_migration`` 只读文件系统 + 一个已经查好的 :class:`Attributions`,
所以归属判定的每一档都能单独喂进去验,不用起数据库。

**守恒是这套测试的主判据。** 搬迁动的是用户唯一的一份工作记忆,
「少了一个文件」在真栈里表现为模型某天突然不记得客户是谁 —— 没有报错、
没有日志、隔几周才被发现。所以每条用例都顺带断言文件总数不变。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from tools.persistence.migrate_workspace_agent_scoping import (
    Attributions,
    MigrationPlan,
    MigrationReport,
    _render,
    apply_migration,
    plan_migration,
)

_KEY_A = "plan-aaaaaaaa"
_KEY_B = "sop-bbbbbbbb"
TENANT = UUID("dd068302-5364-4174-8c5c-11d46aa7caa0")
USER = UUID("99d3c664-be10-4e9c-be03-4fcc81f12894")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _count_files(root: Path) -> int:
    return sum(len(files) for _, _, files in os.walk(root, followlinks=False))


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, _, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = p.read_text(encoding="utf-8")
    return out


def _empty(sole: str | None = None) -> Attributions:
    return Attributions(
        sole_agent_key=sole,
        uploads={},
        artifacts={},
        threads={},
        runs={},
        artifact_paths=frozenset(),
    )


@pytest.fixture
def ids() -> tuple[UUID, UUID]:
    return uuid4(), uuid4()


# --------------------------------------------------------------------------
# 单 agent 用户 —— 实测 56/64 属于这一档
# --------------------------------------------------------------------------


def test_single_agent_user_moves_whole_tree(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """整棵树归唯一那个 agent,零歧义 —— 不用逐文件反推。"""
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "客户案例" / "秀域" / "x.md", "x")
    _write(root / "uploads" / "a.docx", "a")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves["MEMORY.md"] == f"agents/{_KEY_A}/MEMORY.md"
    assert plan.moves["客户案例/秀域/x.md"] == f"agents/{_KEY_A}/客户案例/秀域/x.md"
    assert plan.moves["uploads/a.docx"] == f"agents/{_KEY_A}/uploads/a.docx"
    assert plan.to_shared == ()


def test_single_agent_shortcut_needs_no_registry_rows(
    tmp_path: Path, ids: tuple[UUID, UUID]
) -> None:
    """捷径的意义就在这儿:没有任何登记行也不会有东西掉进 shared/。

    多 agent 那一档里 ``MEMORY.md`` 必进 ``shared/``(推不出归谁);
    单 agent 用户同一个文件必须进 agent 目录。两条路径的差别只由
    ``sole_agent_key`` 决定,这条用例把它钉住。
    """
    tenant_id, user_id = ids
    _write(tmp_path / str(tenant_id) / str(user_id) / "MEMORY.md", "m")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.to_shared == ()
    assert plan.moves == {"MEMORY.md": f"agents/{_KEY_A}/MEMORY.md"}


# --------------------------------------------------------------------------
# 多 agent 用户 —— 实测 8/64,逐类反推
# --------------------------------------------------------------------------


def test_multi_agent_user_splits_by_registry(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """有登记行的各归其位:uploads / threads / .tool_results / 产物 四类。"""
    tenant_id, user_id = ids
    thread_id, run_id = uuid4(), uuid4()
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "uploads" / "a.docx", "a")
    _write(root / "threads" / str(thread_id) / "PLAN.md", "p")
    _write(root / ".tool_results" / str(run_id) / "call_x-bash.txt", "o")
    _write(root / "报告.docx", "r")

    plan = plan_migration(
        str(tmp_path),
        tenant_id,
        user_id,
        attributions=Attributions(
            sole_agent_key=None,
            uploads={"uploads/a.docx": _KEY_A},
            artifacts={"报告.docx": _KEY_B},
            threads={str(thread_id): _KEY_B},
            runs={str(run_id): _KEY_A},
            artifact_paths=frozenset(),
        ),
    )

    assert plan.moves["uploads/a.docx"] == f"agents/{_KEY_A}/uploads/a.docx"
    assert plan.moves[f"threads/{thread_id}/PLAN.md"] == (
        f"agents/{_KEY_B}/threads/{thread_id}/PLAN.md"
    )
    assert plan.moves[f".tool_results/{run_id}/call_x-bash.txt"] == (
        f"agents/{_KEY_A}/.tool_results/{run_id}/call_x-bash.txt"
    )
    assert plan.moves["报告.docx"] == f"agents/{_KEY_B}/报告.docx"
    assert plan.to_shared == ()


def test_unattributable_goes_to_shared(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """无登记行的 legacy 进 shared/ —— **不猜**。

    猜错的代价不对称:猜对了省一次人工认领,猜错了把 A 的客户资料喂进 B 的
    上下文,而且事后查不出来(本来就是因为查不出归属才要猜)。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "style" / "PLAN_STYLE.md", "s")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty())

    assert set(plan.to_shared) == {"MEMORY.md", "style/PLAN_STYLE.md"}
    assert plan.moves["MEMORY.md"] == "shared/MEMORY.md"
    assert plan.moves["style/PLAN_STYLE.md"] == "shared/style/PLAN_STYLE.md"


def test_unregistered_thread_dir_goes_to_shared(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """``threads/<id>/`` 形状对但查不到登记行 —— 仍然不猜。

    形状可判(第一段是 ``threads``)不等于归属可判。会话行被清理过的目录
    正是这个形状,按「形状对就归第一个 agent」处理会静默串号。
    """
    tenant_id, user_id = ids
    gone = uuid4()
    _write(tmp_path / str(tenant_id) / str(user_id) / "threads" / str(gone) / "PLAN.md", "p")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty())

    assert plan.to_shared == (f"threads/{gone}/PLAN.md",)


# --------------------------------------------------------------------------
# 保留段与幂等
# --------------------------------------------------------------------------


def test_reserved_prefixes_are_never_moved(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """``skills/`` 是保留段,搬迁一个字节都不碰 —— 连单 agent 捷径也绕开它。"""
    tenant_id, user_id = ids
    _write(tmp_path / str(tenant_id) / str(user_id) / "skills" / "seeded.md", "s")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves == {}
    assert plan.untouched == ("skills/seeded.md",)


def test_already_migrated_tree_is_a_noop(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """重跑一遍不该再搬 —— 运维会重跑,而搬迁没有「只能跑一次」的保护。

    ``agents/`` 与 ``shared/`` 下的东西已经在终态,再当成待搬的源会把
    ``agents/<key>/x`` 搬成 ``agents/<key>/agents/<key>/x``。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "agents" / _KEY_A / "MEMORY.md", "m")
    _write(root / "shared" / "style" / "PLAN_STYLE.md", "s")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves == {}
    assert set(plan.untouched) == {
        f"agents/{_KEY_A}/MEMORY.md",
        "shared/style/PLAN_STYLE.md",
    }


def test_destination_already_has_a_newer_copy_is_not_overwritten(
    tmp_path: Path, ids: tuple[UUID, UUID]
) -> None:
    """目的地已有文件 → 源进 ``shared/``,**绝不覆盖**。

    这不是理论情形,是单 agent 用户的常态:PR3 上线后 agent 的写一律落
    ``agents/<key>/``,而读不到就回落用户根 —— 于是「读老的 ``MEMORY.md``、
    写新的 ``agents/<key>/MEMORY.md``」每天都在发生。搬迁若按计划把老的盖上去,
    抹掉的正是这段时间里所有的更新,而且事后无从恢复。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "老的")
    _write(root / "agents" / _KEY_A / "MEMORY.md", "新的")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves["MEMORY.md"] == "shared/MEMORY.md"
    assert plan.conflicts == ("MEMORY.md",)

    asyncio.run(apply_migration(plan, root=str(tmp_path), dry_run=False))
    assert (root / "agents" / _KEY_A / "MEMORY.md").read_text(encoding="utf-8") == "新的"
    assert (root / "shared" / "MEMORY.md").read_text(encoding="utf-8") == "老的"


# --------------------------------------------------------------------------
# apply —— 守恒与 dry-run
# --------------------------------------------------------------------------


def test_file_count_is_conserved(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """守恒:搬完文件总数不变,一个不多一个不少。"""
    tenant_id, user_id = ids
    thread_id = uuid4()
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "uploads" / "a.docx", "a")
    _write(root / "threads" / str(thread_id) / "PLAN.md", "p")
    _write(root / "skills" / "seeded.md", "s")
    before = _count_files(tmp_path)

    plan = plan_migration(
        str(tmp_path),
        tenant_id,
        user_id,
        attributions=Attributions(
            sole_agent_key=None,
            uploads={"uploads/a.docx": _KEY_A},
            artifacts={},
            threads={str(thread_id): _KEY_B},
            runs={},
            artifact_paths=frozenset(),
        ),
    )
    report = asyncio.run(apply_migration(plan, root=str(tmp_path), dry_run=False))

    assert _count_files(tmp_path) == before
    assert report.moved + report.to_shared + report.untouched == before


def test_apply_lands_bytes_at_the_planned_paths(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """搬完的位置就是 plan 说的位置,内容原样。

    只断言总数守恒是不够的 —— 把每个文件都搬到同一个目录下、数量也守恒。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "客户案例" / "秀域" / "x.md", "x")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))
    asyncio.run(apply_migration(plan, root=str(tmp_path), dry_run=False))

    for old, new in plan.moves.items():
        assert not (root / old).exists()
        assert (root / new).exists()
    assert (root / "agents" / _KEY_A / "MEMORY.md").read_text(encoding="utf-8") == "m"
    assert (root / "agents" / _KEY_A / "客户案例" / "秀域" / "x.md").read_text(
        encoding="utf-8"
    ) == "x"


def test_dry_run_changes_nothing(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """默认形态 —— 运维先看报告再决定,脚本不自作主张改盘。"""
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "uploads" / "a.docx", "a")
    before = _snapshot(tmp_path)

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))
    report = asyncio.run(apply_migration(plan, root=str(tmp_path), dry_run=True))

    assert _snapshot(tmp_path) == before
    assert report.moved == len(plan.moves) - len(plan.to_shared)


def test_apply_is_idempotent(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """跑两遍与跑一遍等价 —— 第二遍重新 plan,应该无事可做。"""
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")

    first = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))
    asyncio.run(apply_migration(first, root=str(tmp_path), dry_run=False))
    after_first = _snapshot(tmp_path)

    second = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))
    asyncio.run(apply_migration(second, root=str(tmp_path), dry_run=False))

    assert second.moves == {}
    assert _snapshot(tmp_path) == after_first


def test_missing_user_dir_is_an_empty_plan(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """用户目录不存在(从没起过沙箱)—— 空计划,不是异常。

    运维会拿一份用户清单逐个跑,里头必然有还没落过盘的用户。
    """
    tenant_id, user_id = ids

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves == {}
    assert plan.untouched == ()


def test_symlinks_are_not_followed(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """符号链接不跟进 —— 跟进会把链接目标(可能在工作区外)搬进来。

    ``NasWorkspaceStore.list_files`` 用的就是 ``followlinks=False``,搬迁必须
    同口径,否则枚举到的东西和 agent 看得见的东西对不上。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    outside = tmp_path / "outside"
    _write(outside / "secret.md", "s")
    (root / "link").symlink_to(outside, target_is_directory=True)

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert "link/secret.md" not in plan.moves
    assert all("secret" not in p for p in plan.moves)


def test_registry_rows_present_but_this_file_has_none(
    tmp_path: Path, ids: tuple[UUID, UUID]
) -> None:
    """多 agent 用户 + 有登记行 + 这个文件查不到 —— 仍然进 ``shared/``。

    **这是生产上那 8 个多 agent 用户的真实形状**,不是构造出来的边角:
    他们既有一堆 ``threads/<id>/`` 登记行,根级又躺着 ``MEMORY.md`` /
    ``style/`` / ``客户案例/`` 这批无登记行的 legacy。

    上面两条「进 shared/」的用例喂的都是**空** ``Attributions``,于是
    「查不到就从已知 agent 里挑一个」这种猜法在它们身上根本触发不到 ——
    变异自证时实测零杀。判据必须在「有得可猜」的条件下才有效
    (同「在不可能失败的条件下验证等于没验证」)。
    """
    tenant_id, user_id = ids
    thread_id = uuid4()
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "threads" / str(thread_id) / "PLAN.md", "p")
    _write(root / "MEMORY.md", "m")
    _write(root / "客户案例" / "秀域" / "x.md", "x")

    plan = plan_migration(
        str(tmp_path),
        tenant_id,
        user_id,
        attributions=Attributions(
            sole_agent_key=None,
            uploads={"uploads/a.docx": _KEY_A},
            artifacts={"报告.docx": _KEY_A},
            threads={str(thread_id): _KEY_B},
            runs={str(uuid4()): _KEY_A},
            artifact_paths=frozenset(),
        ),
    )

    assert set(plan.to_shared) == {"MEMORY.md", "客户案例/秀域/x.md"}
    assert plan.moves["MEMORY.md"] == "shared/MEMORY.md"
    assert plan.moves["客户案例/秀域/x.md"] == "shared/客户案例/秀域/x.md"
    # 有登记行的那条照常各归其位 —— 收紧不能收成「什么都进 shared/」。
    assert plan.moves[f"threads/{thread_id}/PLAN.md"] == (
        f"agents/{_KEY_B}/threads/{thread_id}/PLAN.md"
    )


def test_apply_actually_moved_every_planned_file(tmp_path: Path, ids: tuple[UUID, UUID]) -> None:
    """报告说搬了 N 个,盘上就得真有 N 个到位。

    ``test_file_count_is_conserved`` 那条**单独逮不住「apply 漏搬」**:没搬的
    文件还在原处,总数照样守恒;``report.moved`` 又是从计划算出来的,也照样
    对得上 —— 变异自证实测(apply 循环改 ``[1:]``)它确实是绿的,红的是另外
    三条。守恒是必要条件不是充分条件。

    这条补的是它们都没管的那一半:把**报告里的数**与**盘上的终态**对起来。
    前三条验的是「文件到位了」,这条验的是「到位的数量正是报告声称的数量」——
    运维拿着报告对账,那个数错了比文件没搬更难发现。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "MEMORY.md", "m")
    _write(root / "uploads" / "a.docx", "a")
    _write(root / "客户案例" / "秀域" / "x.md", "x")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))
    report = asyncio.run(apply_migration(plan, root=str(tmp_path), dry_run=False))

    landed = [dest for dest in plan.moves.values() if (root / dest).is_file()]
    assert len(landed) == len(plan.moves)
    assert report.moved + report.to_shared == len(landed)
    assert not [old for old in plan.moves if (root / old).exists()]


def test_tool_results_and_uploads_are_moved_not_skipped(
    tmp_path: Path, ids: tuple[UUID, UUID]
) -> None:
    """``.tool_results/`` 与 ``uploads/`` 都要搬 —— 它们在「浏览面隐藏」集合里,
    但**搬迁不碰**的集合与那个集合不是一回事。

    第一版把 ``_NEVER_MOVED`` 写成 ``WORKSPACE_RESERVED_PREFIXES - {uploads}``。
    Task 13a 往 ``RESERVED`` 里加了 ``.tool_results``(为了让浏览面别列它),
    搬迁**当场**静默不再搬它 —— 而那次改动完全在另一个包里,本文件一行没动,
    只有跑全量才红。这条把两个集合钉开。

    两者都可反推:``uploads/<name>`` 走 ``user_upload.thread_id``,
    ``.tool_results/<run_id>/`` 走 ``agent_run`` → thread → agent。
    """
    tenant_id, user_id = ids
    root = tmp_path / str(tenant_id) / str(user_id)
    _write(root / "uploads" / "a.docx", "a")
    _write(root / ".tool_results" / "r1" / "call_x.txt", "o")
    _write(root / "skills" / "seeded.md", "s")

    plan = plan_migration(str(tmp_path), tenant_id, user_id, attributions=_empty(sole=_KEY_A))

    assert plan.moves["uploads/a.docx"] == f"agents/{_KEY_A}/uploads/a.docx"
    assert plan.moves[".tool_results/r1/call_x.txt"] == (
        f"agents/{_KEY_A}/.tool_results/r1/call_x.txt"
    )
    # skills/ 仍然不碰 —— 收窄不能把它一起放开。
    assert plan.untouched == ("skills/seeded.md",)


def test_dry_run_reports_planned_artifact_rows_not_the_written_zero() -> None:
    """空跑必须报**计划要改多少行**,不能报恒为 0 的实改行数。

    2026-09-12 真栈空跑实测踩到:某用户 208 条产物登记行的 ``path_in_workspace``
    全都要改,而空跑报告印的是 ``artifact rows updated 0`` —— 因为空跑本来就
    不写库。运维看到 0 分不清两件事:

    * 空跑不计数(真相),还是
    * 真的一行都不用改 —— 那是红旗,意味着这 208 行搬完之后全指向旧路径、静默断链。

    两种归因下一步动作相反,而数字长得一模一样(同 [[same-observation-different-attribution]])。
    """
    plan = MigrationPlan(
        tenant_id=TENANT,
        user_id=USER,
        moves={"a.pptx": "agents/k-11111111/a.pptx"},
        to_shared=(),
        conflicts=(),
        untouched=(),
        artifact_path_updates={"a.pptx": "agents/k-11111111/a.pptx"},
    )
    report = MigrationReport(moved=1, to_shared=0, untouched=0, artifact_rows_updated=0)

    dry = _render(plan, report, dry_run=True)
    assert "artifact rows to update 1" in dry, dry
    assert "not written" in dry, dry
    assert "artifact rows updated 0" not in dry, dry


def test_applied_report_flags_only_updates_that_matched_nothing(tmp_path: Path) -> None:
    """红旗是「某条更新一行都没命中」,不是「行数 ≠ path 数」。

    第一版我把判据写成 ``rows_updated != len(artifact_path_updates)``,真跑第一下
    就误报:208 个 path 改到了 **223 行** —— 一个 path 挂着同一产物的多个版本行,
    两个数**本来就不相等**。而且那条告警的措辞对 ``>`` 的情形完全说反了
    (「差额的那些没匹配上」)。两个不同单位的数不能直接比。
    """
    plan = MigrationPlan(
        tenant_id=TENANT,
        user_id=USER,
        moves={},
        to_shared=(),
        conflicts=(),
        untouched=(),
        artifact_path_updates={"a.pptx": f"agents/{_KEY_A}/a.pptx"},
    )

    more_rows_than_paths = _render(
        plan,
        MigrationReport(
            moved=0, to_shared=0, untouched=0, artifact_rows_updated=7, artifact_updates_unmatched=0
        ),
        dry_run=False,
    )
    assert "artifact rows updated 7" in more_rows_than_paths
    assert "⚠️" not in more_rows_than_paths, (
        "7 行 vs 1 个 path 是正常的(多版本),不许告警:" + more_rows_than_paths
    )

    missed = _render(
        plan,
        MigrationReport(
            moved=0, to_shared=0, untouched=0, artifact_rows_updated=0, artifact_updates_unmatched=1
        ),
        dry_run=False,
    )
    assert "1 条更新在库里一行都没命中" in missed, missed


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    """够用的假会话 —— 只记下执行过什么,以及有没有 commit。"""

    def __init__(self, sink: list[dict[str, object]]) -> None:
        self._sink = sink
        self.committed = False
        #: 按顺序吐给每条 UPDATE 的 rowcount;用尽之后一律回 1。
        self.rowcounts: list[int] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def execute(self, _stmt: object, params: dict[str, object]) -> _FakeResult:
        self._sink.append(params)
        n = self.rowcounts.pop(0) if self.rowcounts else 1
        return _FakeResult(n)

    async def commit(self) -> None:
        self.committed = True


def test_apply_with_a_session_factory_actually_updates_artifact_rows(tmp_path: Path) -> None:
    """带 ``session_factory`` 的那条分支 —— 在 2026-09-13 之前**一次都没被执行过**。

    仓库里八处 ``apply_migration`` 调用全都不传 ``session_factory``,于是
    ``asyncio.run(_update_artifact_paths(...))`` 这行里的嵌套 ``asyncio.run``
    一直躺着:CLI 的 ``_main`` 本身就是 ``asyncio.run`` 起的,真跑必抛
    ``RuntimeError: asyncio.run() cannot be called from a running event loop``。
    第一个有产物登记行的真实用户就炸了,而且是**文件已经搬完之后**才炸 ——
    235 条登记行当场断链。

    这条测试的存在意义就是让这条分支真的被执行到。
    """
    _write(tmp_path / str(TENANT) / str(USER) / "a.pptx", "x")
    plan = MigrationPlan(
        tenant_id=TENANT,
        user_id=USER,
        moves={"a.pptx": f"agents/{_KEY_A}/a.pptx"},
        to_shared=(),
        conflicts=(),
        untouched=(),
        artifact_path_updates={"a.pptx": f"agents/{_KEY_A}/a.pptx"},
    )
    calls: list[dict[str, object]] = []
    sessions: list[_FakeSession] = []

    def factory() -> _FakeSession:
        s = _FakeSession(calls)
        sessions.append(s)
        return s

    # 假工厂只满足「调用一下拿到 async context manager」这一点协议,不是真的
    # async_sessionmaker —— 那玩意儿要一个引擎,而这条测试的全部意义是验分支
    # 被执行到、参数传对、commit 了,跟真库无关。
    report = asyncio.run(
        apply_migration(
            plan,
            root=str(tmp_path),
            dry_run=True,
            session_factory=factory,  # type: ignore[arg-type]
        )
    )
    assert report.artifact_rows_updated == 0, "空跑不许写库"
    assert calls == []

    report = asyncio.run(
        apply_migration(
            plan,
            root=str(tmp_path),
            dry_run=False,
            session_factory=factory,  # type: ignore[arg-type]
        )
    )
    assert report.artifact_rows_updated == 1
    assert calls == [
        {
            "new": f"agents/{_KEY_A}/a.pptx",
            "old": "a.pptx",
            "tenant_id": TENANT,
            "user_id": USER,
        }
    ]
    assert sessions[-1].committed, "改完必须 commit"


def test_plan_repairs_artifact_rows_whose_file_is_already_in_place(tmp_path: Path) -> None:
    """文件已经在终态、登记行还是旧路径 —— 计划必须把这一行补上。

    这是「跑到一半崩了」的恢复路径:文件全搬完、更新登记行那步抛了异常。
    重跑时 ``moves`` 是空的(没有文件需要搬),如果 ``artifact_path_updates``
    只从 ``moves`` 推导,那 235 条断链的行永远补不回来。
    """
    user_root = tmp_path / str(TENANT) / str(USER)
    _write(user_root / "agents" / _KEY_A / "done.pptx", "x")
    _write(user_root / "shared" / "orphan.pptx", "y")

    plan = plan_migration(
        str(tmp_path),
        TENANT,
        USER,
        attributions=Attributions(
            sole_agent_key=None,
            uploads={},
            artifacts={"done.pptx": _KEY_A, "orphan.pptx": _KEY_A},
            threads={},
            runs={},
            artifact_paths=frozenset({"done.pptx", "orphan.pptx"}),
        ),
    )

    assert plan.moves == {}, "文件都在终态,不该有搬迁"
    assert plan.artifact_path_updates == {
        "done.pptx": f"agents/{_KEY_A}/done.pptx",
        "orphan.pptx": "shared/orphan.pptx",
    }, plan.artifact_path_updates


def test_plan_leaves_alone_artifact_rows_whose_file_never_moved(tmp_path: Path) -> None:
    """还在扁平根上的登记行不许乱改 —— 上面那条恢复逻辑的反面。"""
    user_root = tmp_path / str(TENANT) / str(USER)
    _write(user_root / "still-here.pptx", "x")

    plan = plan_migration(
        str(tmp_path),
        TENANT,
        USER,
        attributions=Attributions(
            sole_agent_key=_KEY_A,
            uploads={},
            artifacts={"still-here.pptx": _KEY_A},
            threads={},
            runs={},
            artifact_paths=frozenset({"still-here.pptx"}),
        ),
    )
    # 它要搬,所以更新值来自搬迁表,而不是「已在终态」那条补丁
    assert plan.artifact_path_updates == {"still-here.pptx": f"agents/{_KEY_A}/still-here.pptx"}


def test_artifact_rows_follow_the_file_into_shared_even_without_an_agent_key(
    tmp_path: Path,
) -> None:
    """``agent_key`` 为空的产物照样要改登记行 —— 它的文件进了 ``shared/``。

    2026-09-13 真栈实见:12 条空 key 的产物行没跟着改,指向不存在的旧路径。
    根因是**一张表被当两件事用** —— ``artifacts`` 既是「归属判定表」(该过滤
    空 key)又被拿去当「哪些路径是登记过的产物」(不该过滤)。拆成两个结构之后
    这条才成立。
    """
    user_root = tmp_path / str(TENANT) / str(USER)
    _write(user_root / "owned.pptx", "a")
    _write(user_root / "unowned.md", "b")

    plan = plan_migration(
        str(tmp_path),
        TENANT,
        USER,
        attributions=Attributions(
            sole_agent_key=None,
            uploads={},
            # 只有 owned.pptx 判得出归属;unowned.md 的 agent_key 是空的
            artifacts={"owned.pptx": _KEY_A},
            threads={},
            runs={},
            artifact_paths=frozenset({"owned.pptx", "unowned.md"}),
        ),
    )

    assert plan.moves == {
        "owned.pptx": f"agents/{_KEY_A}/owned.pptx",
        "unowned.md": "shared/unowned.md",
    }
    assert plan.artifact_path_updates == {
        "owned.pptx": f"agents/{_KEY_A}/owned.pptx",
        "unowned.md": "shared/unowned.md",
    }, "空 key 的那条也必须安排更新,否则它的登记行会指向已被搬走的旧路径"


def test_single_agent_shortcut_still_carries_the_registered_artifact_paths() -> None:
    """单 agent 捷径跳过的是**归属映射**,不是 ``artifact_paths``。

    捷径档占 65 个用户里的 56 个。曾经把 ``artifact_paths`` 和归属表合成一个,
    于是捷径返回空表 —— **那 56 个用户的产物登记行从来不会被更新**,搬完集体
    指向旧路径,而报告里的 ``artifact rows updated 0`` 看着完全正常。

    这条只读源码,不连库:``collect_attributions`` 要真集群才跑得到,而捷径
    分支正是最容易在重构时被顺手清空的那个。
    """
    import ast
    import pathlib as _pathlib

    src = (
        _pathlib.Path(__file__).resolve().parent / "migrate_workspace_agent_scoping.py"
    ).read_text()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "collect_attributions"
    )
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    assert len(returns) == 2, f"期待捷径档 + 常规档两个 return,实际 {len(returns)}"
    for ret in returns:
        call = ret.value
        assert isinstance(call, ast.Call)
        kwargs = {k.arg for k in call.keywords}
        assert "artifact_paths" in kwargs, (
            "collect_attributions 的每一个 return 都必须带上 artifact_paths —— "
            "少一个就是那一档的用户产物登记行集体不更新,而且不报错。"
        )


def test_updates_that_match_zero_rows_are_counted(tmp_path: Path) -> None:
    """``rowcount == 0`` 的那条更新必须被数出来 —— 它是唯一的真红旗。

    钉的是 ``_update_artifact_paths`` 里的计数本身。上一版只测了渲染
    (直接构造 ``MigrationReport``),把计数那几行删掉一条测试都不红(变异零杀)。
    """
    user_root = tmp_path / str(TENANT) / str(USER)
    _write(user_root / "hit.pptx", "a")
    _write(user_root / "miss.pptx", "b")
    plan = MigrationPlan(
        tenant_id=TENANT,
        user_id=USER,
        moves={
            "hit.pptx": f"agents/{_KEY_A}/hit.pptx",
            "miss.pptx": f"agents/{_KEY_A}/miss.pptx",
        },
        to_shared=(),
        conflicts=(),
        untouched=(),
        artifact_path_updates={
            "hit.pptx": f"agents/{_KEY_A}/hit.pptx",
            "miss.pptx": f"agents/{_KEY_A}/miss.pptx",
        },
    )

    calls: list[dict[str, object]] = []

    def factory() -> _FakeSession:
        s = _FakeSession(calls)
        s.rowcounts = [3, 0]  # 第一条命中 3 行(多版本),第二条一行都没命中
        return s

    report = asyncio.run(
        apply_migration(
            plan,
            root=str(tmp_path),
            dry_run=False,
            session_factory=factory,  # type: ignore[arg-type]
        )
    )
    assert report.artifact_rows_updated == 3
    assert report.artifact_updates_unmatched == 1, "rowcount=0 的那条必须被数出来"
