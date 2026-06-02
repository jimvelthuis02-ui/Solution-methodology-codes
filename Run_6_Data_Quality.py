"""Root runner for Stage 6: Data Quality."""

from __future__ import annotations

import runpy
from pathlib import Path


def main() -> int:
    script_path = Path(__file__).resolve().parent / "Scripts" / "6_Data_Quality" / "stage_main.py"
    runpy.run_path(str(script_path), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
