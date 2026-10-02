"""Stage 7 robustness evaluation for the picking-efficiency Stage 6 variant."""

import importlib.util
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common

BASELINE_SCRIPT = PIPELINE_ROOT / "07_Robustness_Evaluation" / "07_robustness_evaluation.py"
VARIANT_STAGE6_OUTPUT_DIR = common.OUTPUT_ROOT / "06_Layout_Generation_Picking_Efficiency"
VARIANT_STAGE7_OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Picking_Efficiency"


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location("stage7_baseline_picking", BASELINE_SCRIPT)
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
        rows = stage7.build_robustness_evaluation()
    finally:
        common.STAGE6_OUTPUT_DIR = original_stage6_output_dir
        common.STAGE7_OUTPUT_DIR = original_stage7_output_dir
    print(
        "Picking-efficiency Stage 7 complete. "
        f"Summary rows: {len(rows)}. Output: {VARIANT_STAGE7_OUTPUT_DIR}"
    )


if __name__ == "__main__":
    main()
