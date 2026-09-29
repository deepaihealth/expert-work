import copy
import json
import sys
from pathlib import Path

import pytest

_SKILL_DIR = Path(__file__).resolve().parents[2] / "health-plan-report"
_SCRIPTS = _SKILL_DIR / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@pytest.fixture
def sample() -> dict:
    path = _SKILL_DIR / "sample" / "sample-plan.json"
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
