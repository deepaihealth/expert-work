"""B-209 PR-2 (review rounds 3-5) —— how much of a Word / slide file is read
for its pictures: a Word file's pictures are capped where the converter reads
them (and never inlined), a slide file is bounded before python-pptx loads it,
and both are checked for entry count, total size and XML part size first."""

from __future__ import annotations

import base64
import io
import random
import re
import struct
import tracemalloc
import warnings
import zipfile
from collections.abc import Callable
from typing import Any

import pytest

from control_plane.knowledge.parsing import (
    DocumentParseError,
    find_data_uri_pictures,
    office_markdown,
    parse_document,
)
from control_plane.knowledge.recognition import office_package
from control_plane.knowledge.recognition.figure_extract import parse_office_figures
from control_plane.knowledge.recognition.office_package import (
    STAND_IN,
    DocxPictureReader,
    bound_office_package,
    declared_directory,
    declared_entry_count,
)
from tests.figure_test_images import noise_png
from tests.knowledge_form_fixtures import _docx, picture_docx, picture_pptx

_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_BIG = noise_png(400, 300, seed=1)


def _rebuilt(
    raw: bytes,
    *,
    renames: dict[str, str] | None = None,
    rewrite: dict[str, Callable[[str], str]] | None = None,
    drop: tuple[str, ...] = (),
) -> bytes:
    """``raw`` (an Office package) with parts renamed, rewritten or dropped."""
    source = zipfile.ZipFile(io.BytesIO(raw))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as package:
        for item in source.infolist():
            if item.filename in drop:
                continue
            data = source.read(item)
            if item.filename in (rewrite or {}):
                data = (rewrite or {})[item.filename](data.decode()).encode()
            package.writestr((renames or {}).get(item.filename, item.filename), data)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Word: capped where mammoth reads the bytes (the reviewer's probe, A-K)
# ---------------------------------------------------------------------------

_IMAGE_RELATIONSHIP = re.compile(r'<Relationship Id="rId2"[^>]*/>')


def _word(new_name: str, attributes: str, *, override: str = "", drop_types: bool = False) -> bytes:
    """The picture renamed to ``new_name``, its relationship rewritten to
    ``attributes``, optionally an Override for it or no content types."""
    rewrite: dict[str, Callable[[str], str]] = {
        "word/_rels/document.xml.rels": lambda x: _IMAGE_RELATIONSHIP.sub(
            f"<Relationship {attributes}/>", x
        )
    }
    if override:
        rewrite["[Content_Types].xml"] = lambda t: t.replace(
            "</Types>", f'<Override PartName="/{new_name}" ContentType="{override}"/></Types>'
        )
    return _rebuilt(
        picture_docx(_BIG, before="Intro"),
        renames={"word/media/image1.png": new_name},
        rewrite=rewrite,
        drop=("[Content_Types].xml",) if drop_types else (),
    )


_VARIANTS = {
    "A xml-override-image": _word(
        "word/media/image1.xml",
        f'Id="rId2" Type="{_R}/image" Target="media/image1.xml"',
        override="image/png",
    ),
    "B xml-default": _word(
        "word/media/image1.xml", f'Id="rId2" Type="{_R}/image" Target="media/image1.xml"'
    ),
    "C single-quoted": _word(
        "word/media/image1.xml", f"Id='rId2' Type='{_R}/image' Target='media/image1.xml'"
    ),
    "D external-mode": _word(
        "word/media/image1.xml",
        f'Id="rId2" Type="{_R}/image" Target="media/image1.xml" TargetMode="External"',
    ),
    "E custom-type": _word(
        "word/media/image1.xml", f'Id="rId2" Type="{_R}/customXml" Target="media/image1.xml"'
    ),
    "F padded": _word(
        "word/media/image1.xml",
        f'Id="rId2" Type="{_R}/image" Target="media/image1.xml" x="{"a" * 4100}"',
    ),
    "G png-override-xml": _word(
        "word/media/image1.png",
        f'Id="rId2" Type="{_R}/image" Target="media/image1.png"',
        override="application/xml",
    ),
    "H png-override-xml-custom-type": _word(
        "word/media/image1.png",
        f'Id="rId2" Type="{_R}/customXml" Target="media/image1.png"',
        override="application/xml",
    ),
    "I upper-case-name": _word(
        "word/media/image1.XML", f'Id="rId2" Type="{_R}/customXml" Target="media/image1.XML"'
    ),
    "J no-content-types": _word(
        "word/media/image1.png",
        f'Id="rId2" Type="{_R}/image" Target="media/image1.png"',
        drop_types=True,
    ),
    "K percent-encoded": _word(
        "word/media/image1.xml", f'Id="rId2" Type="{_R}/image" Target="media/image1%2Exml"'
    ),
}


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_no_word_picture_is_read_past_the_cap(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    # Round 5 (A): whatever the name, type, relationship type, mode or quoting,
    # the bytes come through the reader, which applies the cap.
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", len(_BIG) - 1)
    raw = _VARIANTS[variant]
    parsed = parse_office_figures(raw, ".docx")
    assert parsed.figures.candidates == ()
    assert parsed.figures.unsupported == 1  # counted, not silently dropped
    assert "Intro" in parsed.markdown and "data:" not in parsed.markdown
    # The plain parse (no figure step) never reads a picture at all.
    assert find_data_uri_pictures(office_markdown(raw, ".docx")) == []
    assert "expert-work-figure" not in parse_document("a.docx", raw)


def test_a_word_picture_under_the_cap_is_still_read() -> None:
    parsed = parse_office_figures(_VARIANTS["C single-quoted"], ".docx")
    assert len(parsed.figures.candidates) == 1


def test_a_picture_referenced_many_times_is_read_once_and_never_inlined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round 5 (A, the reviewer's repeat probe): 100 references of one picture
    # made 48 MB of Markdown from a 0.44 MB upload.
    reads: list[str] = []
    real = DocxPictureReader._read_picture

    def counting(self: DocxPictureReader, image: Any) -> str:
        reads.append("read")
        return real(self, image)

    monkeypatch.setattr(DocxPictureReader, "_read_picture", counting)
    raw = picture_docx(_BIG, before="Intro", repeat=100)
    parsed = parse_office_figures(raw, ".docx")
    assert reads == ["read"]
    assert len(parsed.markdown) < 20_000
    # Read once, referenced 100 times: a repeated icon (filtered), not 100 copies.
    assert (parsed.figures.extracted, parsed.figures.filtered) == (1, 1)
    assert len(office_markdown(raw, ".docx")) < 20_000


def _image(data: bytes, *, path: str | None, fails: bool = False) -> Any:
    """A stand-in for mammoth's image element (``open`` closing over the
    part's ``image_path`` like mammoth's own)."""

    def opener(image_path: str | None) -> Callable[[], io.BytesIO]:
        def open_image() -> io.BytesIO:
            if fails:
                raise KeyError(image_path)
            return io.BytesIO(data)

        return open_image

    class _Image:
        content_type = "image/png"
        open = staticmethod(opener(path))

    return _Image()


def test_the_reader_caps_each_picture_and_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", 100)
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", 200)
    reader = DocxPictureReader()
    assert reader(_image(b"a" * 101, path="word/media/big.png")) == {
        "src": "expert-work-figure:skipped"
    }
    assert reader(_image(b"b" * 80, path="word/media/one.png"))["src"].endswith(":0")
    # Same bytes under another name: read (it counts), the same picture.
    assert reader(_image(b"b" * 80, path="word/media/copy.png"))["src"].endswith(":0")
    # 80 more bytes would pass the read budget.
    assert reader(_image(b"c" * 80, path="word/media/two.png"))["src"].endswith(":skipped")
    assert reader(_image(b"", path="word/media/gone.png", fails=True))["src"].endswith(":skipped")
    assert (len(reader.pictures), reader.skipped) == (1, 3)


def test_a_picture_is_read_at_most_one_byte_past_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", 100)
    asked: list[int] = []

    class Recording(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            asked.append(-1 if size is None else size)
            return super().read(size)

    class _Image:
        content_type = "image/png"

        @staticmethod
        def open() -> io.BytesIO:
            return Recording(b"a" * 10_000)

    assert DocxPictureReader()(_Image())["src"].endswith(":skipped")
    assert asked == [101]


def test_a_part_already_read_is_not_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", 150)
    reader = DocxPictureReader()
    for _ in range(5):  # 5 x 80 bytes would pass the budget if each were read
        assert reader(_image(b"d" * 80, path="word/media/one.png"))["src"].endswith(":0")
    # Without a part name every reference is read — and still counted.
    nameless = DocxPictureReader()
    results = [nameless(_image(b"d" * 80, path=None))["src"] for _ in range(3)]
    assert results[:1] == ["expert-work-figure:0"] and results[2].endswith(":skipped")


# ---------------------------------------------------------------------------
# Slides: bounded before python-pptx loads every part
# ---------------------------------------------------------------------------

_SLIDE_RELS = "ppt/slides/_rels/slide1.xml.rels"


def _deck(**rewrites: Callable[[str], str]) -> tuple[bytes, str]:
    raw = picture_pptx(_BIG)
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        rels = package.read(_SLIDE_RELS).decode()
    match = re.search(r'<Relationship Id="(rId\d+)"[^>]*Target="\.\./media/[^"]+"/>', rels)
    assert match is not None
    return raw, match.group(1)


def _deck_variant(attributes: Callable[[str], str], slide: Callable[[str], str] = str) -> bytes:
    raw, rid = _deck()
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        (picture,) = [n for n in package.namelist() if n.startswith("ppt/media/")]
    target = "../media/" + picture.rsplit("/", 1)[1]
    pattern = re.compile(rf'<Relationship Id="{rid}"[^>]*/>')
    return _rebuilt(
        raw,
        rewrite={
            _SLIDE_RELS: lambda x: pattern.sub(f"<Relationship {attributes(target)}/>", x),
            "ppt/slides/slide1.xml": slide,
        },
    )


_DECKS = {
    "single-quoted": lambda t: f"Id='rId2' Type='{_R}/image' Target='{t}'",
    "external-mode": lambda t: f'Id="rId2" Type="{_R}/image" Target="{t}" TargetMode="External"',
    "custom-type": lambda t: f'Id="rId2" Type="{_R}/customXml" Target="{t}"',
    "padded": lambda t: f'Id="rId2" Type="{_R}/image" Target="{t}" x="{"a" * 4100}"',
    "upper-case": lambda t: f'Id="rId2" Type="{_R}/image" Target="{t.upper()}"',
    "percent-encoded": lambda t: f'Id="rId2" Type="{_R}/image" Target="{t[:-4]}%2E{t[-3:]}"',
}


@pytest.mark.parametrize("variant", sorted(_DECKS))
def test_a_slide_picture_is_bounded_however_its_relationship_reads(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", len(_BIG) - 1)
    raw, rid = _deck()
    raw = _deck_variant(
        _DECKS[variant], slide=lambda x: x.replace(f'r:embed="{rid}"', 'r:embed="rId2"')
    )
    bounded, replaced = bound_office_package(raw, ".pptx")
    assert replaced == 1
    with zipfile.ZipFile(io.BytesIO(bounded)) as package:
        media = [n for n in package.namelist() if n.startswith("ppt/media/")]
        assert all(package.read(n) == STAND_IN for n in media)


@pytest.mark.parametrize("attribute", ["embed", "link", "id"])
def test_a_slide_picture_is_bounded_whichever_reference_names_it(
    monkeypatch: pytest.MonkeyPatch, attribute: str
) -> None:
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", len(_BIG) - 1)
    raw, _ = _deck()
    rewritten = _rebuilt(
        raw,
        rewrite={"ppt/slides/slide1.xml": lambda x: x.replace("r:embed=", f"r:{attribute}=")},
    )
    assert bound_office_package(rewritten, ".pptx")[1] == 1


def test_slides_layouts_and_masters_are_never_taken_for_pictures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # They are referenced by r:id too (presentation -> slides, master ->
    # layouts) but parsed as XML: replacing one would break the deck.
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", 1000)
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", 1000)
    raw = picture_pptx(_BIG, title="Deck")
    bounded, replaced = bound_office_package(raw, ".pptx")
    with zipfile.ZipFile(io.BytesIO(bounded)) as package:
        stood_in = {n for n in package.namelist() if package.read(n) == STAND_IN}
    assert replaced == len(stood_in) >= 1
    assert all(not name.endswith(".xml") for name in stood_in)
    assert "Deck" in parse_office_figures(raw, ".pptx").markdown
    # A slide typed by the Default for .xml alone is XML too.
    untyped = _rebuilt(
        raw,
        rewrite={
            "[Content_Types].xml": lambda t: re.sub(
                r'<Override PartName="/ppt/slides/slide1\.xml"[^>]*/>', "", t
            )
        },
    )
    assert (
        "slide1.xml"
        not in zipfile.ZipFile(io.BytesIO(untyped)).read("[Content_Types].xml").decode()
    )
    with zipfile.ZipFile(io.BytesIO(bound_office_package(untyped, ".pptx")[0])) as package:
        assert package.read("ppt/slides/slide1.xml") != STAND_IN


@pytest.mark.parametrize("kind", ["image/png", "image/svg+xml"])
def test_a_slide_picture_named_like_markup_is_still_a_picture(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Typed as an image by its Override: python-pptx loads it as a picture.
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", len(_BIG) - 1)
    raw, _ = _deck()
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        (picture,) = [n for n in package.namelist() if n.startswith("ppt/media/")]
    renamed = picture.rsplit(".", 1)[0] + ".xml"
    short = picture.rsplit("/", 1)[1]
    disguised = _rebuilt(
        raw,
        renames={picture: renamed},
        rewrite={
            _SLIDE_RELS: lambda x: x.replace(short, renamed.rsplit("/", 1)[1]),
            "[Content_Types].xml": lambda t: t.replace(
                "</Types>", f'<Override PartName="/{renamed}" ContentType="{kind}"/></Types>'
            ),
        },
    )
    bounded, replaced = bound_office_package(disguised, ".pptx")
    assert replaced == 1
    with zipfile.ZipFile(io.BytesIO(bounded)) as package:
        assert package.read(renamed) == STAND_IN


def test_slide_references_times_size_count_against_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One picture shown three times is inlined three times by the converter.
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", 2 * len(_BIG))
    raw, rid = _deck()
    once = bound_office_package(raw, ".pptx")
    assert once == (raw, 0)
    tripled = _rebuilt(
        raw,
        rewrite={
            "ppt/slides/slide1.xml": lambda x: x.replace(
                "</p:spTree>", f'<a:blip r:embed="{rid}"/><a:blip r:embed="{rid}"/></p:spTree>'
            )
        },
    )
    assert bound_office_package(tripled, ".pptx")[1] == 1


# ---------------------------------------------------------------------------
# Both: what is checked before anything is read
# ---------------------------------------------------------------------------


def test_an_oversized_xml_part_fails_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = picture_docx(noise_png(400, 300), before="Intro " * 200)
    monkeypatch.setattr(office_package, "MAX_XML_PART_BYTES", 1000)
    with pytest.raises(DocumentParseError, match="part of the file"):
        bound_office_package(raw, ".docx")


def test_a_package_that_unpacks_too_large_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = picture_docx(noise_png(400, 300), before="Intro")
    monkeypatch.setattr(office_package, "MAX_OFFICE_PACKAGE_BYTES", 10_000)
    with pytest.raises(DocumentParseError, match="unpacks"):
        bound_office_package(raw, ".docx")
    assert bound_office_package(b"not a zip", ".docx") == (b"not a zip", 0)


def test_the_entry_count_is_read_from_the_end_record_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Round 5 (I): refused before zipfile builds a list of every entry.
    raw = picture_docx(noise_png(400, 300))
    parts = len(zipfile.ZipFile(io.BytesIO(raw)).infolist())
    assert declared_entry_count(raw) == parts
    monkeypatch.setattr(office_package, "MAX_PACKAGE_ENTRIES", parts - 1)

    def no_listing(*args: object, **kwargs: object) -> object:
        raise AssertionError("the entry list was built")

    monkeypatch.setattr(office_package.zipfile, "ZipFile", no_listing)
    with pytest.raises(DocumentParseError, match="parts"):
        bound_office_package(raw, ".docx")


def test_the_zip64_entry_count_is_read() -> None:
    # A well-formed ZIP64 end record: Python 3.12.12+ / 3.13.8+ (CVE-2025-8291)
    # reject one whose size or version fields are inconsistent.
    record = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, 70_000, 70_000, 0, 0)
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    end = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0)
    assert declared_entry_count(record + locator + end) == 70_000
    assert declared_entry_count(b"no end record") is None


def test_a_failing_word_conversion_never_falls_back_to_reading_pictures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # MarkItDown tries the next converter when one raises — its own Word
    # converter would read every picture uncapped. Ours is the only one.
    from control_plane.knowledge import office_docx

    def broken(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(office_docx._TokenDocxConverter, "convert", broken)
    with pytest.raises(Exception, match="conversion failed"):
        office_docx.docx_markdown(picture_docx(_BIG, before="Intro"))


def test_pre_processing_never_holds_a_picture_twice() -> None:
    # MarkItDown's own pre-processing read every part whole and wrote each out
    # again uncompressed: a picture's bytes held twice before any cap applies.
    from control_plane.knowledge.office_docx import docx_markdown

    # Incompressible filler, never decoded (no figure step).
    picture = random.Random(0).randbytes(8 << 20)  # noqa: S311 — test data, not a secret
    raw = picture_docx(picture, before="Intro")
    tracemalloc.start()
    try:
        assert docx_markdown(raw).startswith("Intro")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1.5 * len(picture)


def test_word_math_is_still_turned_into_latex() -> None:
    from control_plane.knowledge.office_docx import docx_markdown

    math = "http://schemas.openxmlformats.org/officeDocument/2006/math"
    body = "<w:p><w:r><w:t>Before</w:t></w:r><m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath></w:p>"
    raw = _rebuilt(
        _docx(body),
        rewrite={
            "word/document.xml": lambda x: x.replace(
                "<w:document ", f'<w:document xmlns:m="{math}" '
            )
        },
    )
    assert "$x=1$" in docx_markdown(raw)


# ---------------------------------------------------------------------------
# Review round 6: the reviewer's probes (pptx_refs, pptx_types, eocd) and N6/N7
# ---------------------------------------------------------------------------


def _repeated_picture_deck(references: int, *, dtd: bool = False) -> bytes:
    """The slide's picture shown ``references`` times, optionally behind a DTD
    (which python-pptx reads past)."""

    def slide(text: str) -> str:
        picture = re.search(r"<p:pic>.*?</p:pic>", text, re.S)
        assert picture is not None
        text = text.replace(picture.group(0), picture.group(0) * references)
        if dtd:
            text = re.sub(r"^(<\?xml[^>]*\?>)", r'\1<!DOCTYPE p:sld [<!ENTITY x "y">]>', text)
        return text

    return _rebuilt(picture_pptx(_BIG), rewrite={"ppt/slides/slide1.xml": slide})


@pytest.mark.parametrize("dtd", [False, True])
def test_references_are_counted_past_a_dtd(monkeypatch: pytest.MonkeyPatch, dtd: bool) -> None:
    # N1: defusedxml refused the DTD, the 50 references counted once, and 50
    # inline copies of the picture got through.
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", len(_BIG) * 10)
    bounded, replaced = bound_office_package(_repeated_picture_deck(50, dtd=dtd), ".pptx")
    assert replaced == 1
    pictures = find_data_uri_pictures(office_markdown(bounded, ".pptx"))
    assert len(pictures) == 50
    assert all(base64.b64decode(picture.data) == STAND_IN for picture in pictures)


def test_a_slide_python_pptx_cannot_read_fails_the_document() -> None:
    raw = _rebuilt(picture_pptx(_BIG), rewrite={"ppt/slides/slide1.xml": lambda x: x[:-40]})
    with pytest.raises(DocumentParseError, match="unreadable part"):
        bound_office_package(raw, ".pptx")


def _added(raw: bytes, parts: dict[str, str]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as package:
        for item in source.infolist():
            package.writestr(item, source.read(item))
        for name, text in parts.items():
            package.writestr(name, text)
    return out.getvalue()


def test_an_unreadable_part_python_pptx_never_parses_counts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A legacy VML drawing (often not well-formed XML) is not parsed by
    # python-pptx, so nothing in it is shown: it must not fail the deck, and
    # each of its relationships still counts once.
    raw, _ = _deck()
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        (picture,) = [n for n in package.namelist() if n.startswith("ppt/media/")]
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="'
        'http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_R}/image" Target="../media/{picture.rsplit("/", 1)[1]}"/>'
        "</Relationships>"
    )
    extended = _added(
        raw,
        {
            "ppt/drawings/vmlDrawing1.vml": "<xml><v:shape><br></xml>",
            "ppt/drawings/_rels/vmlDrawing1.vml.rels": rels,
        },
    )
    assert bound_office_package(extended, ".pptx") == (extended, 0)
    # Slide + drawing: two references; a budget for one and a half.
    monkeypatch.setattr(office_package, "MAX_OFFICE_MEDIA_BYTES", len(_BIG) * 3 // 2)
    assert bound_office_package(extended, ".pptx")[1] == 1


def _decoy_types(raw: bytes) -> tuple[str, str]:
    """The deck's picture and its content types with that picture typed XML."""
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        (picture,) = [n for n in package.namelist() if n.startswith("ppt/media/")]
        types = package.read("[Content_Types].xml").decode()
    override = f'<Override PartName="/{picture}" ContentType="application/xml"/></Types>'
    return picture, types.replace("</Types>", override)


@pytest.mark.parametrize("decoy", ["[CONTENT_TYPES].XML", "[Content_Types].xml"])
def test_a_second_part_of_the_same_name_fails_the_document(decoy: str) -> None:
    # N2 (probe pptx_types): the check read the first of two content types
    # parts, python-pptx the last — typed XML there, the picture went uncapped.
    raw, _ = _deck()
    _, decoyed = _decoy_types(raw)
    out = io.BytesIO()
    with warnings.catch_warnings():  # zipfile warns about the duplicate name
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(out, "w") as package:
            package.writestr(decoy, decoyed)
            for item in source.infolist():
                package.writestr(item, source.read(item))
    with pytest.raises(DocumentParseError, match="same name"):
        bound_office_package(out.getvalue(), ".pptx")


def test_content_types_are_read_by_their_exact_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # Under another case python-pptx does not read it, so neither does the check.
    monkeypatch.setattr(office_package, "MAX_IMAGE_BYTES", len(_BIG) - 1)
    raw, _ = _deck()
    _, decoyed = _decoy_types(raw)
    renamed = _rebuilt(
        raw,
        renames={"[Content_Types].xml": "[CONTENT_TYPES].XML"},
        rewrite={"[Content_Types].xml": lambda _: decoyed},
    )
    assert bound_office_package(renamed, ".pptx")[1] == 1


def test_a_directory_larger_than_its_entries_need_is_refused_before_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # N5 (probe eocd): the end record's count said 1; zipfile lists whatever
    # the declared directory size covers.
    monkeypatch.setattr(office_package, "MAX_PACKAGE_ENTRIES", 100)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as package:
        for number in range(2_000):
            package.writestr(f"{number:x}", b"")
    spoofed = bytearray(out.getvalue())
    at = spoofed.rfind(b"PK\x05\x06")
    spoofed[at + 8 : at + 12] = (1).to_bytes(2, "little") * 2
    raw = bytes(spoofed)
    assert declared_entry_count(raw) == 1

    def no_listing(*args: object, **kwargs: object) -> object:
        raise AssertionError("the entry list was built")

    monkeypatch.setattr(office_package.zipfile, "ZipFile", no_listing)
    with pytest.raises(DocumentParseError, match="parts"):
        bound_office_package(raw, ".docx")


def test_real_packages_stay_well_inside_the_directory_budget() -> None:
    for raw in (picture_pptx(_BIG, title="Deck"), picture_docx(_BIG, before="Intro")):
        declared = declared_directory(raw)
        assert declared is not None
        entries, size = declared
        assert size / entries < office_package.DIRECTORY_BYTES_PER_ENTRY / 2


@pytest.mark.parametrize(
    ("raw", "words"),
    [
        (b"<html><body><h1>Title</h1><p>Hello HTML words</p></body></html>", "Hello HTML words"),
        (b"Just plain words in a file named like Word.", "Just plain words"),
    ],
)
def test_a_file_merely_named_docx_is_parsed_as_before(raw: bytes, words: str) -> None:
    # N6: only a zip goes to the Word converter, the rest to MarkItDown.
    assert words in parse_document("a.docx", raw)
    assert words in parse_office_figures(raw, ".docx").markdown


def test_a_zip_behind_a_prefix_still_goes_to_the_capped_converter() -> None:
    # zipfile (and so any Word converter) opens it all the same.
    raw = b"prefix " + picture_docx(_BIG, before="Intro")
    assert find_data_uri_pictures(office_markdown(raw, ".docx")) == []
    assert len(parse_office_figures(raw, ".docx").figures.candidates) == 1


def test_a_linked_picture_is_never_opened(tmp_path: Any) -> None:
    # N7: mammoth's external_file_access is pinned off.
    linked = tmp_path / "linked.png"
    linked.write_bytes(_BIG)
    raw = _rebuilt(
        _word(
            "word/media/image1.png",
            f'Id="rId2" Type="{_R}/image" Target="{linked.as_uri()}" TargetMode="External"',
        ),
        rewrite={"word/document.xml": lambda x: x.replace('r:embed="rId2"', 'r:link="rId2"')},
    )
    parsed = parse_office_figures(raw, ".docx")
    assert (parsed.figures.candidates, parsed.figures.unsupported) == ((), 1)


def test_the_markitdown_internals_reused_are_still_there() -> None:
    # N7: office_docx reuses MarkItDown's private math pre-processing (its
    # import fails at module load) and the Word converter's HTML converter.
    from markitdown.converter_utils.docx import pre_process
    from markitdown.converters import DocxConverter

    assert callable(pre_process._pre_process_math)
    assert callable(DocxConverter()._html_converter.convert_string)
