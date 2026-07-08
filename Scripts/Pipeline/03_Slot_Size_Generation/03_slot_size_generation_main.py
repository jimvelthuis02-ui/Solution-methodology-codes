import runpy
import csv
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR.parents[2] / "Output" / "03_Slot_Size_Generation"
METHOD_SCRIPTS = [
    "03a_quantile_model.py",
    "03b_hierarchical_model.py",
    "03c_kmeans_model.py",
]
SUMMARY_FILES = [
    "Quantile_Slot_Size_Configuration_Summary.csv",
    "Hierarchical_Slot_Size_Configuration_Summary.csv",
    "KMeans_Slot_Size_Configuration_Summary.csv",
]


def _build_merged_summary() -> Path:
    """Merge all Stage 3 method summaries into one consolidated summary CSV."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    merged_file = OUTPUT_DIR / "Stage3_Slot_Size_Configuration_Summary_All.csv"

    merged_rows: list[dict[str, str]] = []
    fieldnames: list[str] = []
    for filename in SUMMARY_FILES:
        path = OUTPUT_DIR / filename
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                continue
            if not fieldnames:
                fieldnames = list(reader.fieldnames)
            merged_rows.extend(list(reader))

    with merged_file.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(merged_rows)

    # Keep only the consolidated summary in Stage 3 output.
    for filename in SUMMARY_FILES:
        path = OUTPUT_DIR / filename
        if path.exists():
            path.unlink()

    return merged_file


def run_slot_size_generation() -> None:
    """Run each Stage 3 slot-size method script in sequence."""
    for script in METHOD_SCRIPTS:
        script_path = SCRIPT_DIR / script
        print(f"Running slot-size method model: {script}")
        runpy.run_path(str(script_path), run_name="__main__")

    merged_summary = _build_merged_summary()
    print(f"Merged Stage 3 summary written to: {merged_summary}")


if __name__ == "__main__":
    # Stage 3 entrypoint: execute all method variants (quantile, hierarchical, k-means).
    run_slot_size_generation()
    print("Slot-size method generation complete.")
