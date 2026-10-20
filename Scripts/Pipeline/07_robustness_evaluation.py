from collections import defaultdict
import os
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common
from heuristic_output_utils import suffix_output_tree


# Stage 7 input/output paths and scenario source.
LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
ROBUSTNESS_DETAILS_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Details.csv"
SCENARIO_INPUT_FILE = common.STAGE2_OUTPUT_DIR / "02_Item_Height_Scenarios_Delta_Weighted.csv"

# Stage 7 is now a proper scenario-based robustness pass. It evaluates the generated layout against
# the item-height scenarios created in Stage 2, rather than treating the stage as a single fixed-count
# capacity check. This makes the stage a true robustness assessment of the feasible layouts.
_read_csv = common._read_csv


_write_csv_preserve = common._write_csv_preserve


def _layouts() -> list[dict[str, str]]:
    """Load all Stage 6 layouts so exclusions can be reported explicitly."""
    if not LAYOUT_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing layout summary file: {LAYOUT_SUMMARY_FILE}")
    rows = _read_csv(LAYOUT_SUMMARY_FILE)
    return rows


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


_normalize_distribution_to_target = common._normalize_distribution_to_target


def _at_or_above_count(distribution: dict[int, int], threshold: int) -> int:
    """Slots/items with size >= threshold: a slot can always hold an item shorter than itself."""
    return sum(count for size, count in distribution.items() if size >= threshold)


def build_robustness_evaluation() -> list[dict[str, str]]:
    """Evaluate only the Stage 6 layouts that were marked feasible."""
    layouts = _layouts()
    feasible_layouts = [
        layout for layout in layouts
        if str(layout.get("Layout_Feasible", "")).strip().upper() == "YES"
    ]
    scenario_rows = _read_csv(SCENARIO_INPUT_FILE) if SCENARIO_INPUT_FILE.exists() else []
    scenario_columns = _scenario_columns()
    robustness_rows: list[dict[str, str]] = []

    for layout in feasible_layouts:
        config_id = str(layout.get("Config_ID", "")).strip()
        if not config_id:
            continue

        assigned_locations_total = common._to_int_default(layout.get("Total_Locations"), 0)
        beam_relocations_total = common._to_int_default(layout.get("Beam_Relocations_Total"), 0)
        additional_beams = common._to_int_default(layout.get("Additional_Beams_Required"), 0)
        additional_grids = common._to_int_default(layout.get("Additional_Grids_Required"), 0)
        layout_slot_counts = _parse_layout_slot_counts(layout)

        failure_reasons: set[str] = set()

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

            # Check overflow feasibility from the largest slot size down: an item can be placed in its
            # best-fit slot size or any larger one, so a full smaller slot size doesn't block assignment
            # as long as enough locations exist at or above that item's height somewhere in the layout.
            threshold_values = sorted(set(scenario_counts.keys()) | set(layout_slot_counts.keys()))
            scenario_pass = True
            threshold_ratios: list[float] = []
            for threshold in threshold_values:
                demand_at_or_above = sum(count for size, count in scenario_counts.items() if size >= threshold)
                available_at_or_above = _at_or_above_count(layout_slot_counts, threshold)
                if demand_at_or_above > 0:
                    threshold_ratios.append(min(available_at_or_above / demand_at_or_above, 1.0))
                if demand_at_or_above > available_at_or_above:
                    scenario_pass = False
                    scenario_failures.append(f"{scenario_column}:{threshold}:{demand_at_or_above}>{available_at_or_above}")

            coverage_ratio = sum(threshold_ratios) / len(threshold_ratios) if threshold_ratios else 1.0
            scenario_coverage_ratios.append(coverage_ratio)

            if scenario_pass:
                scenario_pass_count += 1
            else:
                failure_reasons.add(f"scenario failed: {scenario_column}")

        robustness = (scenario_pass_count / scenario_total) if scenario_total else 0.0
        mean_scenario_coverage = sum(scenario_coverage_ratios) / len(scenario_coverage_ratios) if scenario_coverage_ratios else 0.0
        worst_scenario_coverage = min(scenario_coverage_ratios) if scenario_coverage_ratios else 0.0

        robustness_rows.append(
            {
                "Config_ID": config_id,
                "Layout_Feasible": str(layout.get("Layout_Feasible", "")),
                "Assigned_Locations_Total": str(assigned_locations_total),
                "Capacity_Margin": str(common._to_int_default(layout.get("Capacity_Margin"), assigned_locations_total - int(common.BASE_OCCUPIED_LOCATIONS_COUNT))),
                "Robustness": f"{robustness:.6f}",
                "Scenario_Pass_Count": str(scenario_pass_count),
                "Scenario_Total_Count": str(scenario_total),
                "Mean_Scenario_Coverage_Ratio": f"{mean_scenario_coverage:.6f}",
                "Worst_Scenario_Coverage_Ratio": f"{worst_scenario_coverage:.6f}",
                "Beam_Relocations_Total": str(beam_relocations_total),
                "Additional_Beams_Required": str(additional_beams),
                "Additional_Grids_Required": str(additional_grids),
                "Failure_Reasons": "; ".join(sorted(failure_reasons)) if failure_reasons else "",
                "Scenario_Details": "; ".join(scenario_failures),
            }
        )

    detail_rows = [
        {
            "Config_ID": row.get("Config_ID", ""),
            "Layout_Feasible": row.get("Layout_Feasible", ""),
            "Assigned_Locations_Total": row.get("Assigned_Locations_Total", ""),
            "Capacity_Margin": row.get("Capacity_Margin", ""),
            "Robustness": row.get("Robustness", ""),
            "Scenario_Pass_Count": row.get("Scenario_Pass_Count", ""),
            "Mean_Scenario_Coverage_Ratio": row.get("Mean_Scenario_Coverage_Ratio", ""),
            "Worst_Scenario_Coverage_Ratio": row.get("Worst_Scenario_Coverage_Ratio", ""),
            "Beam_Relocations_Total": row.get("Beam_Relocations_Total", ""),
            "Additional_Beams_Required": row.get("Additional_Beams_Required", ""),
            "Additional_Grids_Required": row.get("Additional_Grids_Required", ""),
            "Failure_Reasons": row.get("Failure_Reasons", ""),
            "Scenario_Details": row.get("Scenario_Details", ""),
        }
        for row in robustness_rows
    ]

    _write_csv_preserve(
        ROBUSTNESS_DETAILS_FILE,
        [
            "Config_ID",
            "Layout_Feasible",
            "Assigned_Locations_Total",
            "Capacity_Margin",
            "Robustness",
            "Scenario_Pass_Count",
            "Mean_Scenario_Coverage_Ratio",
            "Worst_Scenario_Coverage_Ratio",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
            "Failure_Reasons",
            "Scenario_Details",
        ],
        detail_rows,
    )

    return robustness_rows


def run_robustness_evaluation(method: str = "Baseline") -> list[dict[str, str]]:
    """Evaluate baseline or method-specific layouts and restore shared paths afterward."""
    global LAYOUT_SUMMARY_FILE, ROBUSTNESS_DETAILS_FILE
    method = str(method or "Baseline").strip().upper()
    if method in {"BASELINE", "BASE"}:
        return build_robustness_evaluation()

    method_directories = {
        "SU": "Space_Utilization",
        "PE": "Picking_Efficiency",
        "BP": "Beam_Preservation",
    }
    if method not in method_directories:
        raise ValueError(f"Unknown Stage 7 method: {method}. Expected Baseline, SU, PE, or BP.")

    suffix = method
    directory = method_directories[method]
    stage6_dir = common.OUTPUT_ROOT / f"06_Layout_Generation_{directory}"
    stage7_dir = common.OUTPUT_ROOT / f"07_Robustness_Evaluation_{directory}"
    original_summary_file = LAYOUT_SUMMARY_FILE
    original_details_file = ROBUSTNESS_DETAILS_FILE
    try:
        LAYOUT_SUMMARY_FILE = stage6_dir / f"Candidate_Layout_Summary_{suffix}.csv"
        ROBUSTNESS_DETAILS_FILE = stage7_dir / "Candidate_Layout_Robustness_Details.csv"
        stage7_dir.mkdir(parents=True, exist_ok=True)
        rows = build_robustness_evaluation()
        suffix_output_tree(stage7_dir, suffix)
        return rows
    finally:
        LAYOUT_SUMMARY_FILE = original_summary_file
        ROBUSTNESS_DETAILS_FILE = original_details_file


if __name__ == "__main__":
    # Stage 7 evaluates the implemented layout without synthetic top-fill adjustments.
    method = os.environ.get("PIPELINE_ACTIVE_METHOD", "Baseline")
    robustness_rows = run_robustness_evaluation(method)
    print(
        f"{method} robustness evaluation complete. "
        f"Summary rows: {len(robustness_rows)}."
    )
