import csv
import importlib.util
import os
import sys
from pathlib import Path

repo_root = Path(r"c:\Users\jimve\OneDrive\Documenten\Master IEM Year 2\Thesis Benchmark\Github\Solution-methodology-codes")
script_path = repo_root / "Scripts" / "Pipeline" / "06_Layout_Generation" / "06_layout_generation.py"

spec = importlib.util.spec_from_file_location("stage6", str(script_path))
mod = importlib.util.module_from_spec(spec)
sys.modules["stage6"] = mod
spec.loader.exec_module(mod)

configs = [
    "CFG_046",
    "CFG_056",
    "CFG_120",
    "CFG_078",
    "CFG_178",
    "CFG_010",
    "CFG_116",
    "CFG_080",
    "CFG_144",
    "CFG_103",
]

os.environ["PIPELINE_TARGET_CONFIGS"] = ",".join(configs)
rows, _, _ = mod.build_layout_generation()
summary_path = repo_root / "Output" / "06_Layout_Generation" / "Candidate_Layout_Summary.csv"
print("SUMMARY_PATH", summary_path)
print("ROWS_RETURNED", len(rows))
print("FEASIBLE_ROWS", sum(1 for row in rows if str(row.get("Layout_Feasible", "")).upper() == "YES"))
for row in rows:
    if str(row.get("Config_ID", "")).strip() in configs:
        print(
            row.get("Config_ID"),
            row.get("Layout_Feasible"),
            row.get("Runtime_Seconds"),
            row.get("Profile_Generation_Seconds"),
            row.get("Rack_Search_Seconds"),
        )

if summary_path.exists():
    with summary_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        filtered = [r for r in reader if r.get("Config_ID", "").strip() in configs]
        print("CSV_MATCHED_ROWS", len(filtered))
        for r in filtered:
            print(
                r.get("Config_ID"),
                r.get("Layout_Feasible"),
                r.get("Runtime_Seconds"),
                r.get("Profile_Generation_Seconds"),
                r.get("Rack_Search_Seconds"),
            )
