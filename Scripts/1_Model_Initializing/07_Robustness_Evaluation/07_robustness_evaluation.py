import csv
from collections import defaultdict
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stage_pipeline_common import load_legacy_pipeline


legacy = load_legacy_pipeline()
LAYOUT_SUMMARY_FILE = legacy.LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
SCENARIO_FILE = legacy.ROOT / "Output" / "02_Scenario_Generation" / "02_Item_Height_Scenarios_Delta_Weighted.csv"
OUTPUT_FILE = legacy.FEASIBLE_OUTPUT_DIR / "Candidate_Layout_Scenario_Evaluation.csv"
ROBUSTNESS_SUMMARY_FILE = legacy.FEASIBLE_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"


SKU_SCENARIOS = legacy._build_sku_count_scenarios([])


def _read_csv(path: object) -> list[dict[str, str]]:
    return legacy._read_csv(path)


def _item_height_scenarios() -> list[str]:
    if not SCENARIO_FILE.exists():
        return [f"Scenario_{index}_Item_Height" for index in range(1, 7)]
    with SCENARIO_FILE.open("r", newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames is None:
            return [f"Scenario_{index}_Item_Height" for index in range(1, 7)]
        return [field for field in reader.fieldnames if field.startswith("Scenario_") and field.endswith("_Item_Height")]


def _layouts() -> list[dict[str, str]]:
    if not LAYOUT_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing layout summary file: {LAYOUT_SUMMARY_FILE}")
    return _read_csv(LAYOUT_SUMMARY_FILE)


def build_robustness_evaluation() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    layouts = _layouts()
    height_scenarios = _item_height_scenarios()

    scenario_rows: list[dict[str, str]] = []
    robustness_rows: list[dict[str, str]] = []

    for layout in layouts:
        layout_id = str(layout.get("Layout_ID", "")).strip()
        if not layout_id:
            continue

        assigned_locations_total = legacy._to_int_default(layout.get("Assigned_Locations_Total"), 0)
        total_physical_locations = legacy._to_int_default(layout.get("Total_Physical_Locations"), 0)
        beam_relocations_total = legacy._to_int_default(layout.get("Beam_Relocations_Total"), 0)
        additional_beams = legacy._to_int_default(layout.get("Additional_Beams_Required"), 0)
        additional_grids = legacy._to_int_default(layout.get("Additional_Grids_Required"), 0)
        space_left = legacy._to_float(layout.get("Space_Left")) or 0.0

        occupancy_values: list[float] = []
        utilization_values: list[float] = []
        satisfied_count = 0
        total_count = 0

        for height_scenario in height_scenarios:
            for sku_scenario, sku_count in SKU_SCENARIOS.items():
                occupancy = sku_count / max(assigned_locations_total, 1)
                utilization = sku_count / max(total_physical_locations, 1)
                exact_match = sku_count == assigned_locations_total
                constraint_satisfied = exact_match and occupancy <= 1.0 and utilization <= 1.0

                scenario_rows.append(
                    {
                        "Layout_ID": layout_id,
                        "Config_ID": str(layout.get("Config_ID", "")),
                        "Style": str(layout.get("Style", "")),
                        "Item_Height_Scenario": height_scenario,
                        "SKU_Scenario": sku_scenario,
                        "SKU_Count": str(sku_count),
                        "Constraint_Satisfied": "YES" if constraint_satisfied else "NO",
                        "Occupancy_Rate": f"{occupancy:.6f}",
                        "Utilization_Rate": f"{utilization:.6f}",
                        "Space_Left": f"{space_left:.3f}",
                        "Demand_Assignment_Exact_Match": "YES" if exact_match else "NO",
                        "Beam_Relocations_Total": str(beam_relocations_total),
                        "Additional_Beams_Required": str(additional_beams),
                        "Additional_Grids_Required": str(additional_grids),
                    }
                )

                occupancy_values.append(occupancy)
                utilization_values.append(utilization)
                total_count += 1
                if constraint_satisfied:
                    satisfied_count += 1

        robustness_rows.append(
            {
                "Layout_ID": layout_id,
                "Config_ID": str(layout.get("Config_ID", "")),
                "Style": str(layout.get("Style", "")),
                "Layout_Feasible": str(layout.get("Layout_Feasible", "")),
                "Mean_Occupancy_Rate": f"{(sum(occupancy_values) / len(occupancy_values)) if occupancy_values else 0.0:.6f}",
                "Worst_Occupancy_Rate": f"{max(occupancy_values) if occupancy_values else 0.0:.6f}",
                "Mean_Utilization_Rate": f"{(sum(utilization_values) / len(utilization_values)) if utilization_values else 0.0:.6f}",
                "Worst_Utilization_Rate": f"{max(utilization_values) if utilization_values else 0.0:.6f}",
                "Robustness": f"{(satisfied_count / total_count) if total_count else 0.0:.6f}",
                "Scenario_Pass_Count": str(satisfied_count),
                "Scenario_Total_Count": str(total_count),
                "Beam_Relocations_Total": str(beam_relocations_total),
                "Additional_Beams_Required": str(additional_beams),
                "Additional_Grids_Required": str(additional_grids),
                "Space_Left": f"{space_left:.3f}",
            }
        )

    legacy._write_csv_clean(
        OUTPUT_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Item_Height_Scenario",
            "SKU_Scenario",
            "SKU_Count",
            "Constraint_Satisfied",
            "Occupancy_Rate",
            "Utilization_Rate",
            "Space_Left",
            "Demand_Assignment_Exact_Match",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
        ],
        scenario_rows,
    )

    legacy._write_csv_clean(
        ROBUSTNESS_SUMMARY_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Layout_Feasible",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Mean_Utilization_Rate",
            "Worst_Utilization_Rate",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Space_Left",
        ],
        robustness_rows,
    )

    return scenario_rows, robustness_rows


if __name__ == "__main__":
    scenario_rows, robustness_rows = build_robustness_evaluation()
    print(
        "Robustness evaluation complete. "
        f"Scenario rows: {len(scenario_rows)}, summary rows: {len(robustness_rows)}."
    )
