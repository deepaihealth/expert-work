"""知识库智能体层评测的选题:固定 60 题、近名干扰题、难负例轮,以及判分用的含答案段落。
从 ``kb_agent_eval.py`` 拆出(B-206 11-09)。"""

from __future__ import annotations

import argparse
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parent))

from kb_dataset import KbCase, KbDataset  # type: ignore[import-not-found]
from kb_eval import (  # type: ignore[import-not-found]
    _all_chunk_texts,
    _basename,
    _resolve_manifest,
    _resolve_tiers,
    read_manifest,
    select_rows,
)
from kb_hard_sets import (  # type: ignore[import-not-found]
    DISTRACTOR_SET,
    hard_negative_cases,
    load_hard_negative_spec,
    pick_distractors,
)
from kb_metrics import anchor_in, anchor_key  # type: ignore[import-not-found]

from expert_work.persistence import KnowledgeStore
from expert_work.protocol import DocumentStatus

SEED = 20261018
N_ANSWERABLE_PER_SET = 15
#: B-206 11-09:标准一轮里另加几道近名干扰题(有答案)。
DEFAULT_DISTRACTORS = 15
#: 判分时每题最多给几段「含答案的完整段落」。
GOLD_PASSAGES = 2


# ---------------------------------------------------------------------------
# 选题 / 判分
# ---------------------------------------------------------------------------


def select_cases(main: KbDataset, chat: KbDataset) -> list[tuple[str, KbCase]]:
    """两个集各取全部无答案题 + 固定种子抽 :data:`N_ANSWERABLE_PER_SET` 道有答案题。"""
    rng = random.Random(SEED)  # noqa: S311 - a fixed, reproducible sample
    picked: list[tuple[str, KbCase]] = []
    for label, dataset in (("main", main), ("chat", chat)):
        no_answer = [c for c in dataset.cases if not c.expected]
        answerable = [c for c in dataset.cases if c.expected]
        picked += [(label, c) for c in no_answer]
        picked += [(label, c) for c in rng.sample(answerable, N_ANSWERABLE_PER_SET)]
    return picked


async def gold_passages(
    store: KnowledgeStore, *, tenant_id: UUID, kb_id: UUID, cases: Sequence[KbCase]
) -> dict[str, list[str]]:
    """每道有答案题给判分的「含答案的完整段落」:期望文档里第一段含锚文本的段,最多
    :data:`GOLD_PASSAGES` 段。"""
    docs = {
        d.filename: d
        for d in await store.list_documents(tenant_id=tenant_id, kb_id=kb_id)
        if d.status is DocumentStatus.READY
    }
    chunks: dict[str, list[str]] = {}
    gold: dict[str, list[str]] = {}
    for case in cases:
        found: list[str] = []
        for exp in case.expected:
            if exp.document not in docs:
                continue
            if exp.document not in chunks:
                chunks[exp.document] = await _all_chunk_texts(
                    store, tenant_id=tenant_id, document_id=docs[exp.document].id
                )
            text = next((t for t in chunks[exp.document] if anchor_in(exp.anchor, t)), None)
            if text is not None and text not in found:
                found.append(text)
        gold[case.id] = found[:GOLD_PASSAGES]
    return gold


async def store_residue(
    store: KnowledgeStore, *, tenant_id: UUID, kb_id: UUID, anchors: Sequence[str]
) -> list[str]:
    """难负例臂:删过句的库里还能找到的锚(应为空;不空说明删句没删干净)。"""
    keys = {a: anchor_key(a) for a in anchors}
    left: set[str] = set()
    for doc in await store.list_documents(tenant_id=tenant_id, kb_id=kb_id):
        texts = await _all_chunk_texts(store, tenant_id=tenant_id, document_id=doc.id)
        folded = "\n".join(anchor_key(t) for t in texts)
        left |= {a for a, key in keys.items() if key and key in folded}
    return [a for a in anchors if a in left]


def stop_on_lost_controls(failures: Sequence[Any]) -> None:
    """难负例建库:解析器发现删句删掉了对照题答案(``ControlAnchorDeletedError``)就停。"""
    lost = [f.filename for f in failures if f.error_type == "ControlAnchorDeletedError"]
    if lost:
        raise SystemExit(
            f"hard-negative deletion removes control answers in {len(lost)} document(s) "
            f"({', '.join(lost[:5])}); regenerate the spec with kb_hard_sets.py"
        )


async def check_hard_negative_corpus(
    store: KnowledgeStore,
    *,
    tenant_id: UUID,
    kb_id: UUID,
    todo: Sequence[tuple[str, KbCase]],
    gold: Mapping[str, list[str]],
    plan: RunPlan,
) -> dict[str, Any]:
    """难负例建库后:删过的锚还能找到就停;对照题取不到含答案段落只警告(删句没删掉对照答案
    已由解析器逐文档核过,取不到是切块造成的,没删句的标准库上也会有)。返回记进 run 行的核对。"""
    left = await store_residue(store, tenant_id=tenant_id, kb_id=kb_id, anchors=plan.delete_anchors)
    if left:
        raise SystemExit(f"hard-negative corpus is not usable: deleted anchors still found {left}")
    no_gold = [c.id for _label, c in todo if c.expected and not gold.get(c.id)]
    if no_gold:
        print(f"WARNING control questions without a gold passage: {no_gold}", file=sys.stderr)
    return {
        "cases": sum(1 for _label, c in todo if c.source == "hard_negative"),
        "anchors": len(plan.delete_anchors),
        "residue": left,
        "controls_without_gold": no_gold,
    }


@dataclass(frozen=True)
class RunPlan:
    """这一轮跑哪些题:标准轮 = 固定 60 题 + 近名干扰题;难负例轮 = 30 道有答案对照题 +
    难负例(``delete_anchors`` 非空:建库时删这些锚所在的句子)。"""

    cases: list[tuple[str, KbCase]]
    notes: dict[str, str]
    delete_anchors: tuple[str, ...] = ()
    #: 难负例轮里对照题的答案锚:建库删句删掉其中任何一条就停(11-09 评审)。
    guard_anchors: tuple[str, ...] = ()
    #: 难负例题号 → 它被删掉的锚:判分引文对上这些就是编造(11-09 复审)。
    deleted_by_case: dict[str, tuple[str, ...]] = field(default_factory=dict)
    distractors: int = 0


def plan_run(args: argparse.Namespace, main_set: KbDataset, chat_set: KbDataset) -> RunPlan:
    picked = select_cases(main_set, chat_set)
    if args.hard_negatives is not None:
        spec = load_hard_negative_spec(args.hard_negatives)
        control = [(label, c) for label, c in picked if c.expected]
        return RunPlan(
            cases=control + hard_negative_cases(spec),
            notes=dict(spec.notes),
            deleted_by_case=dict(spec.deleted),
            delete_anchors=spec.anchors,
            guard_anchors=tuple(dict.fromkeys(e.anchor for _l, c in control for e in c.expected)),
        )
    if not args.distractors:
        return RunPlan(cases=picked, notes={})
    _manifest, selection = corpus_selection(args, main_set)
    found = pick_distractors(
        [*main_set.cases, *chat_set.cases],
        [_basename(row["path"]) for row in selection.rows],
        exclude_ids={c.id for _label, c in picked},
        n=args.distractors,
    )
    return RunPlan(
        cases=picked + [(DISTRACTOR_SET, d.case) for d in found],
        notes={d.case.id: d.note for d in found},
        distractors=len(found),
    )


def corpus_selection(args: argparse.Namespace, main_set: KbDataset) -> tuple[Path, Any]:
    """``(manifest.csv, 进评测库的清单行)``。"""
    manifest = _resolve_manifest(args, main_set)
    rows = read_manifest(manifest)
    return manifest, select_rows(rows, tiers=_resolve_tiers(None, main_set), limit_docs=0)
