import csv
from pathlib import Path
import sys

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


BASELINE_FILE = common.OUTPUT_ROOT / "08_Final_Selection" / "Candidate_Layout_Metric_Ranking.csv"
GREEDY_FILE = common.OUTPUT_ROOT / "08_Final_Selection_Greedy" / "Candidate_Layout_Metric_Ranking_greedy.csv"
OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Comparison"
OUTPUT_FILE = OUTPUT_DIR / "Baseline_vs_Greedy_Metric_Comparison.csv"


METRIC_FIELDS = [
    "Assigned_Locations_Total",
    "Capacity_Margin",
    "Occupancy_Rate",
    "Beam_Relocations_Total",
    "Additional_Beams_Required",
    "Additional_Grids_Required",
    "Implementation_Effort_Total",
    "Standardization_Unique_Slot_Sizes",
    "Additional_Fill_Height_Total_cm",
    "Additional_Fill_Extra_Slot_Size_Variants",
]

RANK_FIELDS = [
    "Rank_Occupancy_Rate",
    "Rank_Beam_Relocations_Total",
    "Rank_Additional_Required_Beams",
    "Rank_Additional_Required_Grids",
    "Rank_Implementation_Effort_Total",
    "Rank_Standardization",
    "Rank_Additional_Fill_Height_Total_cm",
    "Rank_Additional_Fill_Extra_Slot_Size_Variants",
]

TEXT_FIELDS = [
    "Source_Slot_Sizes",
    "Additional_Fill_Extra_Slot_Sizes",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    return common._read_csv(path)


def _rows_by_config(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_csv(path)
    return {
        str(row.get("Config_ID", "")).strip(): row
        for row in rows
        if str(row.get("Config_ID", "")).strip()
    }


def _as_float(value: object | None) -> float:
    parsed = common._to_float(value)
    return 0.0 if parsed is None else float(parsed)


def _as_int(value: object | None) -> int:
    return common._to_int_default(value, 0)


def _safe_text(value: object | None) -> str:
    return common._encode_excel_text(common._decode_excel_text(value))


def build_comparison() -> Path:
    baseline = _rows_by_config(BASELINE_FILE)
    greedy = _rows_by_config(GREEDY_FILE)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_config_ids = sorted(set(baseline.keys()) | set(greedy.keys()))
    output_rows: list[dict[str, str]] = []
    for config_id in all_config_ids:
        base_row = baseline.get(config_id, {})
        greedy_row = greedy.get(config_id, {})

        merged = {
            "Config_ID": config_id,
            "Present_In_Baseline": "YES" if config_id in baseline else "NO",
            "Present_In_Greedy": "YES" if config_id in greedy else "NO",
            "Baseline_Source_Slot_Sizes": _safe_text(base_row.get("Source_Slot_Sizes", "")),
            "Greedy_Source_Slot_Sizes": _safe_text(greedy_row.get("Source_Slot_Sizes", "")),
            "Baseline_Additional_Fill_Extra_Slot_Sizes": _safe_text(base_row.get("Additional_Fill_Extra_Slot_Sizes", "")),
            "Greedy_Additional_Fill_Extra_Slot_Sizes": _safe_text(greedy_row.get("Additional_Fill_Extra_Slot_Sizes", "")),
        }

        for field in METRIC_FIELDS:
            base_value = _as_float(base_row.get(field))
            greedy_value = _as_float(greedy_row.get(field))
            merged[f"Baseline_{field}"] = str(base_row.get(field, ""))
            merged[f"Greedy_{field}"] = str(greedy_row.get(field, ""))
            merged[f"Delta_{field}"] = f"{(greedy_value - base_value):.6f}" if (config_id in baseline and config_id in greedy) else ""

        for field in RANK_FIELDS:
            base_rank = _as_int(base_row.get(field))
            greedy_rank = _as_int(greedy_row.get(field))
            merged[f"Baseline_{field}"] = str(base_row.get(field, ""))
            merged[f"Greedy_{field}"] = str(greedy_row.get(field, ""))
            merged[f"Delta_{field}"] = str(greedy_rank - base_rank) if (config_id in baseline and config_id in greedy) else ""

        output_rows.append(merged)

    fieldnames = [
        "Config_ID",
        "Present_In_Baseline",
        "Present_In_Greedy",
        "Baseline_Source_Slot_Sizes",
        "Greedy_Source_Slot_Sizes",
        "Baseline_Additional_Fill_Extra_Slot_Sizes",
        "Greedy_Additional_Fill_Extra_Slot_Sizes",
    ]
    for field in METRIC_FIELDS:
        fieldnames.extend([
            f"Baseline_{field}",
            f"Greedy_{field}",
            f"Delta_{field}",
        ])
    for field in RANK_FIELDS:
        fieldnames.extend([
            f"Baseline_{field}",
            f"Greedy_{field}",
            f"Delta_{field}",
        ])

    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(output_rows)

    return OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_comparison()
    print(f"Baseline vs greedy comparison written to: {output_path}")
