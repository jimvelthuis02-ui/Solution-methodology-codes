"""Root runner for Stage 3: Model Implementation."""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> int:
    script_path = Path(__file__).resolve().parent / "Scripts" / "3_Model_Implementation" / "stage_main.py"
    runpy.run_path(str(script_path), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
