"""Stage 8 final selection for the beam-preservation Stage 6 variant."""

import importlib.util
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common

BASELINE_SCRIPT = PIPELINE_ROOT / "08_Final_Selection" / "08_final_selection.py"
VARIANT_STAGE6_OUTPUT_DIR = common.OUTPUT_ROOT / "06_Layout_Generation_Beam_Preservation"
VARIANT_STAGE7_OUTPUT_DIR = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Beam_Preservation"
VARIANT_STAGE8_OUTPUT_DIR = common.OUTPUT_ROOT / "08_Final_Selection_Beam_Preservation"


def _load_baseline_module():
    spec = importlib.util.spec_from_file_location("stage8_baseline_beam", BASELINE_SCRIPT)
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
        rows = stage8.build_final_selection()
    finally:
        common.STAGE6_OUTPUT_DIR = original_stage6_output_dir
        common.STAGE7_OUTPUT_DIR = original_stage7_output_dir
        common.STAGE8_OUTPUT_DIR = original_stage8_output_dir
    print(f"Beam-preservation Stage 8 complete. Candidate rows: {len(rows)}.")


if __name__ == "__main__":
    main()
