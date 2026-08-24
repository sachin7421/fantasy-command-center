#!/usr/bin/env python
"""Entry point: python fcc.py <command>  (or ./fcc on POSIX)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
