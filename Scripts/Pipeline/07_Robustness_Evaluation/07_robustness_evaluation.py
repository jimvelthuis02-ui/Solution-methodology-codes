import csv
from collections import defaultdict
import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

import run_ordered_pipeline as common


LAYOUT_SUMMARY_FILE = common.STAGE6_OUTPUT_DIR / "Candidate_Layout_Summary.csv"
OUTPUT_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Scenario_Evaluation.csv"
ROBUSTNESS_SUMMARY_FILE = common.STAGE7_OUTPUT_DIR / "Candidate_Layout_Robustness_Summary.csv"
CAPACITY_CONSTRAINT_FILE = common.STAGE5_OUTPUT_DIR / "Constraint_Location_Counts_By_Slot_Size.csv"


SKU_SCENARIOS = common._build_sku_count_scenarios([])


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
    """Load Stage 6 candidate layout summary rows."""
    if not LAYOUT_SUMMARY_FILE.exists():
        raise FileNotFoundError(f"Missing layout summary file: {LAYOUT_SUMMARY_FILE}")
    rows = _read_csv(LAYOUT_SUMMARY_FILE)
    if not rows:
        return rows

    if "Pre_Robustness_Status" in rows[0]:
        selected = [
            row
            for row in rows
            if str(row.get("Pre_Robustness_Status", "")).strip().upper() == "SELECTED"
        ]
        return selected

    return rows


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
        required = common._to_int_default(row.get("Min_Required_Locations_At_Or_Above_Size"), 0)
        if config_id and sku_scenario and size >= 0:
            grouped[(config_id, sku_scenario)][size] = max(grouped[(config_id, sku_scenario)].get(size, 0), required)
    return dict(grouped)


def build_robustness_evaluation() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Evaluate each selected layout across low/base/high SKU scenarios."""
    layouts = _layouts()
    scenario_requirements = _scenario_requirements_by_config()

    scenario_rows: list[dict[str, str]] = []
    robustness_rows: list[dict[str, str]] = []

    for layout in layouts:
        layout_id = str(layout.get("Layout_ID", "")).strip()
        if not layout_id:
            continue

        assigned_locations_total = common._to_int_default(
            layout.get("Assigned_Locations_Total") or layout.get("Required_Locations_Total"),
            0,
        )
        total_physical_locations = common._to_int_default(
            layout.get("Total_Physical_Locations") or layout.get("Assigned_Locations_Total") or layout.get("Required_Locations_Total"),
            0,
        )
        beam_relocations_total = common._to_int_default(layout.get("Beam_Relocations_Total"), 0)
        additional_beams = common._to_int_default(layout.get("Additional_Beams_Required"), 0)
        additional_grids = common._to_int_default(layout.get("Additional_Grids_Required"), 0)
        space_left = common._to_float(layout.get("Space_Left")) or 0.0

        occupancy_values: list[float] = []
        utilization_values: list[float] = []
        capacity_margin_values: list[int] = []
        slot_gap_values: list[int] = []
        satisfied_count = 0
        total_count = 0

        layout_slot_counts = _parse_slot_distribution(str(layout.get("Layout_Slot_Size_Distribution", "")))

        for sku_scenario, sku_count in SKU_SCENARIOS.items():
            occupancy = sku_count / max(assigned_locations_total, 1)
            utilization = sku_count / max(total_physical_locations, 1)
            capacity_margin = assigned_locations_total - sku_count
            capacity_ratio = assigned_locations_total / max(sku_count, 1)
            layout_feasible_flag = str(layout.get("Layout_Feasible", "YES")).strip().upper() == "YES"

            required_by_size = scenario_requirements.get((str(layout.get("Config_ID", "")), sku_scenario), {})
            worst_gap = 0
            for size, required in required_by_size.items():
                available = _available_at_or_above(layout_slot_counts, size)
                gap = max(required - available, 0)
                if gap > worst_gap:
                    worst_gap = gap
            slot_coverage_pass = worst_gap == 0

            constraint_satisfied = (
                layout_feasible_flag
                and capacity_margin >= 0
                and occupancy <= 1.0
                and utilization <= 1.0
                and slot_coverage_pass
            )

            scenario_rows.append(
                {
                    "Layout_ID": layout_id,
                    "Config_ID": str(layout.get("Config_ID", "")),
                    "Style": str(layout.get("Style", "")),
                    "SKU_Scenario": sku_scenario,
                    "SKU_Count": str(sku_count),
                    "Constraint_Satisfied": "YES" if constraint_satisfied else "NO",
                    "Occupancy_Rate": f"{occupancy:.6f}",
                    "Utilization_Rate": f"{utilization:.6f}",
                    "Capacity_Margin": str(capacity_margin),
                    "Capacity_Ratio": f"{capacity_ratio:.6f}",
                    "Slot_Coverage_Pass": "YES" if slot_coverage_pass else "NO",
                    "Worst_Slot_Coverage_Gap": str(worst_gap),
                    "Space_Left": f"{space_left:.3f}",
                    "Beam_Relocations_Total": str(beam_relocations_total),
                    "Additional_Beams_Required": str(additional_beams),
                    "Additional_Grids_Required": str(additional_grids),
                }
            )

            occupancy_values.append(occupancy)
            utilization_values.append(utilization)
            capacity_margin_values.append(capacity_margin)
            slot_gap_values.append(worst_gap)
            total_count += 1
            if constraint_satisfied:
                satisfied_count += 1

        # Aggregate scenario-level results into one robustness summary row per layout.
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
                "Worst_Capacity_Margin": str(min(capacity_margin_values) if capacity_margin_values else 0),
                "Worst_Slot_Coverage_Gap": str(max(slot_gap_values) if slot_gap_values else 0),
                "Robustness": f"{(satisfied_count / total_count) if total_count else 0.0:.6f}",
                "Scenario_Pass_Count": str(satisfied_count),
                "Scenario_Total_Count": str(total_count),
                "Beam_Relocations_Total": str(beam_relocations_total),
                "Additional_Beams_Required": str(additional_beams),
                "Additional_Grids_Required": str(additional_grids),
                "Space_Left": f"{space_left:.3f}",
            }
        )

    # Write scenario-level evaluation output.
    _write_csv_preserve(
        OUTPUT_FILE,
        [
            "Layout_ID",
            "Config_ID",
            "Style",
            "SKU_Scenario",
            "SKU_Count",
            "Constraint_Satisfied",
            "Occupancy_Rate",
            "Utilization_Rate",
            "Capacity_Margin",
            "Capacity_Ratio",
            "Slot_Coverage_Pass",
            "Worst_Slot_Coverage_Gap",
            "Space_Left",
            "Beam_Relocations_Total",
            "Additional_Beams_Required",
            "Additional_Grids_Required",
        ],
        scenario_rows,
    )

    # Write layout-level robustness summary used by final ranking.
    _write_csv_preserve(
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
            "Worst_Capacity_Margin",
            "Worst_Slot_Coverage_Gap",
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
    # Stage 7 entrypoint: produce detailed scenario checks and aggregated robustness.
    scenario_rows, robustness_rows = build_robustness_evaluation()
    print(
        "Robustness evaluation complete. "
        f"Scenario rows: {len(scenario_rows)}, summary rows: {len(robustness_rows)}."
    )
