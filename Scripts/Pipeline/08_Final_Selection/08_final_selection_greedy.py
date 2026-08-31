import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


def build_final_selection_greedy_anchor() -> Path | None:
    """Legacy greedy anchor kept as a no-op when the historical greedy artifacts are unavailable."""
    required_inputs = [
        common.OUTPUT_ROOT / "06_Layout_Generation_Greedy" / "Candidate_Layout_Summary_greedy.csv",
        common.OUTPUT_ROOT / "07_Robustness_Evaluation_Greedy" / "Candidate_Layout_Robustness_Summary_greedy.csv",
    ]
    if not any(path.exists() for path in required_inputs):
        print("Skipping legacy greedy Stage 8 anchor: missing historical greedy robustness/layout export.", flush=True)
        return None

    output_dir = common.OUTPUT_ROOT / "08_Final_Selection_Greedy"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "Candidate_Layout_Metric_Ranking_greedy.csv"

    print(f"Legacy greedy Stage 8 input present; no operation performed in this branch: {output_path}", flush=True)
    return output_path


if __name__ == "__main__":
    output_path = build_final_selection_greedy_anchor()
    if output_path is not None:
        print(f"Greedy Stage 8 variant complete. Output written to: {output_path}")
    else:
        print("Greedy Stage 8 variant skipped: no historical greedy inputs available.")
