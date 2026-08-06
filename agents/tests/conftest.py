"""Test setup: everything here runs offline.

No test may call Anthropic or OpenRouter. Model clients are stubbed and the
platform is a local mock WebSocket server.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"

for path in (str(REPO_ROOT), str(AGENTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)
