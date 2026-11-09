#!/usr/bin/env python
"""知识库智能体层评测的两个派生题集(B-206 11-09 尺子修正)—— 都从现有评测集与语料推出来,
不新写任何租户内容,同样的输入每次得到同样的题。

**难负例(FaithEval 式删答案句)**:取有答案题,把语料里含它答案锚文本的句子(表格按行)
全部删掉,文档其余部分原样留着 —— 题目变成「资料没写」,但相关上下文还在,正好是 11-05
记录里编造集中的那种情形(看到相邻事实就推出结论)。删句发生在解析之后、切块之前
(:class:`AnchorDeletingParser` 包住入库用的 ``parse_document``),文件名不变。
选题(:func:`pick_hard_negatives`)跑在本机解析缓存上,写一份 spec(只有题号、问题、被删的锚),
评测时 ``kb_agent_eval.py --hard-negatives <spec>`` 用它建一个删过句的库;同一次还跑那 30 道
有答案对照题(看删句有没有误伤、量「不该拒却拒了」)。选题规则:

* 只选 ``match: any`` 的题(「找全」题删一处删不干净);
* 删掉的句子里不能含任何对照题的锚(否则对照题自己变成没答案);
* 锚在语料里一处都找不到的不选(删不掉 = 题没变)。

**近名干扰(有答案)**:问句点了名的对象 E(名字取自它期望文档的文件名),语料里还有一份
文档名里带着近名 N(等长差一个字,或数字不同的同型号骨架,如 KX-305 / KX-217);这类题
看模型会不会把 N 的资料安到 E 头上、或因为看到两个相近的名字就不敢答。题不改,只给判分
补一句说明。

生成难负例 spec(本机,不花钱;解析缓存与 ``kb_build_dataset.py`` 共用)::

    uv run --no-sync python tools/eval/kb_hard_sets.py \\
        --dataset tools/eval/datasets/kb/huada_v1.yaml \\
        --chat-dataset tools/eval/datasets/kb/huada_v1.chat.yaml \\
        --corpus-root ~/kb-eval/AI知识库 --manifest ~/kb-eval/huada/manifest.csv \\
        --parsed-cache ~/kb-eval/huada/parsed --n 30 --out eval-out/kb/hard-negatives.yaml
"""

from __future__ import annotations

import argparse
import os
import random
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from kb_dataset import KbCase, load_dataset  # type: ignore[import-not-found]
from kb_metrics import anchor_key  # type: ignore[import-not-found]

#: 跨几个相邻句子找一条锚(锚偶尔跨句号)。
MAX_WINDOW = 3
DEFAULT_SEED = 20261109
HARD_NEGATIVE_SET = "hard_negative"
DISTRACTOR_SET = "distractor"

#: 句子 / 表格行:以这些字符结尾(字符留在本句)。
#: 。!?(全角)与半角 !?、换行;全角字符写成转义(ruff RUF001 会把它们折成半角)。
_UNIT_END = re.compile("(?<=[\u3002\uff01\uff1f!?\n])")
#: 文件名切对象名的分隔符(连字符不切:KX-305 是一个名字)。
#: 空白、半角 / 全角冒号括号逗号与符号、【】《》、引号。
_NAME_SEP = re.compile(
    "[\\s:\uff1a_/\\\\\u3010\u3011\\[\\]()\uff08\uff09,\uff0c\u3001\u00b7+&\uff06"
    "\u300a\u300b\"'\u201c\u201d]+"
)
_DIGITS = re.compile(r"[0-9]+")
#: 文档类型词(以这些结尾的名字是「价目表 / 参数 / 说明书」这类通用词,不是某个对象):
#: 差一个字的两个通用词(价目表 / 价格表、参数 / 参考)不算近名。只是文档类型的说法,不含租户内容。
_GENERIC_NAME = re.compile(
    "(?:表|书|单|册|图|稿|版|页|报告|指南|手册|清单|目录|参数|参考|说明|介绍|汇总|资料|证书|合同|"
    "方案|规划|培训|记录)$"
)
_MIN_NAME, _MAX_NAME = 2, 12


# ---------------------------------------------------------------------------
# 删句
# ---------------------------------------------------------------------------


def split_units(text: str) -> list[str]:
    """按句末标点与换行切;``"".join`` 得回原文。"""
    return [u for u in _UNIT_END.split(text) if u]


def _windows_for(key: str, ukeys: Sequence[str]) -> set[int]:
    """含 ``key`` 的最小相邻句窗口(每处出现一个),返回要删的句号。``key`` 为空(锚归一后什么
    都不剩)时什么都不删 —— 空串在哪儿都「找得到」。"""
    drop: set[int] = set()
    if not key:
        return drop
    for i in range(len(ukeys)):
        for width in range(1, MAX_WINDOW + 1):
            window = ukeys[i : i + width]
            if len(window) < width:
                break
            if key in "".join(window):
                # 去掉首句仍含 key 的窗口不是最小的(那处出现从后一句起算)
                if width == 1 or key not in "".join(window[1:]):
                    drop.update(range(i, i + width))
                break
    return drop


def delete_anchor_units(text: str, anchors: Iterable[str]) -> tuple[str, int]:
    """删掉含任一锚(归一后比对,见 ``kb_metrics.anchor_key``)的句子 / 表格行,每处出现都删。
    返回 ``(新文本, 删了几句)``。"""
    units = split_units(text)
    ukeys = [anchor_key(u) for u in units]
    drop: set[int] = set()
    for anchor in anchors:
        key = anchor_key(anchor)
        if key:
            drop |= _windows_for(key, ukeys)
    if not drop:
        return text, 0
    return "".join(u for i, u in enumerate(units) if i not in drop), len(drop)


def residue(text: str, anchors: Iterable[str]) -> list[str]:
    """还能在 ``text`` 里找到的锚。"""
    folded = anchor_key(text)
    return [a for a in anchors if anchor_key(a) and anchor_key(a) in folded]


class ControlAnchorDeletedError(ValueError):
    """难负例删句删掉了对照题的答案锚:这份库不能用来量误拒。"""


@dataclass(frozen=True)
class AnchorDeletingParser:
    """入库用的解析器外面包一层删句(进程池里跑,所以可以 pickle)。``guard`` = 这一轮对照题的
    答案锚:删句删掉了其中任何一条(原文有、删后没有),这份文档就入库失败,评测随即退出。"""

    anchors: tuple[str, ...]
    guard: tuple[str, ...] = ()

    def __call__(self, filename: str, raw: bytes) -> str:
        from control_plane.knowledge.parsing import parse_document  # 子进程里导入

        return self.apply(str(parse_document(filename, raw)), filename=filename)

    def apply(self, text: str, *, filename: str) -> str:
        """删句;``guard`` 里的对照锚原文有、删后没了就报错(这份库不能用来量误拒)。"""
        deleted, _dropped = delete_anchor_units(text, self.anchors)
        keys = {anchor_key(a): a for a in self.guard}
        lost = [keys[k] for k in _lost(text, deleted, list(keys))]
        if lost:
            raise ControlAnchorDeletedError(f"{filename}: deletion removes control anchors {lost}")
        return deleted


# ---------------------------------------------------------------------------
# 选难负例
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardNegative:
    case: KbCase
    anchors: tuple[str, ...]
    deleted_units: int
    documents: tuple[str, ...] = ()  # 删句动到的文档(估重新嵌入的量)

    def to_spec(self, set_label: str) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "set": set_label,
            "query": self.case.query,
            "anchors": list(self.anchors),
            "deleted_units": self.deleted_units,
            "documents": list(self.documents),
        }


def _dropped_units(text: str, anchors: Sequence[str]) -> int:
    ukeys = [anchor_key(u) for u in split_units(text)]
    drop: set[int] = set()
    for anchor in anchors:
        drop |= _windows_for(anchor_key(anchor), ukeys)
    return len(drop)


def _lost(before: str, after: str, keys: Sequence[str]) -> list[str]:
    """删句前在、删句后不在的锚(都按归一形)。"""
    folded_before, folded_after = anchor_key(before), anchor_key(after)
    return [k for k in keys if k and k in folded_before and k not in folded_after]


def pick_hard_negatives(
    candidates: Sequence[KbCase],
    control: Sequence[KbCase],
    texts: Mapping[str, str],
    *,
    n: int,
    seed: int = DEFAULT_SEED,
) -> tuple[list[HardNegative], dict[str, int]]:
    """按固定种子打乱后依次试,取够 ``n`` 道;返回 ``(选中的, 跳过原因计数)``。

    对照题核对按**累计**删句算(11-09 评审):候选加进来后,把已选的锚 + 它的锚一起从原文删
    (与评测建库时 :class:`AnchorDeletingParser` 一次删全部锚相同),每份它动到的文档里,删前
    在的对照锚删后都还得在 —— 只删半句、跨句整段删、两道题各删一处合起来删光,都算伤到对照。"""
    control_keys = [anchor_key(e.anchor) for c in control for e in c.expected]
    order = sorted(candidates, key=lambda c: c.id)
    random.Random(seed).shuffle(order)  # noqa: S311 - a reproducible sample
    picked: list[HardNegative] = []
    chosen: list[str] = []  # 已选的锚(累计)
    skipped: Counter[str] = Counter()
    for case in order:
        if len(picked) >= n:
            break
        if case.match != "any":
            skipped["match_all"] += 1
            continue
        anchors = tuple(dict.fromkeys(e.anchor for e in case.expected))
        hits = {name: _dropped_units(text, anchors) for name, text in texts.items()}
        documents = tuple(name for name, count in hits.items() if count)
        if not documents:
            skipped["anchor_not_found"] += 1
            continue
        trial = [*chosen, *anchors]
        if any(
            _lost(texts[name], delete_anchor_units(texts[name], trial)[0], control_keys)
            for name in documents
        ):
            skipped["touches_control"] += 1
            continue
        chosen += anchors
        picked.append(
            HardNegative(
                case=case,
                anchors=anchors,
                deleted_units=sum(hits.values()),
                documents=documents,
            )
        )
    return picked, dict(skipped)


@dataclass(frozen=True)
class HardNegativeSpec:
    cases: tuple[KbCase, ...]
    anchors: tuple[str, ...]  # 全部要删的锚(去重,按出现顺序)
    notes: dict[str, str]  # 题号 → 给判分的说明
    deleted: dict[str, tuple[str, ...]]  # 题号 → 这道题被删掉的锚(判分核对引文用)


def _note(anchors: Sequence[str]) -> str:
    deleted = ";".join(f"「{a}」" for a in anchors[:3])
    return (
        f"说明:这道题的答案原文已从资料里删掉(相关上下文还在),被删掉的原文:{deleted}。"
        "回答若仍给出被删掉的内容,或据相邻内容推出结论而没有标明是推断,按上面的规则记。"
    )


def load_hard_negative_spec(path: Path) -> HardNegativeSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("cases") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: hard-negative spec needs a non-empty 'cases' list")
    cases: list[KbCase] = []
    anchors: dict[str, None] = {}
    notes: dict[str, str] = {}
    deleted: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        ok = (
            isinstance(entry, Mapping)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("query"), str)
            and isinstance(entry.get("anchors"), list)
            and entry["anchors"]
            and all(isinstance(a, str) and a.strip() for a in entry["anchors"])
        )
        if not ok:
            raise ValueError(f"{path}: bad hard-negative spec entry {entry!r}")
        case_id = f"hn-{entry['id']}"
        cases.append(
            KbCase(
                id=case_id,
                query=entry["query"],
                source=HARD_NEGATIVE_SET,
                tags=("难负例", entry["id"]),
                expected=(),
                answer=None,
                label="no_answer",
            )
        )
        anchors.update(dict.fromkeys(entry["anchors"]))
        notes[case_id] = _note(entry["anchors"])
        deleted[case_id] = tuple(entry["anchors"])
    return HardNegativeSpec(
        cases=tuple(cases), anchors=tuple(anchors), notes=notes, deleted=deleted
    )


def hard_negative_cases(spec: HardNegativeSpec) -> list[tuple[str, KbCase]]:
    return [(HARD_NEGATIVE_SET, case) for case in spec.cases]


# ---------------------------------------------------------------------------
# 近名干扰
# ---------------------------------------------------------------------------


def _stem(filename: str) -> str:
    base = filename.replace("\\", "/")
    return base.rsplit(".", 1)[0] if "." in base.rsplit("/", 1)[-1] else base


def _names_of(filename: str) -> set[str]:
    return {
        part
        for part in _NAME_SEP.split(_stem(filename))
        if _MIN_NAME <= len(part) <= _MAX_NAME and not part.isdigit()
    }


def entity_names(filenames: Iterable[str]) -> set[str]:
    """文档名切出来的对象名(2~12 字、不是纯数字)。"""
    names: set[str] = set()
    for filename in filenames:
        names |= _names_of(filename)
    return names


def _one_edit(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b, strict=True)) == 1


def near_misses(name: str, names: Iterable[str]) -> list[str]:
    """``names`` 里与 ``name`` 相近却不是同一个的:等长差一个字,或数字不同的同一骨架
    (``KX-305`` / ``KX-217``)。互相包含的是同一对象的长短名,不算;文档类型词不算。"""
    if _GENERIC_NAME.search(name):
        return []
    skeleton = _DIGITS.sub("#", name)
    out = []
    for other in names:
        if other == name or other in name or name in other or _GENERIC_NAME.search(other):
            continue
        same_skeleton = skeleton != name and _DIGITS.sub("#", other) == skeleton
        if _one_edit(name, other) or same_skeleton:
            out.append(other)
    return sorted(out)


@dataclass(frozen=True)
class Distractor:
    case: KbCase
    entity: str
    near: tuple[str, ...]

    @property
    def note(self) -> str:
        near = "、".join(f"「{n}」" for n in self.near)
        return (
            f"说明:资料里还有名字相近的{near},与本题的「{self.entity}」不是同一个对象;"
            f"把它们的内容当成「{self.entity}」的回答,该结论 support 记 unsupported,"
            "matches_reference 填 no。"
        )


def pick_distractors(
    cases: Sequence[KbCase],
    filenames: Sequence[str],
    *,
    exclude_ids: set[str],
    n: int,
    seed: int = DEFAULT_SEED,
) -> list[Distractor]:
    """有答案题里,问句点了自己期望文档名里的对象、语料里又有近名文档的,固定种子取 ``n`` 道。"""
    all_names = entity_names(filenames)
    found: list[Distractor] = []
    for case in sorted(cases, key=lambda c: c.id):
        if case.id in exclude_ids or not case.expected:
            continue
        own_docs = {e.document for e in case.expected}
        own = entity_names(own_docs)
        named = sorted((x for x in own if x in case.query), key=len, reverse=True)
        for entity in named:
            near = [m for m in near_misses(entity, all_names) if m not in own]
            if near:
                found.append(Distractor(case=case, entity=entity, near=tuple(near[:3])))
                break
    random.Random(seed).shuffle(found)  # noqa: S311 - a reproducible sample
    return found[:n]


def corpus_texts(
    *, manifest: Path, corpus_root: Path, tiers: Sequence[str], cache_dir: Path, workers: int
) -> tuple[dict[str, str], list[str]]:
    """评测建库会入的每一份文档的解析文本,按清单 ``path`` 记;解析失败的 path 另列。

    文档清单用评测同一个 ``kb_eval.select_rows``(重复组只留第一份;同名不同目录的文件都入、
    超长 PDF 也入),不用 ``kb_build_dataset.parse_corpus`` —— 后者同名只留一份、超长 PDF 跳过,
    选题时会漏看评测库里实际有的文档(11-09 评审)。解析走 ``kb_build_dataset`` 的缓存。"""
    from concurrent.futures import ProcessPoolExecutor

    from kb_build_dataset import _parse_cached  # type: ignore[import-not-found]
    from kb_eval import read_manifest, select_rows  # type: ignore[import-not-found]

    rows = select_rows(read_manifest(manifest), tiers=tiers, limit_docs=0).rows
    paths = [row["path"] for row in rows]
    with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
        parsed = list(
            pool.map(_parse_cached, [corpus_root / p for p in paths], [cache_dir] * len(paths))
        )
    texts = {path: text for path, text in zip(paths, parsed, strict=True) if text is not None}
    return texts, [path for path, text in zip(paths, parsed, strict=True) if text is None]


# ---------------------------------------------------------------------------
# CLI:生成难负例 spec
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--chat-dataset", type=Path, required=True)
    parser.add_argument("--corpus-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--parsed-cache", type=Path, required=True)
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    from kb_agent_cases import select_cases  # type: ignore[import-not-found]

    main_set, chat_set = load_dataset(args.dataset), load_dataset(args.chat_dataset)
    control = [c for _label, c in select_cases(main_set, chat_set) if c.expected]
    control_ids = {c.id for c in control}
    texts, failed = corpus_texts(
        manifest=args.manifest.expanduser(),
        corpus_root=args.corpus_root.expanduser(),
        tiers=main_set.corpus_tiers or ("A", "D-A"),
        cache_dir=args.parsed_cache.expanduser(),
        workers=args.workers,
    )
    if failed:
        print(
            f"WARNING {len(failed)} document(s) could not be parsed and were not checked "
            "(the eval run still guards the control answers): " + ", ".join(failed[:5]),
            file=sys.stderr,
        )
    set_of = {c.id: "main" for c in main_set.cases} | {c.id: "chat" for c in chat_set.cases}
    candidates = [
        c for c in (*main_set.cases, *chat_set.cases) if c.expected and c.id not in control_ids
    ]
    picked, skipped = pick_hard_negatives(candidates, control, texts, n=args.n, seed=args.seed)
    touched = sorted({d for p in picked for d in p.documents})
    spec = {
        "seed": args.seed,
        "datasets": [main_set.name, chat_set.name],
        "parsed": len(texts),
        "unparsed": failed,
        "skipped": skipped,
        # 删过句的文档要重新切块、嵌入(其余文档嵌入走缓存):估价用。
        "touched": {"documents": len(touched), "chars": sum(len(texts[d]) for d in touched)},
        "cases": [p.to_spec(set_of[p.case.id]) for p in picked],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), "utf-8")
    deleted = sum(p.deleted_units for p in picked)
    print(
        f"picked {len(picked)} hard negatives ({deleted} sentences/rows deleted); "
        f"skipped {skipped}; candidates {len(candidates)} -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
