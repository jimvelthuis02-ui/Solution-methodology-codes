import csv
import math
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
ROBUSTNESS_SUMMARY_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
ROBUSTNESS_DETAILS_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Details.csv"
NON_FEASIBLE_OUTPUT_FILE = common.STAGE7_OUTPUT_DIR / "Non_Feasible_Layouts.csv"
NON_ROBUST_OUTPUT_FILE = common.STAGE7_OUTPUT_DIR / "Non_Robust_Layouts.csv"
CAPACITY_CONSTRAINT_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"
SCENARIO_INPUT_FILE = common.STAGE2_OUTPUT_DIR / "02_Item_Height_Scenarios_Delta_Weighted.csv"

# Stage 7 is now a proper scenario-based robustness pass. It evaluates the generated layout against
# the item-height scenarios created in Stage 2, rather than treating the stage as a single fixed-count
# capacity check. This makes the stage a true robustness assessment of the feasible layouts.
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


def _parse_layout_slot_counts(layout: dict[str, str]) -> dict[int, int]:
    """Parse the layout slot-size distribution from either the direct distribution field or the source sizes."""
    distribution_fields = [
        "Layout_Slot_Size_Distribution",
        "Base_Layout_Slot_Size_Distribution",
        "Layout_Slot_Size_Cumulative_Coverage",
    ]
    for field_name in distribution_fields:
        raw = str(layout.get(field_name, "")).strip()
        if not raw:
            continue
        counts: dict[int, int] = defaultdict(int)
        for token in raw.split("|"):
            size_text, count_text = token.split(":", 1) if ":" in token else (token, "1")
            size = common._to_int_default(size_text, -1)
            count = common._to_int_default(count_text, 0)
            if size >= 0 and count > 0:
                counts[size] += count
        if counts:
            return dict(counts)

    source_sizes = str(layout.get("Source_Slot_Sizes", "")).strip()
    if source_sizes:
        counts: dict[int, int] = defaultdict(int)
        for token in source_sizes.split(","):
            size = common._to_int_default(token, -1)
            if size >= 0:
                counts[size] += 1
        if counts:
            return dict(counts)

    return {}


def _scenario_columns() -> list[str]:
    """Return the scenario columns generated in Stage 2, e.g. Scenario_1_Item_Height."""
    if not SCENARIO_INPUT_FILE.exists():
        return []
    rows = _read_csv(SCENARIO_INPUT_FILE)
    if not rows:
        return []
    return [
        field_name
        for field_name in rows[0].keys()
        if field_name.lower().startswith("scenario_") and "item_height" in field_name.lower()
    ]


def _scenario_required_size_counts(scenario_rows: list[dict[str, str]], scenario_column: str) -> dict[int, int]:
    """Aggregate the scenario item-height values into a size-frequency table."""
    counts: dict[int, int] = defaultdict(int)
    for row in scenario_rows:
        value = common._to_float(row.get(scenario_column))
        if value is None:
            continue
        counts[int(round(value))] += 1
    return dict(counts)


def _scenario_item_height_distribution(scenario_rows: list[dict[str, str]], scenario_column: str) -> dict[int, int]:
    """Count how many scenario item heights fall into each item-height bucket."""
    counts: dict[int, int] = defaultdict(int)
    for row in scenario_rows:
        value = common._to_float(row.get(scenario_column))
        if value is None:
            continue
        bucket = int(round(value))
        counts[bucket] += 1
    return dict(counts)


def _normalize_distribution_to_target(distribution: dict[int, int], target_total: int) -> dict[int, int]:
    """Scale a scenario distribution to the exact base occupied-location count while preserving its shape."""
    if not distribution:
        return {}

    current_total = sum(distribution.values())
    if current_total <= 0:
        return {}
    if current_total == target_total:
        return dict(distribution)

    raw_scaled = {size: count * (target_total / current_total) for size, count in distribution.items()}
    floored = {size: int(math.floor(value)) for size, value in raw_scaled.items()}
    remainder = target_total - sum(floored.values())
    if remainder > 0:
        ranked = sorted(
            distribution.keys(),
            key=lambda size: (raw_scaled[size] - floored[size], -size),
            reverse=True,
        )
        for size in ranked[:remainder]:
            floored[size] += 1
    elif remainder < 0:
        ranked = sorted(
            distribution.keys(),
            key=lambda size: (floored[size] - raw_scaled[size], size),
            reverse=True,
        )
        for size in ranked[:abs(remainder)]:
            if floored[size] > 0:
                floored[size] -= 1

    return {size: count for size, count in floored.items() if count > 0}


def _at_or_below_count(distribution: dict[int, int], threshold: int) -> int:
    return sum(count for size, count in distribution.items() if size <= threshold)


def _scenario_coverage_ratio(layout_slot_counts: dict[int, int], scenario_rows: list[dict[str, str]], scenario_column: str) -> float:
    """Compare scenario demand against layout capacity using cumulative slot-size buckets.

    For each threshold slot size, count how many scenario item heights fall at or below that slot size
    and how many layout slots are available at or below that same threshold. This is the robustness view
    the user wants to see: scenario demand is bucketed by item-height, and the layout is bucketed by its
    slot sizes.
    """
    scenario_counts = _scenario_item_height_distribution(scenario_rows, scenario_column)
    if not scenario_counts:
        return 1.0

    thresholds = sorted(set(scenario_counts.keys()) | set(layout_slot_counts.keys()))
    if not thresholds:
        return 1.0

    ratios: list[float] = []
    for threshold in thresholds:
        demand_at_or_below = sum(count for size, count in scenario_counts.items() if size <= threshold)
        if demand_at_or_below <= 0:
            continue
        available_at_or_below = _at_or_below_count(layout_slot_counts, threshold)
        ratios.append(min(available_at_or_below / demand_at_or_below, 1.0))

    if not ratios:
        return 1.0
    return sum(ratios) / len(ratios)


def _scenario_requirements_by_config() -> dict[tuple[str, str], dict[int, int]]:
    rows = _read_csv(CAPACITY_CONSTRAINT_FILE)
    grouped: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for row in rows:
        config_id = str(row.get("Config_ID", "")).strip()
        sku_scenario = str(row.get("SKU_Scenario", "")).strip()
        size = common._to_int_default(row.get("Representative_Slot_Size"), -1)
        required = common._to_int_default(row.get("Min_Required_Locations_At_Or_Above_Size"), 0)
        if required <= 0:
            required = common._to_int_default(row.get("Cumulative_Assigned_SKUs_At_Or_Above_Size"), 0)
        if config_id and sku_scenario and size >= 0:
            grouped[(config_id, sku_scenario)][size] = max(grouped[(config_id, sku_scenario)].get(size, 0), required)
    return dict(grouped)


def build_robustness_evaluation() -> list[dict[str, str]]:
    """Evaluate only the Stage 6 layouts that were marked feasible."""
    layouts = _layouts()
    feasible_layouts = [
        layout for layout in layouts
        if str(layout.get("Layout_Feasible", "")).strip().upper() == "YES"
    ]
    infeasible_layouts = [
        layout for layout in layouts
        if str(layout.get("Layout_Feasible", "")).strip().upper() != "YES"
    ]
    scenario_rows = _read_csv(SCENARIO_INPUT_FILE) if SCENARIO_INPUT_FILE.exists() else []
    scenario_columns = _scenario_columns()
    robustness_rows: list[dict[str, str]] = []

    for layout in feasible_layouts:
        config_id = str(layout.get("Config_ID", "")).strip()
        if not config_id:
            continue

        layout_feasible_flag = str(layout.get("Layout_Feasible", "YES")).strip().upper() == "YES"
        assigned_locations_total = common._to_int_default(
            layout.get("Total_Locations") or layout.get("Required_Locations_Total"),
            0,
        )
        beam_relocations_total = common._to_int_default(layout.get("Beam_Relocations_Total"), 0)
        additional_beams = common._to_int_default(layout.get("Additional_Beams_Required"), 0)
        additional_grids = common._to_int_default(layout.get("Additional_Grids_Required"), 0)
        space_left = common._to_float(layout.get("Space_Left")) or 0.0
        layout_slot_counts = _parse_layout_slot_counts(layout)

        failure_reasons: set[str] = set()
        if not layout_feasible_flag:
            failure_reasons.add("Stage 6 layout feasibility failed")

        scenario_total = max(len(scenario_columns), 1)
        scenario_pass_count = 0
        scenario_failures: list[str] = []
        scenario_coverage_ratios: list[float] = []

        for scenario_column in scenario_columns:
            raw_scenario_counts = _scenario_item_height_distribution(scenario_rows, scenario_column)
            scenario_counts = _normalize_distribution_to_target(
                raw_scenario_counts,
                int(common.BASE_OCCUPIED_LOCATIONS_COUNT),
            )
            if not scenario_counts:
                scenario_coverage_ratios.append(1.0)
                scenario_pass_count += 1
                continue

            threshold_values = sorted(set(scenario_counts.keys()) | set(layout_slot_counts.keys()))
            scenario_pass = True
            threshold_ratios: list[float] = []
            for threshold in threshold_values:
                demand_at_or_below = sum(count for size, count in scenario_counts.items() if size <= threshold)
                available_at_or_below = _at_or_below_count(layout_slot_counts, threshold)
                if demand_at_or_below > 0:
                    threshold_ratios.append(min(available_at_or_below / demand_at_or_below, 1.0))
                if demand_at_or_below > available_at_or_below:
                    scenario_pass = False
                    scenario_failures.append(f"{scenario_column}:{threshold}:{demand_at_or_below}>{available_at_or_below}")

            coverage_ratio = sum(threshold_ratios) / len(threshold_ratios) if threshold_ratios else 1.0
            scenario_coverage_ratios.append(coverage_ratio)

            if scenario_pass:
                scenario_pass_count += 1
            else:
                failure_reasons.add(f"scenario failed: {scenario_column}")

        robustness = (scenario_pass_count / scenario_total) if scenario_total else 0.0
        mean_scenario_coverage = sum(scenario_coverage_ratios) / len(scenario_coverage_ratios) if scenario_coverage_ratios else 0.0
        worst_scenario_coverage = min(scenario_coverage_ratios) if scenario_coverage_ratios else 0.0

        rack_height_used = common._to_float(layout.get("Percentage_Rack_Height_Used"))
        if rack_height_used is not None:
            physical_utilization = rack_height_used / 100.0
        else:
            physical_utilization = common._to_float(layout.get("Space_Utilization")) or 0.0

        if physical_utilization <= 0.0:
            physical_utilization = common._to_float(layout.get("Space_Utilization")) or 0.0

        robustness_rows.append(
            {
                "Config_ID": config_id,
                "Layout_Feasible": str(layout.get("Layout_Feasible", "")),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Required_Locations_Total": str(common._to_int_default(layout.get("Required_Locations_Total"), 0)),
                "Capacity_Margin": str(common._to_int_default(layout.get("Capacity_Margin"), assigned_locations_total - common._to_int_default(layout.get("Required_Locations_Total"), 0))),
                "Robustness": f"{robustness:.6f}",
                "Scenario_Pass_Count": str(scenario_pass_count),
                "Scenario_Total_Count": str(scenario_total),
                "Mean_Scenario_Coverage_Ratio": f"{mean_scenario_coverage:.6f}",
                "Worst_Scenario_Coverage_Ratio": f"{worst_scenario_coverage:.6f}",
                "Physical_Utilization_Rate": f"{physical_utilization:.6f}",
                "Beam_Relocations_Total": str(beam_relocations_total),
                "Additional_Beams_Required": str(additional_beams),
                "Additional_Grids_Required": str(additional_grids),
                "Space_Left": f"{space_left:.3f}",
                "Failure_Reasons": "; ".join(sorted(failure_reasons)) if failure_reasons else "",
                "Scenario_Details": "; ".join(scenario_failures),
            }
        )

    non_robust_rows: list[dict[str, str]] = []
    non_feasible_rows: list[dict[str, str]] = []

    for layout in infeasible_layouts:
        config_id = str(layout.get("Config_ID", "")).strip()
        if not config_id:
            continue
        non_feasible_rows.append(
            {
                "Config_ID": config_id,
                "Layout_Feasible": str(layout.get("Layout_Feasible", "")),
                "Reason": "Stage 6 layout feasibility failed",
                "Assigned_Locations_Total": str(common._to_int_default(layout.get("Total_Locations") or layout.get("Required_Locations_Total"), 0)),
                "Required_Locations_Total": str(common._to_int_default(layout.get("Required_Locations_Total"), 0)),
                "Capacity_Margin": str(common._to_int_default(layout.get("Capacity_Margin"), 0)),
                "Space_Left": str(layout.get("Space_Left", "")),
                "Required_Beams_Total": str(common._to_int_default(layout.get("Required_Beams_Total"), 0)),
                "Required_Grids_Total": str(common._to_int_default(layout.get("Required_Grids_Total"), 0)),
                "Additional_Beams_Required": str(common._to_int_default(layout.get("Additional_Beams_Required"), 0)),
                "Additional_Grids_Required": str(common._to_int_default(layout.get("Additional_Grids_Required"), 0)),
                "Robustness": "0.000000",
                "Scenario_Pass_Count": "0",
                "Scenario_Total_Count": str(max(len(scenario_columns), 1)),
                "Failure_Reasons": "Stage 6 layout feasibility failed",
            }
        )

    for row in robustness_rows:
        if common._to_int_default(row.get("Scenario_Pass_Count"), 0) < common._to_int_default(row.get("Scenario_Total_Count"), 0):
            non_robust_rows.append(row)

    _write_exclusion_file(NON_FEASIBLE_OUTPUT_FILE, non_feasible_rows)
    _write_exclusion_file(NON_ROBUST_OUTPUT_FILE, non_robust_rows)

    executive_rows = [
        {
            "Config_ID": row.get("Config_ID", ""),
            "Layout_Feasible": row.get("Layout_Feasible", ""),
            "Assigned_Locations_Total": row.get("Assigned_Locations_Total", ""),
            "Required_Locations_Total": row.get("Required_Locations_Total", ""),
            "Capacity_Margin": row.get("Capacity_Margin", ""),
            "Robustness": row.get("Robustness", ""),
            "Scenario_Pass_Count": row.get("Scenario_Pass_Count", ""),
            "Scenario_Total_Count": row.get("Scenario_Total_Count", ""),
            "Mean_Scenario_Coverage_Ratio": row.get("Mean_Scenario_Coverage_Ratio", ""),
            "Worst_Scenario_Coverage_Ratio": row.get("Worst_Scenario_Coverage_Ratio", ""),
            "Physical_Utilization_Rate": row.get("Physical_Utilization_Rate", ""),
            "Failure_Reasons": row.get("Failure_Reasons", ""),
        }
        for row in robustness_rows
    ]

    detail_rows = [
        {
            "Config_ID": row.get("Config_ID", ""),
            "Layout_Feasible": row.get("Layout_Feasible", ""),
            "Assigned_Locations_Total": row.get("Assigned_Locations_Total", ""),
            "Required_Locations_Total": row.get("Required_Locations_Total", ""),
            "Capacity_Margin": row.get("Capacity_Margin", ""),
            "Robustness": row.get("Robustness", ""),
            "Scenario_Pass_Count": row.get("Scenario_Pass_Count", ""),
            "Scenario_Total_Count": row.get("Scenario_Total_Count", ""),
            "Mean_Scenario_Coverage_Ratio": row.get("Mean_Scenario_Coverage_Ratio", ""),
            "Worst_Scenario_Coverage_Ratio": row.get("Worst_Scenario_Coverage_Ratio", ""),
            "Physical_Utilization_Rate": row.get("Physical_Utilization_Rate", ""),
            "Beam_Relocations_Total": row.get("Beam_Relocations_Total", ""),
            "Additional_Beams_Required": row.get("Additional_Beams_Required", ""),
            "Additional_Grids_Required": row.get("Additional_Grids_Required", ""),
            "Space_Left": row.get("Space_Left", ""),
            "Failure_Reasons": row.get("Failure_Reasons", ""),
            "Scenario_Details": row.get("Scenario_Details", ""),
        }
        for row in robustness_rows
    ]

    _write_csv_preserve(
        ROBUSTNESS_SUMMARY_FILE,
        [
            "Config_ID",
            "Layout_Feasible",
            "Assigned_Locations_Total",
            "Required_Locations_Total",
            "Capacity_Margin",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Mean_Scenario_Coverage_Ratio",
            "Worst_Scenario_Coverage_Ratio",
            "Physical_Utilization_Rate",
            "Failure_Reasons",
        ],
        executive_rows,
    )

    _write_csv_preserve(
        ROBUSTNESS_DETAILS_FILE,
        [
            "Config_ID",
            "Layout_Feasible",
            "Assigned_Locations_Total",
            "Required_Locations_Total",
            "Capacity_Margin",
            "Robustness",
            "Scenario_Pass_Count",
            "Scenario_Total_Count",
            "Mean_Scenario_Coverage_Ratio",
            "Worst_Scenario_Coverage_Ratio",
            "Physical_Utilization_Rate",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Space_Left",
            "Failure_Reasons",
            "Scenario_Details",
        ],
        detail_rows,
    )

    return robustness_rows


if __name__ == "__main__":
    # Stage 7 evaluates the implemented layout without synthetic top-fill adjustments.
    robustness_rows = build_robustness_evaluation()
    print(
        "Robustness evaluation complete. "
        f"Summary rows: {len(robustness_rows)}."
    )
