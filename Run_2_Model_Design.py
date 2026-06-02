"""Root runner for Stage 2: Model Design."""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> int:
    script_path = Path(__file__).resolve().parent / "Scripts" / "2_Model_Design" / "stage_main.py"
    runpy.run_path(str(script_path), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
