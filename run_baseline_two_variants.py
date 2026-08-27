import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["PIPELINE_INCLUDE_HEURISTICS"] = "1"
os.environ["PIPELINE_HEURISTIC_LABEL_FILTER"] = "Baseline_None"

# Run the normal pipeline entrypoint, but restricted to the two baseline variants.
runpy.run_path(str(ROOT / "Scripts" / "Pipeline" / "run_ordered_pipeline.py"), run_name="__main__")
