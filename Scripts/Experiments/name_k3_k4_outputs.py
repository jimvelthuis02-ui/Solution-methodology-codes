from __future__ import annotations

import csv
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ROOT = ROOT / "Output" / "06_Layout_Generation_K3_K4_Experiment" / "strict_k3_k4_sweep"
OUTPUT_EXTENSIONS = {".csv", ".png"}


def _is_separate_k_output(path: Path) -> bool:
    return path.stem.endswith(("_K3", "_K4"))


def _combined_path(path: Path) -> Path:
    if path.stem.endswith("_K3_K4"):
        return path
    return path.with_name(f"{path.stem}_K3_K4{path.suffix}")


def _update_figure_index(path: Path) -> None:
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    for row in rows:
        figure = str(row.get("Figure", "")).strip()
        if figure:
            figure_path = Path(figure)
            if not figure_path.stem.endswith("_K3_K4"):
                row["Figure"] = f"{figure_path.stem}_K3_K4{figure_path.suffix}"
        source_text = str(row.get("Source", ""))
        for filename in source_text.split(";"):
            token = filename.strip()
            if token and Path(token).suffix.lower() in OUTPUT_EXTENSIONS:
                suffix = Path(token).suffix
                stem = Path(token).stem
                if not stem.endswith("_K3_K4"):
                    source_text = source_text.replace(token, f"{stem}_K3_K4{suffix}")
        row["Source"] = source_text
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    if not EXPERIMENT_ROOT.exists():
        print(f"Strict K3/K4 output folder not found: {EXPERIMENT_ROOT}")
        return 2

    removed = 0
    renamed = 0
    candidates = sorted(
        path for path in EXPERIMENT_ROOT.rglob("*")
        if path.is_file() and path.suffix.lower() in OUTPUT_EXTENSIONS
    )

    for path in candidates:
        if _is_separate_k_output(path):
            path.unlink()
            removed += 1

    for path in candidates:
        if not path.exists() or _is_separate_k_output(path) or path.stem.endswith("_K3_K4"):
            continue
        destination = _combined_path(path)
        os.replace(path, destination)
        renamed += 1

    for path in EXPERIMENT_ROOT.rglob("Figure_Index_K3_K4.csv"):
        _update_figure_index(path)

    print(f"Removed {removed} per-K split files.")
    print(f"Renamed {renamed} combined files with the _K3_K4 suffix.")
    print(f"Output folder: {EXPERIMENT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())