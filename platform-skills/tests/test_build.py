import hashlib
import importlib.util
import re
import zipfile
from pathlib import Path

import pytest

_BUILD = Path(__file__).resolve().parents[1] / "build.py"
_spec = importlib.util.spec_from_file_location("ps_build", _BUILD)
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _skill(
    root: Path, name: str, *, yaml_text: str = "", scripts: dict[str, str] | None = None
) -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试\nexpert_work:\n  lazy: true\n---\n正文\n",
        encoding="utf-8",
    )
    if yaml_text:
        (d / "skill.yaml").write_text(yaml_text, encoding="utf-8")
    for rel, body in (scripts or {}).items():
        (d / "scripts" / rel).write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "ps"
    (r / "shared").mkdir(parents=True)
    (r / "shared" / "preview.py").write_text("print('p')\n", encoding="utf-8")
    (r / "shared" / "convert.py").write_text("print('c')\n", encoding="utf-8")
    return r


def _names(pkg: Path) -> list[str]:
    with zipfile.ZipFile(pkg) as z:
        return sorted(z.namelist())


def test_declared_shared_files_are_copied_and_undeclared_are_not(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - preview.py\n", scripts={"own.py": "x=1\n"})
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py", "scripts/preview.py"]


def test_no_skill_yaml_means_no_shared_files(root, tmp_path):
    _skill(root, "beta", scripts={"own.py": "x=1\n"})
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py"]


def test_unknown_shared_file_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - nope.py\n")
    with pytest.raises(build.BuildError, match=re.escape("nope.py")):
        build.build_all(root, tmp_path / "out")


def test_unknown_yaml_key_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared: []\nextra: 1\n")
    with pytest.raises(build.BuildError, match="extra"):
        build.build_all(root, tmp_path / "out")


def test_own_script_shadowing_shared_fails(root, tmp_path):
    _skill(root, "alpha", yaml_text="shared:\n  - preview.py\n", scripts={"preview.py": "x=1\n"})
    with pytest.raises(build.BuildError, match=re.escape("preview.py")):
        build.build_all(root, tmp_path / "out")


def test_build_is_byte_identical_across_runs(root, tmp_path):
    _skill(
        root,
        "alpha",
        yaml_text="shared:\n  - preview.py\n  - convert.py\n",
        scripts={"own.py": "x=1\n"},
    )
    [a] = build.build_all(root, tmp_path / "o1")
    (root / "alpha" / "scripts" / "own.py").touch()  # mtime changes must not matter
    [b] = build.build_all(root, tmp_path / "o2")
    assert hashlib.sha256(a.read_bytes()).digest() == hashlib.sha256(b.read_bytes()).digest()


def test_only_filter(root, tmp_path):
    _skill(root, "alpha")
    _skill(root, "beta")
    pkgs = build.build_all(root, tmp_path / "out", only={"beta"})
    assert [p.name for p in pkgs] == ["beta.skill"]


def test_pycache_and_hidden_files_are_excluded(root, tmp_path):
    d = _skill(root, "alpha", scripts={"own.py": "x=1\n"})
    (d / "scripts" / "__pycache__").mkdir()
    (d / "scripts" / "__pycache__" / "own.cpython-312.pyc").write_bytes(b"\0")
    (d / ".DS_Store").write_bytes(b"\0")
    [pkg] = build.build_all(root, tmp_path / "out")
    assert _names(pkg) == ["SKILL.md", "scripts/own.py"]
