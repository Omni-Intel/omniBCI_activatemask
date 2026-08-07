# -*- coding: utf-8 -*-
"""Backward-compatible launcher for the src-layout OmniBCI application."""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from omnibci_app.main import main


if __name__ == "__main__":
    raise SystemExit(main())
