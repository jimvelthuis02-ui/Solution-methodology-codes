import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
ROBUSTNESS_SUMMARY_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
NON_FEASIBLE_OUTPUT_FILE = common.STAGE7_OUTPUT_DIR / "Non_Feasible_Layouts.csv"
NON_ROBUST_OUTPUT_FILE = common.STAGE7_OUTPUT_DIR / "Non_Robust_Layouts.csv"
CAPACITY_CONSTRAINT_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"


# Stage 7 evaluates the fixed occupied-location demand used for the baseline run.
# The historical item-height sample measured 939 locations, but the operational demand for
# the baseline occupancy is 890 occupied slots. The Stage 4 slot-size distribution must be
# mapped onto 890, not 939, before passing the capacity requirement into Stage 7.
OCCUPIED_LOCATION_SCENARIOS = {
    "Scenario 1": int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
    "Scenario_1": int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
    "Base_Count": int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    # Keep CSV read behavior consistent with shared pipeline helpers.
    return common._read_csv(path)


def _write_csv_preserve(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    # Stage 7 outputs feed Stage 8 ranking; keep all columns even when values are identical.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _layouts() -> list[dict[str, str]]:
    """Load all Stage 6 layouts so exclusions can be reported explicitly."""
    if not LAYOUT_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing layout summary file: {LAYOUT_SUMMARY_FILE}")
    rows = _read_csv(LAYOUT_SUMMARY_FILE)
    return rows


def _write_exclusion_file(path: Path, rows: list[dict[str, str]]) -> None:
    fields = [
        "Config_ID",
        "Layout_Feasible",
        "Reason",
        "Assigned_Locations_Total",
        "Required_Locations_Total",
        "Capacity_Margin",
        "Space_Left",
        "Required_Beams_Total",
        "Required_Grids_Total",
        "Additional_Beams_Required",
        "Additional_Grids_Required",
        "Robustness",
        "Scenario_Pass_Count",
        "Scenario_Total_Count",
        "Failure_Reasons",
    ]
    _write_csv_preserve(path, fields, [{field: str(row.get(field, "")) for field in fields} for row in rows])


def _parse_slot_distribution(value: str) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for token in str(value).split("|"):
        text = token.strip()
        if not text or ":" not in text:
            continue
        size_text, count_text = text.split(":", 1)
        size = common._to_int_default(size_text, -1)
        count = common._to_int_default(count_text, 0)
        if size >= 0 and count > 0:
            counts[size] += count
    return dict(counts)


def _available_at_or_above(exact_counts: dict[int, int], threshold_size: int) -> int:
    return sum(count for size, count in exact_counts.items() if size >= threshold_size)


def _scenario_requirements_by_config() -> dict[tuple[str, str], dict[int, int]]:
    rows = _read_csv(CAPACITY_CONSTRAINT_FILE)
    grouped: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for row in rows:
        config_id = str(row.get("Config_ID", "")).strip()
        sku_scenario = str(row.get("SKU_Scenario", "")).strip()
        size = common._to_int_default(row.get("Representative_Slot_Size"), -1)
        # Support both current and legacy Stage 5 headers to avoid silent zero requirements.
        required = common._to_int_default(row.get("Min_Required_Locations_At_Or_Above_Size"), 0)
        if required <= 0:
            required = common._to_int_default(row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size"), 0)
        if config_id and sku_scenario and size >= 0:
            grouped[(config_id, sku_scenario)][size] = max(grouped[(config_id, sku_scenario)].get(size, 0), required)
    return dict(grouped)


def build_robustness_evaluation() -> list[dict[str, str]]:
    """Evaluate each selected layout for the baseline (100%) SKU-count scenario."""
    layouts = _layouts()
    scenario_requirements = _scenario_requirements_by_config()

    robustness_rows: list[dict[str, str]] = []

    for layout in layouts:
        config_id = str(layout.get("Config_ID", "")).strip()
        if not config_id:
            continue

        assigned_locations_total = common._to_int_default(
            layout.get("Total_Locations") or layout.get("Required_Locations_Total"),
            0,
        )
        # Stage 7 utilization uses the vertical-space utilization already computed in Stage 6.
        vertical_space_utilization = common._to_float(layout.get("Space_Utilization"))
        if vertical_space_utilization is None:
            pct_used = common._to_float(layout.get("Percentage_Rack_Height_Used")) or 0.0
            vertical_space_utilization = pct_used / 100.0
        beam_relocations_total = common._to_int_default(layout.get("Beam_Relocations_Total"), 0)
        additional_beams = common._to_int_default(layout.get("Additional_Beams_Required"), 0)
        additional_grids = common._to_int_default(layout.get("Additional_Grids_Required"), 0)
        space_left = common._to_float(layout.get("Space_Left")) or 0.0

        occupancy_values: list[float] = []
        utilization_values: list[float] = []
        capacity_margin_values: list[int] = []
        capacity_ratio_values: list[float] = []
        normalized_slack_values: list[float] = []
        failure_reasons: set[str] = set()
        satisfied_count = 0
        total_count = 0

        layout_slot_counts = _parse_slot_distribution(
            str(
                layout.get("Base_Layout_Slot_Size_Distribution")
                or layout.get("Layout_Slot_Size_Distribution", "")
            )
        )
        doorgang_alignment_allowance = common._to_int_default(
            layout.get("Doorgang_Usable_Alignment_Conversions"),
            0,
        )

        for sku_scenario, sku_count in OCCUPIED_LOCATION_SCENARIOS.items():
            occupancy = sku_count / max(assigned_locations_total, 1)
            utilization = vertical_space_utilization
            capacity_margin = assigned_locations_total - sku_count
            capacity_ratio = assigned_locations_total / max(sku_count, 1)
            normalized_slack = capacity_margin / max(assigned_locations_total, 1)
            layout_feasible_flag = str(layout.get("Layout_Feasible", "YES")).strip().upper() == "YES"

            required_by_size = scenario_requirements.get((str(layout.get("Config_ID", "")), sku_scenario), {})
            slot_coverage_pass = True
            largest_required_size = max(required_by_size.keys(), default=-1)
            for size, required in required_by_size.items():
                available = _available_at_or_above(layout_slot_counts, size)
                deficit = required - available
                if deficit > 0:
                    if size == largest_required_size and deficit <= doorgang_alignment_allowance:
                        continue
                    slot_coverage_pass = False
                    break

            constraint_satisfied = layout_feasible_flag and slot_coverage_pass

            if not layout_feasible_flag:
                failure_reasons.add("Stage 6 Layout_Feasible != YES")
            if not slot_coverage_pass:
                failure_reasons.add("slot-size coverage requirement not met")

            occupancy_values.append(occupancy)
            utilization_values.append(utilization)
            capacity_margin_values.append(capacity_margin)
            capacity_ratio_values.append(capacity_ratio)
            normalized_slack_values.append(normalized_slack)
            total_count += 1
            if constraint_satisfied:
                satisfied_count += 1

        # Aggregate scenario-level results into one robustness summary row per layout.
        robustness_rows.append(
            {
                "Config_ID": config_id,
                "Layout_Feasible": str(layout.get("Layout_Feasible", "")),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Required_Locations_Total": str(common._to_int_default(layout.get("Required_Locations_Total"), 0)),
                "Capacity_Margin": str(common._to_int_default(layout.get("Capacity_Margin"), assigned_locations_total - common._to_int_default(layout.get("Required_Locations_Total"), 0))),
                "Mean_Occupancy_Rate": f"{(sum(occupancy_values) / len(occupancy_values)) if occupancy_values else 0.0:.6f}",
                "Worst_Occupancy_Rate": f"{max(occupancy_values) if occupancy_values else 0.0:.6f}",
                "Mean_Utilization_Rate": f"{(sum(utilization_values) / len(utilization_values)) if utilization_values else 0.0:.6f}",
                "Worst_Utilization_Rate": f"{max(utilization_values) if utilization_values else 0.0:.6f}",
                "Worst_Capacity_Margin": str(min(capacity_margin_values) if capacity_margin_values else 0),
                "Minimum_Capacity_Ratio": f"{min(capacity_ratio_values) if capacity_ratio_values else 0.0:.6f}",
                "Minimum_Normalized_Slack": f"{min(normalized_slack_values) if normalized_slack_values else 0.0:.6f}",
                "Robustness": f"{(satisfied_count / total_count) if total_count else 0.0:.6f}",
                "Scenario_Pass_Count": str(satisfied_count),
                "Scenario_Total_Count": str(total_count),
                "Beam_Relocations_Total": str(beam_relocations_total),
                "Required_Beams_Total": str(common._to_int_default(layout.get("Required_Beams_Total"), 0)),
                "Additional_Beams_Required": str(additional_beams),
                "Required_Grids_Total": str(common._to_int_default(layout.get("Required_Grids_Total"), 0)),
                "Additional_Grids_Required": str(additional_grids),
                "Space_Left": f"{space_left:.3f}",
                "Failure_Reasons": "; ".join(sorted(failure_reasons)),
            }
        )

    non_feasible_rows: list[dict[str, str]] = []
    non_robust_rows: list[dict[str, str]] = []
    robust_rows: list[dict[str, str]] = []
    for row in robustness_rows:
        if str(row.get("Layout_Feasible", "")).strip().upper() != "YES":
            row["Reason"] = "Stage 6 layout feasibility failed"
            non_feasible_rows.append(row)
        elif common._to_int_default(row.get("Scenario_Pass_Count"), 0) < common._to_int_default(row.get("Scenario_Total_Count"), 0):
            row["Reason"] = "One or more robustness scenarios failed"
            non_robust_rows.append(row)
        else:
            robust_rows.append(row)

    _write_exclusion_file(NON_FEASIBLE_OUTPUT_FILE, non_feasible_rows)
    _write_exclusion_file(NON_ROBUST_OUTPUT_FILE, non_robust_rows)

    # Write only feasible and robust layouts to the summary used by final ranking.
    _write_csv_preserve(
        ROBUSTNESS_SUMMARY_FILE,
        [
            "Config_ID",
            "Layout_Feasible",
            "Assigned_Locations_Total",
            "Required_Locations_Total",
            "Capacity_Margin",
            "Mean_Occupancy_Rate",
            "Worst_Occupancy_Rate",
            "Mean_Utilization_Rate",
            "Worst_Utilization_Rate",
            "Worst_Capacity_Margin",
            "Minimum_Capacity_Ratio",
            "Minimum_Normalized_Slack",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Beam_Relocations_Total",
            "Required_Beams_Total",
            "Additional_Beams_Required",
            "Required_Grids_Total",
            "Additional_Grids_Required",
            "Space_Left",
            "Failure_Reasons",
        ],
        robust_rows,
    )

    return robust_rows


if __name__ == "__main__":
    # Stage 7 evaluates the implemented layout without synthetic top-fill adjustments.
    robustness_rows = build_robustness_evaluation()
    print(
        "Robustness evaluation complete. "
        f"Summary rows: {len(robustness_rows)}."
    )
