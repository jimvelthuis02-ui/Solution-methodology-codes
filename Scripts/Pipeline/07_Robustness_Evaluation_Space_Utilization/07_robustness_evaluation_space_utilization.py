"""Stage 7 robustness evaluation for the space-utilization Stage 6 variant."""

import importlib.util
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree, suffixed_path

BASELINE_SCRIPT = PIPELINE_ROOT / "07_Robustness_Evaluation" / "07_robustness_evaluation.py"
VARIANT_STAGE6_OUTPUT_DIR = common.OUTPUT_ROOT / "06_Layout_Generation_Space_Utilization"
VARIANT_STAGE7_OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Space_Utilization"


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location("stage7_baseline", BASELINE_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load baseline Stage 7 module from {BASELINE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    original_stage6_output_dir = common.STAGE6_OUTPUT_DIR
    original_stage7_output_dir = common.STAGE7_OUTPUT_DIR
    try:
        common.STAGE6_OUTPUT_DIR = VARIANT_STAGE6_OUTPUT_DIR
        common.STAGE7_OUTPUT_DIR = VARIANT_STAGE7_OUTPUT_DIR
        VARIANT_STAGE7_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        stage7 = _load_baseline_module()
        stage7.LAYOUT_SUMMARY_FILE = suffixed_path(VARIANT_STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv", "SU")
        stage7.ROBUSTNESS_SUMMARY_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv", "SU")
        stage7.ROBUSTNESS_DETAILS_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Details.csv", "SU")
        stage7.NON_FEASIBLE_OUTPUT_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Non_Feasible_Layouts.csv", "SU")
        stage7.NON_ROBUST_OUTPUT_FILE = suffixed_path(VARIANT_STAGE7_OUTPUT_DIR / "Non_Robust_Layouts.csv", "SU")
        rows = stage7.build_robustness_evaluation()
        suffix_output_tree(VARIANT_STAGE7_OUTPUT_DIR, "SU")
    finally:
        common.STAGE6_OUTPUT_DIR = original_stage6_output_dir
        common.STAGE7_OUTPUT_DIR = original_stage7_output_dir
    print(
        "Space-utilization Stage 7 complete. "
        f"Summary rows: {len(rows)}. Output: {VARIANT_STAGE7_OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
