"""Full dead-link check for the docs-site build: absolute /docs/ links, same-page #anchors,
AND relative ./x.html#anchor links. Run from the docs-site dir after ``pnpm build``:

    python3 scripts/check_links.py

Exit 1 when any link is dead.
"""
import os
import re
import sys
import urllib.parse
from posixpath import normpath

DIST = ".vitepress/dist"
pages: dict[str, set[str]] = {}
files: dict[str, str] = {}
for root, _, names in os.walk(DIST):
    for n in names:
        if not n.endswith(".html"):
            continue
        fp = os.path.join(root, n)
        html = open(fp, encoding="utf-8").read()
        rel = os.path.relpath(fp, DIST)  # e.g. guide/chat.html
        url = "/docs/" + rel
        pages[url] = {urllib.parse.unquote(i) for i in re.findall(r'\sid="([^"]+)"', html)}
        files[url] = html


def canon(path: str) -> str:
    """Normalize any page path to the '/docs/<rel>.html' key."""
    if path.endswith("/"):
        path += "index.html"
    if not path.endswith(".html"):
        path += ".html"
    return normpath(path)


SKIP = re.compile(r"^(https?:|mailto:|javascript:)|^/docs/assets/|\.(css|js|woff2?|png|svg|ico|json)$")
bad: list[tuple[str, str, str]] = []
total = 0
for url, html in files.items():
    base_dir = os.path.dirname(url)  # /docs/guide
    for href in re.findall(r'href="([^"]*)"', html):
        if SKIP.search(href.split("#")[0]) and not href.startswith("#"):
            continue
        if not (href.startswith("/docs/") or href.startswith("#") or href.startswith("./") or href.startswith("../")):
            continue
        total += 1
        path, _, frag = href.partition("#")
        frag = urllib.parse.unquote(frag)
        if not path:  # same-page
            target = url
        elif path.startswith("/docs/"):
            target = canon(path)
        else:  # relative
            target = canon(normpath(os.path.join(base_dir, path)))
        if target not in pages:
            bad.append((url, href, "PAGE"))
            continue
        if frag and frag not in pages[target]:
            bad.append((url, href, "锚点"))

print(f"{total} 条站内链接(含相对) / {len(pages)} 页")
seen = set()
for s, h, w in bad:
    key = (s, h)
    if key in seen:
        continue
    seen.add(key)
    print(f"  ❌ [{w}] {h} ← {s}")
print("✅ 无死链" if not bad else f"{len(seen)} 条死链(去重)")
sys.exit(1 if bad else 0)
