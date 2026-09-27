"""Pack platform-skills/<name>/ into deterministic <name>.skill ZIPs.

Each skill directory holds SKILL.md, optional scripts/, optional skill.yaml.
skill.yaml has exactly one key, ``shared``: files copied from
platform-skills/shared/ into the package's scripts/. Nothing is shared unless
declared (spec D6). Output is byte-identical for identical sources, so the
platform's content_hash idempotency sees "unchanged" as unchanged.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import yaml

_EXCLUDED_DIRS = {"shared", "tests", "dist"}
_FIXED_DATE = (2026, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


def _skill_dirs(root: Path) -> list[Path]:
    return sorted(
        d
        for d in root.iterdir()
        if d.is_dir()
        and d.name not in _EXCLUDED_DIRS
        and not d.name.startswith((".", "_"))
        and (d / "SKILL.md").is_file()
    )


def _read_shared_list(skill_dir: Path) -> list[str]:
    path = skill_dir / "skill.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise BuildError(f"{path}: must be a mapping")
    unknown = set(data) - {"shared"}
    if unknown:
        raise BuildError(f"{path}: unknown keys {sorted(unknown)}")
    shared = data.get("shared") or []
    if not isinstance(shared, list) or not all(isinstance(s, str) for s in shared):
        raise BuildError(f"{path}: 'shared' must be a list of file names")
    return shared


def _package_files(skill_dir: Path, shared_dir: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for p in sorted(skill_dir.rglob("*")):
        rel = p.relative_to(skill_dir)
        if not p.is_file() or p.name == "skill.yaml":
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue
        files[rel.as_posix()] = p.read_bytes()
    for name in _read_shared_list(skill_dir):
        src = shared_dir / name
        if not src.is_file():
            raise BuildError(
                f"{skill_dir.name}: declared shared file {name!r} not found in {shared_dir}"
            )
        dest = f"scripts/{name}"
        if dest in files:
            raise BuildError(f"{skill_dir.name}: own {dest!r} would be shadowed by shared {name!r}")
        files[dest] = src.read_bytes()
    return files


def build_one(skill_dir: Path, shared_dir: Path, out: Path) -> Path:
    files = _package_files(skill_dir, shared_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"{skill_dir.name}.skill"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):
            info = zipfile.ZipInfo(rel, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[rel])
    return target


def build_all(root: Path, out: Path, only: set[str] | None = None) -> list[Path]:
    shared_dir = root / "shared"
    dirs = [d for d in _skill_dirs(root) if only is None or d.name in only]
    return [build_one(d, shared_dir, out) for d in dirs]


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=here)
    ap.add_argument("--out", type=Path, default=here / "dist")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    only = {s for s in args.only.split(",") if s} or None
    try:
        pkgs = build_all(args.root, args.out, only)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    for pkg in pkgs:
        with zipfile.ZipFile(pkg) as z:
            print(f"{pkg}  {pkg.stat().st_size} bytes")
            for info in z.infolist():
                print(f"    {info.filename}  {info.file_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
