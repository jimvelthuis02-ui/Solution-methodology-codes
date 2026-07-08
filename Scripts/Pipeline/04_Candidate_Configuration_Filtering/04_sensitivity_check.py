import csv
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STAGE4_SCRIPT = ROOT / "Scripts" / "Pipeline" / "04_Candidate_Configuration_Filtering" / "04_candidate_configuration_filtering.py"
OUTPUT_FILE = ROOT / "Output" / "04_Candidate_Configuration_Filtering" / "Candidate_Configurations.csv"
SENSITIVITY_FILE = ROOT / "Output" / "04_Candidate_Configuration_Filtering" / "Sensitivity_Check_Summary.csv"


SCENARIOS = [
    {
        "Scenario_Name": "Baseline",
        "MAX_CANDIDATE_CONFIGURATIONS": 30,
        "NEAR_SLOT_TOLERANCE_CM": 5,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.05,
        "FAMILY_SLOT_TOLERANCE_CM": 10,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.15,
        "FAMILY_MEAN_TOLERANCE_CM": 10,
    },
    {
        "Scenario_Name": "Tighter_Family",
        "MAX_CANDIDATE_CONFIGURATIONS": 30,
        "NEAR_SLOT_TOLERANCE_CM": 5,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.05,
        "FAMILY_SLOT_TOLERANCE_CM": 10,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.10,
        "FAMILY_MEAN_TOLERANCE_CM": 5,
    },
    {
        "Scenario_Name": "Looser_Family",
        "MAX_CANDIDATE_CONFIGURATIONS": 30,
        "NEAR_SLOT_TOLERANCE_CM": 5,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.05,
        "FAMILY_SLOT_TOLERANCE_CM": 15,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.20,
        "FAMILY_MEAN_TOLERANCE_CM": 15,
    },
    {
        "Scenario_Name": "Higher_Near_Bucketing",
        "MAX_CANDIDATE_CONFIGURATIONS": 30,
        "NEAR_SLOT_TOLERANCE_CM": 10,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.10,
        "FAMILY_SLOT_TOLERANCE_CM": 15,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.20,
        "FAMILY_MEAN_TOLERANCE_CM": 10,
    },
    {
        "Scenario_Name": "Smaller_Shortlist",
        "MAX_CANDIDATE_CONFIGURATIONS": 24,
        "NEAR_SLOT_TOLERANCE_CM": 5,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.05,
        "FAMILY_SLOT_TOLERANCE_CM": 10,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.15,
        "FAMILY_MEAN_TOLERANCE_CM": 10,
    },
    {
        "Scenario_Name": "Larger_Shortlist",
        "MAX_CANDIDATE_CONFIGURATIONS": 45,
        "NEAR_SLOT_TOLERANCE_CM": 5,
        "NEAR_DISTRIBUTION_TOLERANCE": 0.05,
        "FAMILY_SLOT_TOLERANCE_CM": 10,
        "FAMILY_DISTRIBUTION_TOLERANCE": 0.15,
        "FAMILY_MEAN_TOLERANCE_CM": 10,
    },
]


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        return list(reader)


def _to_int(value: str) -> int:
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return 0


def _run_stage4(params: dict[str, object]) -> list[dict[str, str]]:
    env = os.environ.copy()
    for key, value in params.items():
        if key == "Scenario_Name":
            continue
        env[key] = str(value)

    subprocess.run([sys.executable, str(STAGE4_SCRIPT)], check=True, env=env)
    return _read_rows(OUTPUT_FILE)


def _k_mix(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {"3": 0, "4": 0, "5": 0, "6": 0, "7": 0}
    for row in rows:
        if str(row.get("Selection_Status", "")).strip() != "SHORTLISTED":
            continue
        k = str(_to_int(str(row.get("K", ""))))
        if k in counts:
            counts[k] += 1
    return counts


def run_sensitivity_check() -> Path:
    SENSITIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, str]] = []
    for scenario in SCENARIOS:
        rows = _run_stage4(scenario)
        shortlisted = [row for row in rows if str(row.get("Selection_Status", "")).strip() == "SHORTLISTED"]
        pruned = [row for row in rows if str(row.get("Selection_Status", "")).strip() == "PRUNED"]
        outside_limit = [
            row
            for row in rows
            if str(row.get("Prune_Reason", "")).strip()
            == "Outside shortlist limit after K-representative method-balanced family selection"
        ]
        dominated = [
            row
            for row in rows
            if str(row.get("Prune_Reason", "")).strip() == "Dominated by another family representative"
        ]
        near_identical = [
            row
            for row in rows
            if str(row.get("Prune_Reason", "")).strip().startswith("Near-identical in FAM_")
        ]

        k_counts = _k_mix(rows)

        summary_rows.append(
            {
                "Scenario_Name": str(scenario["Scenario_Name"]),
                "MAX_CANDIDATE_CONFIGURATIONS": str(scenario["MAX_CANDIDATE_CONFIGURATIONS"]),
                "NEAR_SLOT_TOLERANCE_CM": str(scenario["NEAR_SLOT_TOLERANCE_CM"]),
                "NEAR_DISTRIBUTION_TOLERANCE": str(scenario["NEAR_DISTRIBUTION_TOLERANCE"]),
                "FAMILY_SLOT_TOLERANCE_CM": str(scenario["FAMILY_SLOT_TOLERANCE_CM"]),
                "FAMILY_DISTRIBUTION_TOLERANCE": str(scenario["FAMILY_DISTRIBUTION_TOLERANCE"]),
                "FAMILY_MEAN_TOLERANCE_CM": str(scenario["FAMILY_MEAN_TOLERANCE_CM"]),
                "Total_Candidates": str(len(rows)),
                "Shortlisted": str(len(shortlisted)),
                "Pruned": str(len(pruned)),
                "Pruned_Outside_Shortlist": str(len(outside_limit)),
                "Pruned_Dominated": str(len(dominated)),
                "Pruned_Near_Identical": str(len(near_identical)),
                "Shortlisted_K3": str(k_counts["3"]),
                "Shortlisted_K4": str(k_counts["4"]),
                "Shortlisted_K5": str(k_counts["5"]),
                "Shortlisted_K6": str(k_counts["6"]),
                "Shortlisted_K7": str(k_counts["7"]),
            }
        )

    fieldnames = list(summary_rows[0].keys()) if summary_rows else []
    with SENSITIVITY_FILE.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary_rows)

    # Reset Stage 4 output to baseline scenario so downstream runs remain consistent.
    _run_stage4(SCENARIOS[0])

    return SENSITIVITY_FILE


if __name__ == "__main__":
    output_path = run_sensitivity_check()
    print(f"Stage 4 sensitivity check complete. Output written to: {output_path}")
