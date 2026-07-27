"""Put this directory on ``sys.path`` so the tests can ``import entry_latency``.

``tools/bench`` is a dev tool, not an installed workspace package — the
script lives next to its tests. Same shape as ``tools/eval/conftest.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
