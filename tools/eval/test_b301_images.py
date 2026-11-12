"""B-301 PR 2 —— 体检图集:张数与类、确定性、关键字段画上去了、字形齐、方向对、不带方向标记。"""

from __future__ import annotations

import io
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import b301_images as images_mod

from orchestrator.tools.region_zoom import exif_orientation


def _decode(data: bytes) -> np.ndarray:
    image = Image.open(io.BytesIO(data))
    image.load()
    return np.asarray(image.convert("L"), dtype=np.float32)


def test_the_set_is_11_classes_x_3_bases_x_4_turns_plus_4_without_text() -> None:
    image_set = images_mod.build_set()

    assert len(image_set) == 136
    assert len({image.image_id for image in image_set}) == 136
    per_class = Counter(image.image_class for image in image_set)
    assert per_class.pop(images_mod.NO_TEXT_CLASS) == 4
    assert len(per_class) == 11
    assert set(per_class.values()) == {12}
    assert Counter(image.rotation for image in image_set if image.key is not None) == {
        0: 33,
        90: 33,
        180: 33,
        270: 33,
    }
    assert sum(image.upright for image in image_set) == 37
    assert all(
        image.key is None for image in image_set if image.image_class == images_mod.NO_TEXT_CLASS
    )


def test_key_fields_are_not_only_numbers_and_not_only_chinese() -> None:
    specs = images_mod.base_specs()

    assert {spec.kind for spec in specs} == {"sentence", "name", "date", "number", "code"}
    assert sum(spec.script == "en" for spec in specs) >= 8
    assert sum(not any(ch.isdigit() for ch in spec.key) for spec in specs) >= 12
    assert len({spec.key for spec in specs}) == 33


def test_the_set_is_deterministic() -> None:
    again = images_mod.build_set.__wrapped__()

    assert [image.data for image in again] == [image.data for image in images_mod.build_set()]
    assert images_mod.set_digest(again) == images_mod.set_digest(images_mod.build_set())


@pytest.mark.parametrize("spec", images_mod.base_specs(), ids=lambda s: s.base_id)
def test_each_key_field_is_drawn_inked_and_covered_by_its_font(spec: images_mod.BaseSpec) -> None:
    flat = images_mod.render_flat(spec.base_id)

    drawn = [text for text, _face in flat.texts]
    assert any(spec.key in text for text in drawn), (spec.key, drawn)
    for text, face in flat.texts:
        if face != "seven-segment":
            assert images_mod.font_covers(face, text), (face, text)
    left, top, right, bottom = flat.key_box
    assert right - left > 20 and bottom - top > 15
    region = np.asarray(flat.image.convert("L").crop(flat.key_box), dtype=np.float32)
    assert region.std() > 20, "the key field box has no ink"


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_a_stored_turn_needs_exactly_that_clockwise_turn_to_be_upright(turn: int) -> None:
    by_id = {image.image_id: image for image in images_mod.build_set()}
    for base_id in ("ui-1", "ds-1", "vt-2"):
        upright = _decode(by_id[f"{base_id}@0"].data)
        stored = Image.open(io.BytesIO(by_id[f"{base_id}@{turn}"].data)).convert("L")
        back = np.asarray(stored.rotate(-turn, expand=True), dtype=np.float32)  # 顺时针转回去

        assert back.shape == upright.shape
        assert float(np.abs(back - upright).mean()) < 3.0
        if turn != 180:
            assert stored.size == (upright.shape[0], upright.shape[1])


def test_images_carry_no_orientation_tag_and_stay_small_enough_to_send() -> None:
    for image in images_mod.build_set():
        assert exif_orientation(image.data) == 1
        assert len(image.data) < 4_000_000
        with Image.open(io.BytesIO(image.data)) as opened:
            assert opened.format == "JPEG"
            assert opened.width * opened.height < 6_000_000
