from __future__ import annotations

import os
import sys
from pathlib import Path


def configure_pycache() -> None:
    """Redirect Python bytecode cache to a single project-level directory."""
    if sys.dont_write_bytecode:
        return

    root = Path(__file__).resolve().parent.parent
    configured_prefix = os.environ.get("PYTHONPYCACHEPREFIX")
    pycache_prefix = Path(configured_prefix) if configured_prefix else root / ".pycache"
    pycache_prefix = pycache_prefix.expanduser().resolve()
    pycache_prefix.mkdir(parents=True, exist_ok=True)

    prefix_text = os.fspath(pycache_prefix)
    os.environ["PYTHONPYCACHEPREFIX"] = prefix_text
    sys.pycache_prefix = prefix_text
