import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hpr.content import required_texts
from hpr.measure import Measurer
from hpr.ppt_draw import BODY, Canvas, render_pptx
from hpr.ppt_layout import LayoutError
from hpr.qa import missing_texts, pptx_bounds, pptx_corpus, pptx_overflow, qa_pptx
from hpr.style import Layer, resolve
from hpr.theme import build_theme
from PIL import Image as PILImage
from pptx import Presentation
from pptx.oxml.ns import qn

SKILL = Path(__file__).resolve().parents[2] / "health-plan-report"
RENDER = SKILL / "scripts" / "render.py"
M = Measurer("/nonexistent")


@pytest.fixture
def workdir(tmp_path, sample):
    PILImage.new("RGB", (640, 360), "#DDEEEE").save(tmp_path / "trend-note.png")
    (tmp_path / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def _render(content, tmp, layers=()):
    style = resolve(list(layers), content).style
    out = tmp / "out.pptx"
    warnings = render_pptx(content, style, out, tmp, M)
    return out, style, warnings


def test_sample_renders_and_passes_qa(workdir, sample):
    out, style, warnings = _render(sample, workdir)
    prs = Presentation(str(out))
    assert prs.slide_width == 12192000 and prs.slide_height == 6858000
    assert len(prs.slides) >= 8
    qa = qa_pptx(out, required_texts(sample), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", qa
    assert warnings == []


def test_every_required_text_is_found_and_detector_bites(workdir, sample):
    out, _, _ = _render(sample, workdir)
    corpus = pptx_corpus(out)
    assert missing_texts(required_texts(sample), corpus) == []
    assert missing_texts(["一段根本不存在的文字"], corpus) == ["一段根本不存在的文字"]


def test_all_shapes_inside_slide(workdir, sample):
    out, _, _ = _render(sample, workdir)
    assert pptx_bounds(out) == []


def test_bounds_detector_bites(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    Canvas(slide, build_theme(resolve([]).style, "pptx")).text(
        900, 500, 200, 30, "越界", 14, "#000000"
    )
    p = tmp_path / "b.pptx"
    prs.save(str(p))
    assert pptx_bounds(p)


def test_overflow_detector_bites(tmp_path):
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    Canvas(slide, build_theme(resolve([]).style, "pptx")).text(
        40, 40, 80, 20, "很长的文字" * 20, 14, "#000000"
    )
    p = tmp_path / "o.pptx"
    prs.save(str(p))
    assert pptx_overflow(p, M)


def test_cjk_font_on_every_run(workdir, sample):
    out, _, _ = _render(sample, workdir)
    prs = Presentation(str(out))
    for slide in prs.slides:
        for sh in slide.shapes:
            if sh.has_text_frame:
                for para in sh.text_frame.paragraphs:
                    for run in para.runs:
                        ea = run.font._element.find(qn("a:ea"))
                        assert ea is not None and ea.get("typeface") == "微软雅黑"
                        assert run.font.name == "Arial"


def test_body_shapes_are_named(workdir, sample):
    out, _, _ = _render(sample, workdir)
    names = {sh.name for s in Presentation(str(out)).slides for sh in s.shapes}
    assert BODY in names
    assert names <= {
        "hpr:body",
        "hpr:chrome",
        "hpr:deco",
        "hpr:chart",
        "hpr:image",
        "hpr:logo",
        "hpr:media",
    }


@pytest.mark.parametrize("variant", ["band", "split", "minimal"])
@pytest.mark.parametrize("pos", ["top-left", "top-right", "bottom-left", "bottom-right", "center"])
def test_cover_variants_and_logo_positions(workdir, sample, variant, pos):
    PILImage.new("RGB", (300, 120), "#0B4F5C").save(workdir / "logo.png")
    sample["brand"]["logo_path"] = "logo.png"
    layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": pos, "toc": "on"})]
    out, style, warnings = _render(sample, workdir, layers)
    qa = qa_pptx(out, required_texts(sample), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", qa
    assert warnings == []
    logos = [sh for sh in Presentation(str(out)).slides[0].shapes if sh.name == "hpr:logo"]
    assert len(logos) == 1


def test_missing_logo_is_a_warning_not_a_failure(workdir, sample):
    sample["brand"]["logo_path"] = "nope.png"
    _, _, warnings = _render(sample, workdir)
    assert any("LOGO" in w for w in warnings)


def test_cli_writes_outputs_and_reports(workdir):
    style = workdir / "style.json"
    style.write_text(
        json.dumps({"color.primary": "深蓝", "brand.org_name": "别家"}), encoding="utf-8"
    )
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(workdir / "plan.json"),
            "--style",
            str(style),
            "--out-dir",
            str(workdir / "out"),
            "--basename",
            "李先生_20260928100000",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    summary = json.loads(res.stdout)
    assert summary["ok"] is True
    assert Path(summary["files"]["pptx"]).is_file()
    assert [e["key"] for e in summary["not_applied"]] == ["brand.org_name"]
    assert (workdir / "out" / "李先生_20260928100000.qa.json").is_file()
    assert (workdir / "out" / "李先生_20260928100000.params-report.json").is_file()


def test_existing_output_is_not_overwritten(workdir):
    args = [
        sys.executable,
        str(RENDER),
        "--content",
        str(workdir / "plan.json"),
        "--out-dir",
        str(workdir / "out"),
        "--basename",
        "a",
    ]
    assert subprocess.run(args, capture_output=True, check=False).returncode == 0  # noqa: S603
    target = workdir / "out" / "a.pptx"
    before = target.read_bytes()
    again = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    assert again.returncode == 1
    assert "已存在" in again.stdout + again.stderr
    assert target.read_bytes() == before


def test_cli_invalid_content_exit_1(workdir, sample):
    del sample["title"]
    bad = workdir / "bad.json"
    bad.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(bad),
            "--out-dir",
            str(workdir / "o"),
            "--basename",
            "x",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "title: 缺少必填字段" in json.loads(res.stdout)["errors"]


def test_missing_image_reports_block_path(workdir, sample):
    (workdir / "trend-note.png").unlink()
    (workdir / "plan.json").write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(workdir / "plan.json"),
            "--out-dir",
            str(workdir / "o"),
            "--basename",
            "x",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert any(e.startswith("sections[2].blocks[1]") for e in json.loads(res.stdout)["errors"])


def test_long_title_is_a_clear_error(workdir, sample):
    sample["title"] = "非常长的方案名称" * 12
    with pytest.raises(LayoutError, match="title"):
        _render(sample, workdir)


def test_basename_with_slash_rejected(workdir):
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(workdir / "plan.json"),
            "--out-dir",
            str(workdir / "o"),
            "--basename",
            "../x",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1


def test_period_label_is_drawn_even_when_subtitle_omits_it(workdir, sample):
    sample["subtitle"] = "控糖与体重管理"
    out, style, _ = _render(sample, workdir)
    assert "第 1 阶段" in pptx_corpus(out)
    qa = qa_pptx(out, required_texts(sample), build_theme(style, "pptx"), M)
    assert qa["status"] == "passed", qa


def _crowded_cover(sample, repeat):
    sample["title"] = "李先生健康管理方案之控糖减重与睡眠改善综合计划书"
    sample["subtitle"] = (
        "第 1 阶段 · 这是一个较长的副标题，用于说明本方案的适用范围与执行周期，"  # noqa: RUF001
        "内容相对较长需要换行显示"
    ) * repeat
    base = 3 + bool(sample.get("data_basis")) + bool(sample.get("manager"))
    sample["client"]["facts"] = [
        {"label": f"项目{i}", "value": f"数值说明{i}"} for i in range(12 - base)
    ]
    sample["brand"]["logo_path"] = "logo.png"


# split keeps the meta grid beside the title panel, so only a much longer subtitle reaches its limit
@pytest.mark.parametrize(("variant", "repeat"), [("band", 2), ("split", 4), ("minimal", 2)])
def test_cover_text_colliding_with_meta_is_an_error(workdir, sample, variant, repeat):
    PILImage.new("RGB", (300, 120), "#FFFFFF").save(workdir / "logo.png")
    _crowded_cover(sample, repeat)
    layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": "bottom-left"})]
    with pytest.raises(LayoutError) as exc:
        _render(sample, workdir, layers)
    assert exc.value.path in ("title", "subtitle")


def _plates(slide, logo):
    return [
        sh
        for sh in slide.shapes
        if sh.name == "hpr:deco"
        and sh.fill.type == 1
        and str(sh.fill.fore_color.rgb) == "FFFFFF"
        and sh.left <= logo.left
        and sh.top <= logo.top
        and sh.left + sh.width >= logo.left + logo.width
        and sh.top + sh.height >= logo.top + logo.height
    ]


@pytest.mark.parametrize(("variant", "plated"), [("band", True), ("minimal", False)])
def test_logo_on_dark_cover_sits_on_white_plate(workdir, sample, variant, plated):
    PILImage.new("RGB", (300, 120), "#0B4F5C").save(workdir / "logo.png")
    sample["brand"]["logo_path"] = "logo.png"
    layers = [Layer("x", {"cover.variant": variant, "brand.logo_position": "top-right"})]
    out, _, _ = _render(sample, workdir, layers)
    slide = Presentation(str(out)).slides[0]
    (logo,) = [sh for sh in slide.shapes if sh.name == "hpr:logo"]
    assert abs(logo.width / 12700 - 140) < 0.5 and abs(logo.height / 12700 - 56) < 0.5
    assert bool(_plates(slide, logo)) is plated
    assert pptx_bounds(out) == []


def _load_render():
    spec = importlib.util.spec_from_file_location("hpr_render_cli", RENDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_qa_failure_renames_output_and_exits_1(workdir, monkeypatch, capsys):
    mod = _load_render()

    def failed(path, required, theme, m):
        return {
            "status": "failed",
            "slides": 1,
            "missing": ["x"],
            "overflow": [],
            "out_of_bounds": [],
            "contrast": {"ok": True},
        }

    monkeypatch.setattr(mod, "qa_pptx", failed)
    out = workdir / "out"
    code = mod.main(
        ["--content", str(workdir / "plan.json"), "--out-dir", str(out), "--basename", "q"]
    )
    summary = json.loads(capsys.readouterr().out)
    assert code == 1
    assert summary["ok"] is False
    assert (out / "q.qa-failed.pptx").is_file()
    assert not (out / "q.pptx").exists()
    assert summary["files"]["pptx"] == str(out / "q.qa-failed.pptx")
    assert json.loads((out / "q.qa.json").read_text(encoding="utf-8"))["pptx"]["status"] == "failed"
    assert (out / "q.params-report.json").is_file()


@pytest.mark.parametrize("sidecar", ["q.qa-failed.pptx", "q.qa.json", "q.params-report.json"])
def test_rerun_refuses_when_any_output_of_basename_exists(workdir, sidecar):
    out = workdir / "out"
    out.mkdir()
    (out / sidecar).write_text("旧结果", encoding="utf-8")
    res = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(RENDER),
            "--content",
            str(workdir / "plan.json"),
            "--out-dir",
            str(out),
            "--basename",
            "q",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "已存在" in res.stdout
    assert not (out / "q.pptx").exists()
    assert (out / sidecar).read_text(encoding="utf-8") == "旧结果"


def _padded_logo(path):
    im = PILImage.new("RGBA", (300, 120), (0, 0, 0, 0))
    im.paste(PILImage.new("RGBA", (110, 110), (32, 64, 160, 255)), (5, 5))
    im.save(path)


def test_logo_plate_hugs_visible_logo_not_transparent_padding(workdir, sample):
    _padded_logo(workdir / "logo.png")
    sample["brand"]["logo_path"] = "logo.png"
    layers = [
        Layer(
            "x",
            {
                "cover.variant": "band",
                "brand.logo_position": "top-left",
                "brand.org_position": "top-left",
            },
        )
    ]
    out, _, _ = _render(sample, workdir, layers)
    slide = Presentation(str(out)).slides[0]
    (logo,) = [sh for sh in slide.shapes if sh.name == "hpr:logo"]
    (plate,) = _plates(slide, logo)
    pt = 12700
    assert abs(logo.width / pt - 56) < 0.5 and abs(logo.height / pt - 56) < 0.5
    assert plate.width / pt <= logo.width / pt + 2 * 8 + 1
    (org,) = [
        sh
        for sh in slide.shapes
        if sh.has_text_frame and sh.text_frame.text == sample["brand"]["org_name"]
    ]
    gap = (org.left - (plate.left + plate.width)) / pt
    assert 0 <= gap <= 16
    assert list(workdir.glob("logo*")) == [workdir / "logo.png"]


def test_end_slide_long_signature_gets_its_measured_height(workdir, sample):
    sample["brand"]["footer_signature"] = "示例健康管理中心健康管理团队与营养师团队联合出品" * 3
    out, _, _ = _render(sample, workdir)
    last = Presentation(str(out)).slides[-1]
    (sig,) = [
        sh
        for sh in last.shapes
        if sh.has_text_frame and sh.text_frame.text == sample["brand"]["footer_signature"]
    ]
    assert sig.height / 12700 > 16 * 1.35 * 1.5
    assert pptx_overflow(out, M) == []


def test_partial_failure_removes_this_runs_outputs_so_rerun_works(workdir, monkeypatch, capsys):
    mod = _load_render()

    def broken_pdf(content, style, out_path, base_dir):
        out_path.write_bytes(b"%PDF-partial")
        raise LayoutError("brand.disclaimer", "页脚过长")

    monkeypatch.setattr(mod, "render_pdf", broken_pdf)
    out = workdir / "out"
    args = ["--content", str(workdir / "plan.json"), "--out-dir", str(out), "--basename", "q"]
    code = mod.main([*args, "--format", "both"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 1 and "页脚过长" in summary["errors"][0]
    assert sorted(p.name for p in out.iterdir()) == []
    assert mod.main([*args, "--format", "pptx"]) == 0
    assert (out / "q.pptx").is_file()
