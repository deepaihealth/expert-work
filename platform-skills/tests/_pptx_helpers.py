"""Shared helpers for the pptx template tests (loaded by path, not collected by pytest).

Scripts run from the built package (``unpacked_skills``), as the agent runs them; decks
come from in_image/_pptx_fixtures.py so layer 2 builds the very same decks.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from lxml import etree
from pptx.oxml.ns import qn

FIXTURES = Path(__file__).parent / "in_image" / "_pptx_fixtures.py"


def load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses look their module up here
    spec.loader.exec_module(mod)
    return mod


fx = load("ps_pptx_fixtures", FIXTURES)


@pytest.fixture(scope="module")
def scripts(unpacked_skills, tmp_path_factory) -> Path:
    # a private copy: running the scripts writes __pycache__, which must not land in the
    # shared unpacked package that test_platform_checks scans file by file
    dst = tmp_path_factory.mktemp("pptx") / "scripts"
    shutil.copytree(unpacked_skills / "pptx" / "scripts", dst)
    return dst


@pytest.fixture(scope="module")
def text_mod(scripts):
    return load(f"ps_pptx_text_{id(scripts)}", scripts / "_pptx_text.py")


@pytest.fixture
def deck(tmp_path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "设计好的 模板.pptx"
    return path, fx.build_designed_deck(path)


def run(scripts: Path, name: str, *args, expect: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(scripts / name), *map(str, args)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == expect, proc.stdout + proc.stderr
    return proc


def inspect(scripts, path, *flags, legacy: bool = False) -> dict:
    """--texts output (the template workflow) unless ``legacy`` asks for the old default."""
    args = flags if legacy or "--texts" in flags else ("--texts", *flags)
    return json.loads(run(scripts, "inspect_template.py", path, *args).stdout)


def fill(scripts, tmp_path, src, spec, *, expect=0) -> tuple[dict | None, Path]:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "成品.pptx"
    proc = run(scripts, "fill_template.py", src, spec_path, out, expect=expect)
    return (json.loads(proc.stdout) if proc.stdout.strip() else None), out


def texts(result: dict) -> dict[str, dict]:
    return {t["addr"]: t for s in result["slides"] for t in s["texts"]}


def shape_by_addr(prs, addr: str):
    slide_no, path = addr.split("@")[0].split(":")
    shapes = prs.slides[int(slide_no) - 1].shapes
    shape = None
    for sid in path.split("/"):
        shape = next(s for s in shapes if s.shape_id == int(sid))
        shapes = getattr(shape, "shapes", None)
    return shape


def xml_by_id(prs) -> dict[tuple[int, str], bytes]:
    """Every shape element (any depth) by (slide number, cNvPr id) -> canonical bytes."""
    out = {}
    for n, slide in enumerate(prs.slides, start=1):
        tree = slide.shapes._spTree
        for c_nv in tree.iter(qn("p:cNvPr")):
            shape_el = c_nv.getparent().getparent()
            if shape_el is tree:
                continue
            out[(n, c_nv.get("id"))] = etree.tostring(shape_el, method="c14n")
    return out
