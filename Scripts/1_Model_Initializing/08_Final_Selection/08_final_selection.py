import csv
from collections import defaultdict
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stage_pipeline_common import load_legacy_pipeline


legacy = load_legacy_pipeline()
ROBUSTNESS_SUMMARY_FILE = legacy.FEASIBLE_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
LAYOUT_SUMMARY_FILE = legacy.LAYOUT_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
LAYOUT_BY_COLUMN_FILE = legacy.LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Rack_Column.csv"
LAYOUT_BY_LOCATION_FILE = legacy.LAYOUT_OUTPUT_DIR / "Candidate_Layout_By_Location.csv"
OUTPUT_FILE = legacy.FEASIBLE_OUTPUT_DIR / "Final_Layout_Ranking.csv"
FINAL_LAYOUT_BY_COLUMN_FILE = legacy.LAYOUT_OUTPUT_DIR / "Final_Layout_By_Rack_Column.csv"
FINAL_LAYOUT_BY_LOCATION_FILE = legacy.LAYOUT_OUTPUT_DIR / "Final_Layout_By_Location.csv"
FINAL_LAYOUT_COUNT = 6


def _read_csv(path: object) -> list[dict[str, str]]:
    return legacy._read_csv(path)


def _layout_map() -> dict[str, dict[str, str]]:
    layouts: dict[str, dict[str, str]] = {}
    if LAYOUT_SUMMARY_FILE.exists():
        for row in _read_csv(LAYOUT_SUMMARY_FILE):
            layouts[str(row.get("Layout_ID", "")).strip()] = row
    return layouts


def _robustness_rows() -> list[dict[str, str]]:
    if not ROBUSTNESS_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing robustness summary file: {ROBUSTNESS_SUMMARY_FILE}")
    return _read_csv(ROBUSTNESS_SUMMARY_FILE)


def build_final_selection() -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    robustness_rows = _robustness_rows()
    layouts = _layout_map()

    ranking_rows: list[dict[str, str]] = []
    joined_rows: list[dict[str, str]] = []
    for row in robustness_rows:
        layout_id = str(row.get("Layout_ID", "")).strip()
        if not layout_id:
            continue
        layout = layouts.get(layout_id, {})
        merged = dict(layout)
        merged.update(row)
        joined_rows.append(merged)

    joined_rows.sort(
        key=lambda row: (
            0 if str(row.get("Layout_Feasible", "")).strip().upper() == "YES" else 1,
            -(legacy._to_float(row.get("Robustness")) or 0.0),
            -(legacy._to_float(row.get("Mean_Utilization_Rate")) or 0.0),
            -(legacy._to_float(row.get("Mean_Occupancy_Rate")) or 0.0),
            legacy._to_int_default(row.get("Beam_Relocations_Total"), 0),
            legacy._to_int_default(row.get("Additional_Beams_Required"), 0) + legacy._to_int_default(row.get("Additional_Grids_Required"), 0),
            legacy._to_float(row.get("Space_Left")) or 0.0,
            -legacy._to_int_default(row.get("Assigned_Locations_Total"), 0),
        )
    )

    finalist_ids = {str(row.get("Layout_ID", "")).strip() for row in joined_rows[:FINAL_LAYOUT_COUNT]}

    for rank, row in enumerate(joined_rows, start=1):
        layout_id = str(row.get("Layout_ID", "")).strip()
        ranking_rows.append(
            {
                "Rank": str(rank),
                "Selection": "FINAL" if layout_id in finalist_ids else "NON_FINAL",
                "Layout_ID": layout_id,
                "Config_ID": str(row.get("Config_ID", "")),
                "Style": str(row.get("Style", "")),
                "Layout_Feasible": str(row.get("Layout_Feasible", "")),
                "Robustness": f"{legacy._to_float(row.get('Robustness')) or 0.0:.6f}",
                "Scenario_Pass_Count": str(legacy._to_int_default(row.get("Scenario_Pass_Count"), 0)),
                "Scenario_Total_Count": str(legacy._to_int_default(row.get("Scenario_Total_Count"), 0)),
                "Mean_Occupancy_Rate": f"{legacy._to_float(row.get('Mean_Occupancy_Rate')) or 0.0:.6f}",
                "Worst_Occupancy_Rate": f"{legacy._to_float(row.get('Worst_Occupancy_Rate')) or 0.0:.6f}",
                "Mean_Utilization_Rate": f"{legacy._to_float(row.get('Mean_Utilization_Rate')) or 0.0:.6f}",
                "Worst_Utilization_Rate": f"{legacy._to_float(row.get('Worst_Utilization_Rate')) or 0.0:.6f}",
                "Space_Left": f"{legacy._to_float(row.get('Space_Left')) or 0.0:.3f}",
                "Beam_Relocations_Total": str(legacy._to_int_default(row.get("Beam_Relocations_Total"), 0)),
                "Additional_Beams_Required": str(legacy._to_int_default(row.get("Additional_Beams_Required"), 0)),
                "Additional_Grids_Required": str(legacy._to_int_default(row.get("Additional_Grids_Required"), 0)),
                "Assigned_Locations_Total": str(legacy._to_int_default(row.get("Assigned_Locations_Total"), 0)),
                "Notes": str(row.get("Notes", "")),
            }
        )

    legacy._write_csv_clean(
        OUTPUT_FILE,
        [
            "Rank",
            "Selection",
            "Layout_ID",
            "Config_ID",
            "Style",
            "Layout_Feasible",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Mean_Utilization_Rate",
            "Worst_Utilization_Rate",
            "Space_Left",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Assigned_Locations_Total",
            "Notes",
        ],
        ranking_rows,
    )

    finalist_column_rows: list[dict[str, str]] = []
    if LAYOUT_BY_COLUMN_FILE.exists():
        for row in _read_csv(LAYOUT_BY_COLUMN_FILE):
            if str(row.get("Layout_ID", "")).strip() in finalist_ids:
                finalist_column_rows.append(row)

    finalist_location_rows: list[dict[str, str]] = []
    if LAYOUT_BY_LOCATION_FILE.exists():
        for row in _read_csv(LAYOUT_BY_LOCATION_FILE):
            if str(row.get("Layout_ID", "")).strip() in finalist_ids:
                finalist_location_rows.append(row)

    legacy._write_csv_clean(
        FINAL_LAYOUT_BY_COLUMN_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Rack_Column",
            "Beam_Count_Used",
            "Allowed_Used_Height_cm",
            "Assigned_Used_Height_cm",
            "Remaining_Height_cm",
            "Fill_Ratio",
            "Beam_Relocations_In_Column",
            "Slot_Size_Distribution",
        ],
        finalist_column_rows,
    )

    legacy._write_csv_clean(
        FINAL_LAYOUT_BY_LOCATION_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "Location",
            "Rack",
            "Column",
            "Row",
            "Beam_Coordinate",
            "Assignment_Unit_ID",
            "Assignment_Unit_Type",
            "Assigned_Slot_Size_cm",
        ],
        finalist_location_rows,
    )

    return ranking_rows, finalist_column_rows, finalist_location_rows


if __name__ == "__main__":
    ranking_rows, finalist_column_rows, finalist_location_rows = build_final_selection()
    print(
        "Final selection complete. "
        f"Ranking rows: {len(ranking_rows)}, finalists: {len(finalist_column_rows)} columns / {len(finalist_location_rows)} locations."
    )
