import importlib.util
from pathlib import Path
import sys
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


def _load_base_stage7_module():
    stage7_path = Path(__file__).resolve().parent / "07_robustness_evaluation.py"
    spec = importlib.util.spec_from_file_location("stage7_base_module", stage7_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Stage 7 module from {stage7_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_robustness_evaluation_greedy_anchor() -> Path:
    stage7: Any = _load_base_stage7_module()

    output_dir = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Greedy"
    output_dir.mkdir(parents=True, exist_ok=True)

    stage7.LAYOUT_SUMMARY_FILE = (
        common.OUTPUT_ROOT
        / "06_Layout_Generation_Greedy"
        / "Candidate_Layout_Summary_TopFilled_greedy.csv"
    )
    stage7.ROBUSTNESS_SUMMARY_FILE = output_dir / "Candidate_Layout_Robustness_Summary_greedy.csv"

    stage7.build_robustness_evaluation()
    return stage7.ROBUSTNESS_SUMMARY_FILE


if __name__ == "__main__":
    output_path = build_robustness_evaluation_greedy_anchor()
    print(f"Greedy Stage 7 variant complete. Output written to: {output_path}")
