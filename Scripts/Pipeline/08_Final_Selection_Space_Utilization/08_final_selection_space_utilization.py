"""Stage 8 final selection for the space-utilization Stage 6 variant."""

import importlib.util
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree, suffixed_path

BASELINE_SCRIPT = PIPELINE_ROOT / "08_Final_Selection" / "08_final_selection.py"
VARIANT_STAGE6_OUTPUT_DIR = common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization"
VARIANT_STAGE7_OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Space_Utilization"
VARIANT_STAGE8_OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Space_Utilization"


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location("stage8_baseline", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 8 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
        stage8.LAYOUT_SUMMARY_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv", "SU")
        stage8.LAYOUT_BY_COLUMN_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv", "SU")
        stage8.LAYOUT_BY_LOCATION_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Location.csv", "SU")
        stage8.LAYOUT_BY_RACK_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_By_Rack.csv", "SU")
        stage8.ROBUSTNESS_SUMMARY_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv", "SU")
        rows = stage8.build_final_selection()
        suffix_output_tree(VARIANT_STAGE8_OUTPUT_DIR, "SU")
    finally:
        common.STAGE6_OUTPUT_DIR = original_stage6_output_dir
        common.STAGE7_OUTPUT_DIR = original_stage7_output_dir
        common.STAGE8_OUTPUT_DIR = original_stage8_output_dir
    print(
        "Space-utilization Stage 8 complete. "
        f"Candidate rows: {len(rows)}. Output: {VARIANT_STAGE8_OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
