"""Stage 8 final selection for the picking-efficiency Stage 6 variant."""

import csv
import importlib.util
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree, suffixed_path

BASELINE_SCRIPT = PIPELINE_ROOT / "08_Final_Selection" / "08_final_selection.py"
VARIANT_STAGE6_OUTPUT_DIR = common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency"
VARIANT_STAGE7_OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Picking_Efficiency"
VARIANT_STAGE8_OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Picking_Efficiency"


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location("stage8_baseline_picking", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 8 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_picking_metrics_to_output(path: Path, picking_by_config: dict[str, dict[str, str]]) -> None:
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        rows = list(csv.DictReader(source))
        fields = list(rows[0].keys()) if rows else []
    metric_fields = [
        "Picking_Covered_Item_Count",
        "Picking_Covered_Pick_Count",
        "Picking_Uncovered_Item_Count",
        "Picking_Uncovered_Pick_Count",
        "Picking_Score",
    ]
    for field in metric_fields:
        if field not in fields:
            fields.append(field)
    for row in rows:
        metrics = picking_by_config.get(str(row.get("Config_ID", "")).strip(), {})
        for field in metric_fields:
            row[field] = metrics.get(field, "")
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    original_stage6_output_dir = common.STAGE6_OUTPUT_DIR
    original_stage7_output_dir = common.STAGE7_OUTPUT_DIR
    original_stage8_output_dir = common.STAGE8_OUTPUT_DIR
    try:
        common.STAGE6_OUTPUT_DIR = VARIANT_STAGE6_OUTPUT_DIR
        common.STAGE7_OUTPUT_DIR = VARIANT_STAGE7_OUTPUT_DIR
        common.STAGE8_OUTPUT_DIR = VARIANT_STAGE8_OUTPUT_DIR
        VARIANT_STAGE8_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stage8 = _load_baseline_module()
        stage8.LAYOUT_SUMMARY_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv", "PE")
        stage8.LAYOUT_BY_COLUMN_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv", "PE")
        stage8.LAYOUT_BY_LOCATION_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Location.csv", "PE")
        stage8.LAYOUT_BY_RACK_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack.csv", "PE")
        stage8.ROBUSTNESS_SUMMARY_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv", "PE")
        rows = stage8.build_final_selection()
        picking_summary_path = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv", "PE")
        picking_summary = common._read_csv(picking_summary_path)
        picking_by_config = {
            str(row.get("Config_ID", "")).strip(): row
            for row in picking_summary
        }
        _add_picking_metrics_to_output(
            VARIANT_STAGE8_OUTPUT_DIR / "Candidate_Layout_Metric_Ranking.csv",
            picking_by_config,
        )
        _add_picking_metrics_to_output(
            VARIANT_STAGE8_OUTPUT_DIR / "Weighted_Sum_Method_Ranking.csv",
            picking_by_config,
        )
        suffix_output_tree(VARIANT_STAGE8_OUTPUT_DIR, "PE")
    finally:
        common.STAGE6_OUTPUT_DIR = original_stage6_output_dir
        common.STAGE7_OUTPUT_DIR = original_stage7_output_dir
        common.STAGE8_OUTPUT_DIR = original_stage8_output_dir
    print(
        "Picking-efficiency Stage 8 complete. "
        f"Candidate rows: {len(rows)}. Output: {VARIANT_STAGE8_OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
