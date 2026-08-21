import importlib.util
from pathlib import Path
import sys
from typing import Any

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


def _load_base_stage8_module():
    stage8_path = Path(__file__).resolve().parent / "08_final_selection.py"
    spec = importlib.util.spec_from_file_location("stage8_base_module", stage8_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load Stage 8 module from {stage8_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_final_selection_greedy_anchor() -> Path:
    stage8: Any = _load_base_stage8_module()

    stage6_dir = common.OUTPUT_ROOT / "06_Layout_Generation_Greedy"
    stage7_dir = common.OUTPUT_ROOT / "07_Robustness_Evaluation_Greedy"
    output_dir = common.OUTPUT_ROOT / "08_Final_Selection_Greedy"
    output_dir.mkdir(parents=True, exist_ok=True)

    stage8.ROBUSTNESS_SUMMARY_FILE = stage7_dir / "Candidate_Layout_Robustness_Summary_greedy.csv"
    stage8.LAYOUT_SUMMARY_FILE = stage6_dir / "Candidate_Layout_Summary_greedy.csv"
    stage8.LAYOUT_BY_COLUMN_FILE = stage6_dir / "Candidate_Layout_By_Rack_Column_greedy.csv"
    stage8.LAYOUT_BY_LOCATION_FILE = stage6_dir / "Candidate_Layout_By_Location_greedy.csv"
    stage8.OUTPUT_FILE = output_dir / "Candidate_Layout_Metric_Ranking_greedy.csv"
    stage8.FINAL_LAYOUT_BY_COLUMN_FILE = output_dir / "Final_Layout_By_Rack_Column_greedy.csv"
    stage8.FINAL_LAYOUT_BY_LOCATION_FILE = output_dir / "Final_Layout_By_Location_greedy.csv"
    stage8.FINAL_LAYOUT_BY_SEGMENT_FILE = output_dir / "Final_Layout_By_Segment_greedy.csv"
    stage8.LEGACY_OUTPUT_FILES = [
        output_dir / "Objective_Layout_Recommendations.csv",
        output_dir / "Management_Decision_Table.csv",
    ]

    stage8.build_final_selection()
    return stage8.OUTPUT_FILE


if __name__ == "__main__":
    output_path = build_final_selection_greedy_anchor()
    print(f"Greedy Stage 8 variant complete. Output written to: {output_path}")
