import importlib.util
import zipfile
from pathlib import Path

import pytest

PLATFORM_SKILLS = Path(__file__).resolve().parents[1]
EXPECTED_SKILLS = ("docx", "pptx", "xlsx", "pdf")


def _load_build():
    spec = importlib.util.spec_from_file_location("ps_build_conftest", PLATFORM_SKILLS / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def built_packages(tmp_path_factory) -> dict[str, Path]:
    out = tmp_path_factory.mktemp("dist")
    return {p.stem: p for p in _load_build().build_all(PLATFORM_SKILLS, out)}


@pytest.fixture(scope="session")
def unpacked_skills(built_packages, tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("skills")
    for name, pkg in built_packages.items():
        with zipfile.ZipFile(pkg) as z:
            z.extractall(root / name)
    return root
