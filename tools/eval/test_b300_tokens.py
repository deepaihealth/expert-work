"""B-300 token 计数脚本:取出设计里的 ```text 块、按标题标注;计数器用假的(不下载词表)。"""

from __future__ import annotations

from pathlib import Path

import b300_tokens as tokens
import pytest

SAMPLE = """# Title
## 3. Language
```text
## Output language
Write in {language}.
```
### 4.2 Prompts
```text
first
```
```python
not counted
```
```text
second
```
"""


def test_text_blocks_and_headings() -> None:
    blocks = tokens.text_blocks(SAMPLE)
    assert [(b.heading, b.text) for b in blocks] == [
        ("3. Language", "## Output language\nWrite in {language}."),
        ("4.2 Prompts", "first"),
        ("4.2 Prompts", "second"),
    ]
    rows = tokens.count(blocks, len)
    assert [r.label for r in rows] == ["3. Language #1", "4.2 Prompts #1", "4.2 Prompts #2"]
    assert rows[1].chars == rows[1].tokens == 5


def test_design_document_blocks_are_found() -> None:
    blocks = tokens.text_blocks(tokens.DESIGN.read_text(encoding="utf-8"))
    headings = {b.heading for b in blocks}
    assert any(h.startswith("3.") for h in headings)  # 语言一节
    assert any(h.startswith("4.2") for h in headings)  # 两段提炼指令
    assert any(h.startswith("5.1") for h in headings)  # 召回块与提醒节
    assert any("Before relying on a memory" in b.text for b in blocks)


def test_main_with_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "a.txt"
    path.write_text("hello world", encoding="utf-8")
    assert tokens.main(["--file", str(path)], counter=lambda t: 2) == 0
    assert '"tokens": 2' in capsys.readouterr().out
