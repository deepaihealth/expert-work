"""Bake a CJK-capable default into matplotlib's system matplotlibrc (build-time).

Run once during the office image build (OFFICE-ADR-3). It rewrites the rc
shipped in matplotlib's mpl-data so that *every* agent's matplotlib code
renders Chinese without per-call font configuration — important because the
runtime rootfs is read-only and every writable path (HOME=/home/agent,
/workspace) is wiped/rebuilt each session, so a user-level rc would not
persist.

Debian's ``fonts-noto-cjk`` ships ``NotoSansCJK-Regular.ttc`` (a collection);
matplotlib registers only its first face, whose family name is
"Noto Sans CJK JP". That face is part of the unified Noto Sans CJK typeface
and contains the full Han set, so it renders Simplified Chinese correctly —
we list several CJK family names so a matching one resolves regardless of
which face matplotlib happens to register.
"""

from __future__ import annotations

import pathlib

import matplotlib

_KEYS_TO_REPLACE = ("font.family", "font.sans-serif", "axes.unicode_minus")
# Only ``key: value`` lines — matplotlib's rc parser warns on free-form
# comment lines, which would spam every agent chart's stderr (OFFICE-ADR-3).
_APPENDED = (
    "\nfont.family: sans-serif\n"
    "font.sans-serif: Noto Sans CJK JP, Noto Sans CJK SC, WenQuanYi Zen Hei, DejaVu Sans\n"
    "axes.unicode_minus: False\n"
)


def main() -> None:
    rc_path = pathlib.Path(matplotlib.get_data_path()) / "matplotlibrc"
    kept = [
        line
        for line in rc_path.read_text(encoding="utf-8").splitlines()
        # Match on the key boundary (key before the first colon), not a bare
        # prefix, so e.g. a future "font.familyfoo" key is not mis-dropped.
        if line.lstrip("#").split(":", 1)[0].strip() not in _KEYS_TO_REPLACE
    ]
    rc_path.write_text("\n".join(kept) + _APPENDED, encoding="utf-8")
    print(f"patched {rc_path}")


if __name__ == "__main__":
    main()
